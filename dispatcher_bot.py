import io
import json
import logging
import os
import re
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
CARRIER_MC = env("CARRIER_MC", required=False)
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
    """Driving miles via Google Routes API (Distance Matrix is legacy)."""
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
        data = r.json()
        meters = data["routes"][0]["distanceMeters"]
        return meters / 1609.344
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
    return " ".join(x
