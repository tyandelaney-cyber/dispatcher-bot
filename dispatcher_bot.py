import io
import json
import logging
import os
from html import escape
from urllib.parse import quote_plus

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

# ---------- CONFIG (hosting dashboard -> Environment variables) ----------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
BOT_PASSWORD = os.environ["BOT_PASSWORD"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
CARRIER_NAME = os.environ.get("CARRIER_NAME", "Al Amin Express Inc")
CARRIER_MC = os.environ.get("CARRIER_MC", "")
DEFAULT_LOCATION = os.environ.get("DEFAULT_LOCATION", "")  # truck location, e.g. "Atlanta, GA"
AUTH_FILE = os.environ.get("AUTH_FILE", "auth.json")

client = genai.Client(api_key=GEMINI_API_KEY)

PROMPT = """You extract data from a freight rate confirmation. Return ONLY JSON:
{
  "ref": "load/reference number",
  "broker": "broker company name",
  "po": "PO number or empty string",
  "pay": number (total rate in USD, no symbols),
  "pickup": {
    "name": "facility name",
    "address": "street, city, state zip",
    "date": "MM/DD/YY",
    "time": "HH:MM 24h with colon, or window HH:MM-HH:MM; empty if none"
  },
  "delivery": {
    "name": "facility name",
    "address": "street, city, state zip",
    "date": "MM/DD/YY",
    "time": "HH:MM 24h with colon, or window HH:MM-HH:MM; empty if none (use 24/7 if stated)"
  },
  "weight": "weight as written, or empty",
  "equipment": "e.g. Reefer 53' / Dry Van 53'",
  "commodity": "commodity description or empty",
  "requirements": "short one-line summary of special requirements (apps, tracking, trailer, phone rules), or empty"
}
Rules: times must always be colon formatted (16:00, never 1600). Use empty string
when data is missing. Never invent values."""


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


# ---------- FILE READING ----------
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


def extract_load(data: bytes, mime: str, filename: str = "") -> dict:
    name = filename.lower()
    if name.endswith(".docx"):
        contents = [PROMPT, docx_text(data)]
    elif name.endswith((".xlsx", ".xlsm")):
        contents = [PROMPT, xlsx_text(data)]
    else:
        contents = [PROMPT, types.Part.from_bytes(data=data, mime_type=mime)]
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0
        ),
    )
    return json.loads(resp.text)


# ---------- MAPS ----------
async def miles_between(origin: str, dest: str) -> float | None:
    if not origin or not dest:
        return None
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": origin,
        "destinations": dest,
        "units": "imperial",
        "key": MAPS_API_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.get(url, params=params)
        el = r.json()["rows"][0]["elements"][0]
        if el.get("status") != "OK":
            return None
        return el["distance"]["value"] / 1609.344
    except Exception as exc:
        log.warning("maps failed: %s", exc)
        return None


def maps_search(q: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"


def maps_route(a: str, b: str) -> str:
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(a)}&destination={quote_plus(b)}&travelmode=driving"
    )


# ---------- FORMATTING (pure Python, no API calls) ----------
def when(s: dict) -> str:
    return " ".join(x for x in [s.get("date", ""), s.get("time", "")] if x)


def format_load(d: dict, loaded: float | None, dh: float | None):
    e = escape
    pu, do = d["pickup"], d["delivery"]
    route = maps_route(pu["address"], do["address"])

    pay = float(d.get("pay") or 0)
    total_miles = (loaded or 0) + (dh or 0)
    rpm = f"${pay / total_miles:.2f}/mi" if pay and total_miles else "—"
    miles_txt = f"{round(loaded):,}" if loaded else "—"

    carrier = f"🚚 <b>Carrier:</b> {e(CARRIER_NAME)}"
    if CARRIER_MC:
        carrier += f" • <b>MC</b> {e(CARRIER_MC)}"

    def stop(icon: str, title: str, s: dict) -> str:
        return (
            f"{icon} <b>{title}</b>\n"
            f"📅 <b>{e(when(s))}</b>\n"
            f"🏠 {e(s.get('name', ''))}\n"
            f"<pre>{e(s['address'])}</pre>\n"
            f"👉 <a href=\"{maps_search(s['address'])}\">GOOGLE MAP</a>"
        )

    header = (
        f"📦 <b>NEW LOAD</b> • 🏷 <b>REF:</b> {e(str(d.get('ref', '')))}\n"
        f"{carrier}\n"
        f"🤝 <b>Broker:</b> {e(d.get('broker', ''))}"
    )
    if d.get("po"):
        header += f"\n<b>PO#</b> {e(str(d['po']))}"

    money = (
        f"💰 <b>PAY:</b> ${pay:,.0f}\n"
        f"📉 <b>RPM:</b> {rpm}\n"
        f"🚛 <b>Miles:</b> {miles_txt}\n"
        f"🗺 <a href=\"{route}\">Open full route</a>"
    )

    specs = []
    if d.get("weight"):
        specs.append(f"• ⚖️ {e(str(d['weight']))}")
    if d.get("equipment"):
        specs.append(f"• 🚛 {e(d['equipment'])}")
    if d.get("commodity"):
        specs.append(f"• 📦 {e(d['commodity'])}")

    parts = [header, money, stop("🛫", "Pickup", pu), stop("🏁", "Delivery", do)]
    if specs:
        parts.append("📦 <b>Load Specs</b>\n" + "\n".join(specs))
    if d.get("requirements"):
        parts.append(f"⚠️ <b>Requirements:</b> {e(d['requirements'])}")
    parts.append(f"➡️📋 <a href=\"{route}\">DRIVER INSTRUCTIONS — tap to open</a>")

    return "\n\n".join(parts), route


# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await gate(update):
        return
    await update.message.reply_text(
        "Rate confirmation yuboring (PDF, rasm, Word, Excel).\n"
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
    await update.message.reply_text("Rate confirmation fayl yoki rasm yuboring.")


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
        d = extract_load(data, mime, filename)

        pu_addr = d["pickup"]["address"]
        do_addr = d["delivery"]["address"]
        truck = context.user_data.get("loc") or DEFAULT_LOCATION

        loaded = await miles_between(pu_addr, do_addr)
        dh = await miles_between(truck, pu_addr) if truck else None

        text, route = format_load(d, loaded, dh)
        await msg.reply_text(
            text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=route, show_above_text=False, prefer_large_media=False
            ),
        )
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
