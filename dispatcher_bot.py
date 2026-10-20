import io
import json
import logging
import os
import re
import unicodedata
from html import escape as _escape

import httpx
from docx import Document
from google import genai
from google.genai import types
from openpyxl import load_workbook
from telegram import LinkPreviewOptions, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dispatcher-bot")

def escape(s: str) -> str:
    return _escape(s, quote=False)


# ---------- CONFIG (Fly.io -> Secrets) ----------
MISSING: list[str] = []


def env(*names: str, required: bool = True, default: str = "") -> str:
    """Birinchi topilgan nomni oladi, bo'sh joylarni olib tashlaydi."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    if required:
        MISSING.append(names[0])
    return default


BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "TELEGRAM_TOKEN")
GEMINI_API_KEY = env("GEMINI_API_KEY", "GOOGLE_API_KEY")
MAPS_API_KEY = env("GOOGLE_MAPS_API_KEY", "MAPS_API_KEY")
BOT_PASSWORD = env("BOT_PASSWORD")
GEMINI_MODEL = env("GEMINI_MODEL", required=False, default="gemini-2.5-flash")
CARRIER_NAME = env("CARRIER_NAME", required=False, default="Al Amin Express Inc")
DEFAULT_LOCATION = env("DEFAULT_LOCATION", required=False)
AUTH_FILE = env("AUTH_FILE", required=False, default="auth.json")

if MISSING:
    seen = sorted(
        k
        for k in os.environ
        if not k.startswith(("FLY_", "PYTHON", "LANG", "GPG", "PATH", "HOME", "PWD"))
    )
    raise SystemExit(f"YETISHMAYAPTI: {MISSING} | MAVJUD NOMLAR: {seen}")

client = genai.Client(api_key=GEMINI_API_KEY)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

PROMPT = """You read a freight load document (broker rate confirmation, Amazon Relay load,
screenshot, or pasted text). First decide "kind":
- "amazon": an Amazon Relay trip (Trip ID or Map ID, facility codes like CLT2 or POLY_PAK_07105_272,
  stop types such as Preloaded, Drop, Live, Loaded - Live, Empty - Drop).
- "regular": any other broker rate confirmation.

Return ONLY JSON in this shape (fill the matching object, the other one is null):
{
  "kind": "regular" or "amazon",
  "regular": {
    "broker": "broker company name",
    "load": "load / reference number",
    "pickups": [ STOP, ... ],
    "deliveries": [ STOP, ... ],
    "warnings": ["every penalty, deduction or mandatory requirement stated in the document,
                  one short UPPERCASE line each, keep dollar amounts and the word DEDUCTION"]
  },
  "amazon": {
    "id_label": "MAP" or "TRIP",
    "id": "map id or trip id",
    "stops": [ {"stop_id": "the id printed after the stop number, e.g. 1129LLGC6",
                "type": "e.g. Loaded - Drop", "date": "as written, e.g. Sun, Oct 12, 06:37 PM EDT",
                "name": "facility code/name, e.g. BOS3", "street": "street", "city": "City, ST ZIP"} ],
    "miles": trip miles as number or 0,
    "dh": deadhead miles as number or 0,
    "duration": "e.g. 15H 45M or empty",
    "penalties": [ {"label": "Late PU", "amount": 1000} ]
  }
}
STOP = {"name": "facility name", "street": "street", "city": "city", "state": "2-letter state",
        "zip": "zip", "date": "MM/DD/YYYY", "time": "HHMM 24h without colon, or HHMM-HHMM window, or empty",
        "instruction": "DROP / LIVE / etc, or empty", "commodity": "or empty",
        "pu_number": "pickup number or empty", "bol": "BOL number or empty"}
Rules: use empty string when data is missing. Never invent values."""


# ---------- AUTH ----------
def load_auth() -> set[int]:
    try:
        with open(AUTH_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_auth(ids: set[int]) -> None:
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump(sorted(ids), f)
    except Exception as exc:
        log.warning("auth save failed: %s", exc)


AUTHORIZED = load_auth()


async def gate(update: Update) -> bool:
    """True if user may use the bot. First message = password."""
    uid = update.effective_user.id
    if uid in AUTHORIZED:
        return True
    text = (update.message.text or "").strip() if update.message else ""
    if text and text == BOT_PASSWORD:
        AUTHORIZED.add(uid)
        save_auth(AUTHORIZED)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.effective_chat.send_message("✅ Parol to'g'ri. Rate confirmation yuboring.")
        return False
    await update.effective_chat.send_message("🔒 Parolni kiriting.")
    return False


# ---------- FILE READING + GEMINI ----------
def docx_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for t in doc.tables:
        for row in t.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(lines)


def xlsx_text(data: bytes) -> str:
    wb = load_workbook(io.BytesIO(data), data_only=True)
    lines = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def gemini_json(contents: list) -> dict:
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0
        ),
    )
    return json.loads(resp.text)


def extract_file(data: bytes, mime: str, filename: str = "") -> dict:
    name = filename.lower()
    if name.endswith(".docx"):
        contents = [PROMPT, docx_text(data)]
    elif name.endswith((".xlsx", ".xlsm")):
        contents = [PROMPT, xlsx_text(data)]
    else:
        contents = [PROMPT, types.Part.from_bytes(data=data, mime_type=mime)]
    return gemini_json(contents)


def extract_text(text: str) -> dict:
    return gemini_json([PROMPT, text])


# ---------- MAPS (Routes API) ----------
async def miles_between(origin: str, dest: str) -> float | None:
    """Driving miles via Google Routes API."""
    if not origin or not dest:
        return None
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "X-Goog-Api-Key": MAPS_API_KEY,
        "X-Goog-FieldMask": "routes.distanceMeters",
        "Content-Type": "application/json",
    }
    body = {
        "origin": {"address": origin},
        "destination": {"address": dest},
        "travelMode": "DRIVE",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(url, headers=headers, json=body)
        meters = r.json()["routes"][0]["distanceMeters"]
        return meters / 1609.344
    except Exception as exc:
        log.warning("maps failed: %s", exc)
        return None


async def chain_miles(addrs: list[str]) -> float | None:
    if len(addrs) < 2:
        return None
    total = 0.0
    for a, b in zip(addrs, addrs[1:]):
        m = await miles_between(a, b)
        if m is None:
            return None
        total += m
    return total


def full_address(s: dict) -> str:
    tail = " ".join(x for x in [s.get("state", ""), s.get("zip", "")] if x)
    return ", ".join(x for x in [s.get("street", ""), s.get("city", ""), tail] if x)


# ---------- REGULAR DISPATCH FORMAT ----------
def city_line(s: dict) -> str:
    parts = [
        str(s.get("city", "")).upper(),
        str(s.get("state", "")).upper(),
        str(s.get("zip", "")),
    ]
    return ", ".join(p for p in parts if p)


def reg_stop(head: str, s: dict, pu: bool = False) -> str:
    e = escape
    lines = [head]
    for v in (s.get("name"), s.get("street"), city_line(s)):
        if v:
            lines.append(e(str(v)))
    if s.get("date"):
        lines.append(f"📅Date: {e(str(s['date']))}")
    if s.get("time"):
        lines.append(f"🕔Time : {e(str(s['time']))}")
    if s.get("instruction"):
        lines.append(f"<code>🚛 Instruction:{e(str(s['instruction']).upper())}</code>")
    if s.get("commodity"):
        lines.append(f"<code>📤Commodity: {e(str(s['commodity']))}</code>")
    if pu and s.get("pu_number"):
        lines.append(f"<code>❕PU#: {e(str(s['pu_number']))}</code>")
    if s.get("bol"):
        lines.append(f"<code>❕BOL# {e(str(s['bol']))}</code>")
    return "\n".join(lines)


def format_regular(r: dict, empty_miles: float | None, loaded_miles: float | None) -> str:
    e = escape
    parts = [
        f"📌Broker: {e(str(r.get('broker', '')))}\n"
        f"{e(CARRIER_NAME)}\n"
        f"Load: {e(str(r.get('load', '')))}"
    ]
    for i, s in enumerate(r.get("pickups") or [], 1):
        parts.append(reg_stop(f"🟢PU {i} :", s, pu=True))
    for i, s in enumerate(r.get("deliveries") or [], 1):
        parts.append(reg_stop(f"🔴DO {i}:", s))
    em = f"{round(empty_miles)}" if empty_miles is not None else "—"
    lm = f"{round(loaded_miles)}" if loaded_miles is not None else "—"
    parts.append(f"Empty : {em} mile\nLoaded : {lm} mile")
    for w in r.get("warnings") or []:
        parts.append("❌" + e(str(w).strip()))
    return "\n\n".join(parts)


# ---------- AMAZON FORMAT ----------
SEP = "—————————————"


def norm(text: str) -> str:
    """Qalin unicode harflarni (𝗥𝗮𝘁𝗲) oddiy ASCII ga aylantiradi."""
    return unicodedata.normalize("NFKC", text)


ID_RE = re.compile(r"(TRIP|MAP)\s*ID\s*:\s*(\S+)", re.I)
SEP_RE = re.compile(r"^[—–\-_=]{3,}$")
SUMMARY_START = ("🚛", "🕒", "💰", "💵", "❌")


def is_amazon_text(text: str) -> bool:
    t = norm(text)
    return bool(ID_RE.search(t)) and "📍" in t


def parse_stop(chunk: str) -> dict | None:
    lines = [ln.strip() for ln in chunk.splitlines()]
    head = re.match(r"📍\s*(\d+)#:\s*(.*)", lines[0]) if lines else None
    if not head:
        return None
    body = []
    for ln in lines[1:]:
        if not ln:
            continue
        if SEP_RE.match(ln) or ln.startswith(SUMMARY_START):
            break
        body.append(ln)

    date, rest = "", []
    for ln in body:
        if ln.startswith("📅") or re.search(r"\d{1,2}:\d{2}", ln):
            date = re.sub(r"^📅\s*(Date:)?\s*", "", ln).strip()
        else:
            rest.append(ln)

    if len(rest) < 3:
        return None
    typ = rest[0] if len(rest) >= 4 else ""
    core = rest[1:] if len(rest) >= 4 else rest
    return {
        "stop_id": head.group(2).strip(),
        "type": typ,
        "date": date,
        "name": core[0],
        "street": ", ".join(core[1:-1]),
        "city": core[-1],
    }


def parse_amazon_text(raw: str) -> dict:
    text = norm(raw)

    def g(pattern: str, default: str = "") -> str:
        m = re.search(pattern, text, re.I)
        return m.group(1).strip() if m else default

    def num(s: str) -> float:
        try:
            return float(s.replace(",", ""))
        except ValueError:
            return 0.0

    stops = []
    for chunk in re.split(r"(?=📍\s*\d+#:)", text):
        chunk = chunk.lstrip()
        if chunk.startswith("📍"):
            st = parse_stop(chunk)
            if st:
                stops.append(st)

    idm = ID_RE.search(text)
    return {
        "id_label": idm.group(1).upper() if idm else "TRIP",
        "id": idm.group(2) if idm else "",
        "stops": stops,
        "miles": num(g(r"Trip:\s*([\d,\.]+)\s*mi")),
        "dh": num(g(r"DH:\s*([\d,\.]+)")),
        "duration": g(r"Duration:\s*(.+)"),
        "penalties": [
            {"label": k, "amount": v}
            for k, v in re.findall(r"❌\s*(.+?):\s*\$([\d,]+)", text)
        ],
    }


def to_float(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def format_amazon(a: dict, dh: float | None) -> str:
    e = escape
    ident = str(a.get("id", ""))
    label = "Map" if str(a.get("id_label", "")).upper() == "MAP" else "Trip"
    miles = to_float(a.get("miles"))

    out = [f"🗺 <b>{label} ID :</b> {e(ident)}"]
    for i, st in enumerate(a.get("stops") or [], 1):
        out.append(f"📍{i}#: {e(str(st.get('stop_id') or ident))}")
        for v in (st.get("type"), st.get("date"), st.get("name"), st.get("street"), st.get("city")):
            if v:
                out.append(e(str(v)))
        out.append(SEP)

    if miles:
        out.append(f"🚛 <b>Trip:</b> {miles:,.0f}mi")
    out.append(f"DH: {round(dh) if dh is not None else '—'}")
    if a.get("duration"):
        out.append(f"🕒 Duration: {e(str(a['duration']).upper())}")
    pens = a.get("penalties") or []
    if pens:
        out.append("")
        for p in pens:
            out.append(f"❌{e(str(p.get('label', '')))}: ${to_float(p.get('amount')):.0f}")
    return "\n".join(out)


# ---------- SENDING ----------
async def send_result(msg, result: dict, truck: str) -> None:
    kind = result.get("kind")
    if kind == "amazon" and result.get("amazon"):
        a = result["amazon"]
        dh = to_float(a.get("dh")) or None
        stops = a.get("stops") or []
        if dh is None and truck and stops:
            first = stops[0]
            dh = await miles_between(
                truck, f"{first.get('street', '')}, {first.get('city', '')}"
            )
        await msg.reply_text(
            format_amazon(a, dh),
            parse_mode="HTML",
            link_preview_options=NO_PREVIEW,
        )
        return

    r = result.get("regular") or {}
    pickups = r.get("pickups") or []
    deliveries = r.get("deliveries") or []
    stops = [full_address(s) for s in pickups + deliveries]
    loaded = await chain_miles(stops)
    empty = None
    if truck and pickups:
        empty = await miles_between(truck, full_address(pickups[0]))
    await msg.reply_text(
        format_regular(r, empty, loaded),
        parse_mode="HTML",
        link_preview_options=NO_PREVIEW,
    )


# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update):
        return
    await update.message.reply_text(
        "Rate confirmation yuboring (PDF, rasm, Word, Excel) yoki matnini tashlang.\n"
        "Truck joylashuvi: /loc Atlanta, GA"
    )


async def set_loc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED:
        await update.message.reply_text("🔒 Parolni kiriting.")
        return
    loc = " ".join(context.args).strip()
    if not loc:
        cur = context.user_data.get("loc") or DEFAULT_LOCATION or "yo'q"
        await update.message.reply_text(f"Hozirgi joylashuv: {cur}\nO'zgartirish: /loc City, ST")
        return
    context.user_data["loc"] = loc
    await update.message.reply_text(f"📍 Joylashuv: {loc}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update):
        return
    msg = update.message
    text = msg.text or ""
    truck = context.user_data.get("loc") or DEFAULT_LOCATION

    try:
        if is_amazon_text(text):
            a = parse_amazon_text(text)
            if not a["stops"]:
                raise ValueError("stop'lar topilmadi, matnni to'liq tashlang")
            await send_result(msg, {"kind": "amazon", "amazon": a}, truck)
        elif len(text.strip()) >= 80:
            status = await msg.reply_text("⏳ O'qilyapti...")
            try:
                result = extract_text(text)
                await send_result(msg, result, truck)
            finally:
                try:
                    await status.delete()
                except Exception:
                    pass
        else:
            await msg.reply_text("Rate confirmation fayl/rasm yuboring yoki yuk matnini tashlang.")
    except Exception as exc:
        log.exception("text processing failed")
        await msg.reply_text(f"❌ Xatolik: {exc}")


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED:
        await update.message.reply_text("🔒 Parolni kiriting.")
        return

    msg = update.message
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        mime, filename = "image/jpeg", "photo.jpg"
    elif msg.document:
        tg_file = await msg.document.get_file()
        mime = msg.document.mime_type or "application/pdf"
        filename = msg.document.file_name or ""
    else:
        return

    status = await msg.reply_text("⏳ O'qilyapti...")
    try:
        data = bytes(await tg_file.download_as_bytearray())
        result = extract_file(data, mime, filename)
        truck = context.user_data.get("loc") or DEFAULT_LOCATION
        await send_result(msg, result, truck)
    except Exception as exc:
        log.exception("processing failed")
        await msg.reply_text(f"❌ Xatolik: {exc}")
    finally:
        try:
            await status.delete()
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("loc", set_loc))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
