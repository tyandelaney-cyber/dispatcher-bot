import os
import re
import json
import base64
import logging
import tempfile
import mimetypes
import httpx
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_KEY")
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY")

WAITING_LOCATION = 1

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-2.5-flash-lite")

EXTRACT_PROMPT = """You are a freight dispatcher assistant.
Analyze this load/rate confirmation and return ONLY a valid JSON object, no explanation, no markdown, no backticks.
JSON format:
{
  "broker": "broker company name only",
  "load_number": "load number",
  "equipment": "equipment type",
  "weight": "total weight with unit",
  "rate": "total rate e.g. $2,500.00",
  "rate_per_mile": "rate per mile e.g. $3.38/mile or empty string",
  "pickups": [
    {
      "number": 1,
      "facility": "facility name or empty string",
      "address": "full street address, city, state zip",
      "date": "MM/DD/YY",
      "time": "time or ASAP",
      "instruction": "LIVE LOAD or DROP or empty",
      "commodity": "commodity description or empty"
    }
  ],
  "deliveries": [
    {
      "number": 1,
      "facility": "facility name or empty string",
      "address": "full street address, city, state zip",
      "date": "MM/DD/YY",
      "time": "time or ASAP",
      "instruction": "LIVE UNLOAD or DROP or empty",
      "commodity": "commodity description or empty"
    }
  ],
  "special_instructions": "ONLY include critical operational warnings such as: deductions, late fees, required apps, trailer requirements, confirmation requirements, photo requirements. Do NOT include standard legal boilerplate, payment instructions, invoice instructions, broker-carrier agreement text, or email addresses. If there are no critical warnings, return empty string."
}
Rules:
- Include ALL pickup and delivery stops found
- Addresses must be complete for accurate mileage calculation
- special_instructions: ONLY warnings, deductions, penalties, required actions (e.g. LATE PICKUP $500 DEDUCTION, MUST USE AMAZON RELAY, MUST SEND TRAILER PICTURES). Ignore all legal/payment/invoice boilerplate.
- Return ONLY the JSON, nothing else"""

FORMAT_PROMPT = """You are a freight dispatcher assistant.
Format the dispatcher message EXACTLY as shown in this example. Do not add anything extra.

EXAMPLE OUTPUT:
📌Broker: RAVEN CARGO LOGISTICS
Al Amin Express Inc
Load: 0228914

🟢PU 1: LGE DC (NTX)
14901 N Beach St, Fort Worth, TX 76177
📅Date: 06/25/26
🕔Time: ASAP
🚛 Instruction: LIVE LOAD
📤Commodity:
❕VRID#
❕PU#:
❕BOL#
❕Appt#
❕PO#

🔴DO 1: SAMS 8234
3301 EAST PARK & BLASS AVENUE, SEARCY, AR 72143
📅Date: 06/25/26
🕔Time: 16:00
🚛 Instruction: LIVE UNLOAD
📤Commodity:
❕VRID#
❕PU#:
❕BOL#
❕Appt#
❕PO#

🔴DO 2: 6018
2103 SOUTH MAIN, SEARCY, AR 72143
📅Date: 06/25/26
🕔Time: 17:00
🚛 Instruction: LIVE UNLOAD
📤Commodity:
❕VRID#
❕PU#:
❕BOL#
❕Appt#
❕PO#

Empty: 138 mile
Loaded: 401 mile

💰Rate: $2,500.00 ($3.38/mile)
⚖️Weight: 7,283 lbs
🚚Equipment: V - Van

❌MUST SEND TRAILER PICTURES, TRAILER REGISTRATION PAPER TO THE GROUP AND WAIT FOR CONFIRMATION!!!
❌LATE PICKUP $500 DEDUCTION!!!
❌LATE DELIVERY $700 DEDUCTION!!!
❌MUST USE AMAZON RELAY

STRICT RULES:
- Line 1: 📌Broker: BROKER_NAME
- Line 2: Al Amin Express Inc  (always add this line after broker name)
- Line 3: Load: LOAD_NUMBER
- Empty line before each stop
- 🟢PU for pickups, 🔴DO for deliveries
- Facility name on SAME line as PU/DO emoji
- Address on next line (single line, no line breaks in address)
- Every stop MUST have: 📅Date, 🕔Time, 🚛 Instruction, 📤Commodity, and all 5 ❕ fields
- Empty and Loaded miles section
- 💰Rate, ⚖️Weight, 🚚Equipment section
- special_instructions printed in FULL at the bottom, each warning on its own line
- If special_instructions is empty, do not print anything at the bottom
- Output ONLY the message, nothing else, no explanation, no markdown"""


def get_mime(filename):
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


async def get_distance_miles(origin, destination):
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": origin,
        "destinations": destination,
        "units": "imperial",
        "key": GOOGLE_MAPS_KEY
    }
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(url, params=params)
    data = r.json()
    try:
        element = data["rows"][0]["elements"][0]
        if element["status"] != "OK":
            return 0
        return round(element["distance"]["value"] / 1609.344)
    except Exception:
        return 0


async def calculate_miles(current_location, load_data):
    all_stops = [pu["address"] for pu in load_data.get("pickups", [])]
    all_stops += [do["address"] for do in load_data.get("deliveries", [])]
    if not all_stops:
        return 0, 0
    empty = await get_distance_miles(current_location, all_stops[0])
    loaded = 0
    for i in range(len(all_stops) - 1):
        loaded += await get_distance_miles(all_stops[i], all_stops[i + 1])
    return empty, loaded


async def extract_load_data(file_bytes, filename, caption=""):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = get_mime(filename)

    if mime.startswith("image/"):
        image_part = {"mime_type": mime, "data": file_bytes}
        response = model.generate_content([EXTRACT_PROMPT, image_part])
    elif mime == "application/pdf" or ext == "pdf":
        image_part = {"mime_type": "application/pdf", "data": file_bytes}
        response = model.generate_content([EXTRACT_PROMPT, image_part])
    elif ext in ("docx", "doc"):
        try:
            import docx as _docx
            with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            doc = _docx.Document(tmp_path)
            os.unlink(tmp_path)
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            text = "[Could not parse: " + str(e) + "]"
        response = model.generate_content(EXTRACT_PROMPT + "\n\nDocument content:\n" + text)
    elif ext in ("xlsx", "xls", "csv"):
        try:
            if ext == "csv":
                text = file_bytes.decode("utf-8", errors="replace")[:8000]
            else:
                import openpyxl
                with tempfile.NamedTemporaryFile(suffix="." + ext, delete=False) as tmp:
                    tmp.write(file_bytes)
                    tmp_path = tmp.name
                wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
                os.unlink(tmp_path)
                rows = []
                for sheet in wb.worksheets:
                    rows.append("=== " + sheet.title + " ===")
                    for row in sheet.iter_rows(values_only=True):
                        rows.append("\t".join(str(c) if c is not None else "" for c in row))
                text = "\n".join(rows)[:8000]
        except Exception as e:
            text = "[Could not parse: " + str(e) + "]"
        response = model.generate_content(EXTRACT_PROMPT + "\n\nSpreadsheet content:\n" + text)
    else:
        text = file_bytes.decode("utf-8", errors="replace")[:8000]
        response = model.generate_content(EXTRACT_PROMPT + "\n\nFile content:\n" + text)

    raw = response.text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"```$", "", raw).strip()
    return json.loads(raw)


async def format_message(load_data, empty_miles, loaded_miles):
    prompt = (
        FORMAT_PROMPT
        + "\n\nLoad data JSON:\n"
        + json.dumps(load_data, indent=2)
        + "\n\nEmpty miles: " + str(empty_miles)
        + "\nLoaded miles: " + str(loaded_miles)
    )
    response = model.generate_content(prompt)
    return response.text.strip()


async def start(update, context):
    await update.message.reply_text(
        "👋 Dispatcher Bot ready!\n\n"
        "Send me a load confirmation (photo, PDF, Word, Excel) and I'll format it.\n"
        "I'll also ask your current location to calculate empty & loaded miles.\n\n"
        "/help for more info."
    )
    return ConversationHandler.END


async def help_cmd(update, context):
    await update.message.reply_text(
        "📌 Send any load/rate confirmation:\n"
        "🖼 Photo / screenshot\n"
        "📋 PDF\n"
        "📄 Word (.docx)\n"
        "📊 Excel / CSV\n\n"
        "The bot will:\n"
        "1️⃣ Extract all load info\n"
        "2️⃣ Ask your current location\n"
        "3️⃣ Calculate empty & loaded miles\n"
        "4️⃣ Send formatted dispatcher message\n\n"
        "/cancel to cancel current operation"
    )
    return ConversationHandler.END


async def receive_file(update, context):
    msg = update.message
    caption = msg.caption or ""
    await msg.reply_text("⏳ Reading document...")
    try:
        if msg.document:
            f = await msg.document.get_file()
            file_bytes = bytes(await f.download_as_bytearray())
            filename = msg.document.file_name or "file"
        else:
            f = await msg.photo[-1].get_file()
            file_bytes = bytes(await f.download_as_bytearray())
            filename = "photo.jpg"

        load_data = await extract_load_data(file_bytes, filename, caption)
        context.user_data["load_data"] = load_data

        pu_count = len(load_data.get("pickups", []))
        do_count = len(load_data.get("deliveries", []))
        await msg.reply_text(
            "✅ Found: " + str(pu_count) + " pickup(s), " + str(do_count) + " delivery stop(s)\n\n"
            "📍 What's your current location?\n"
            "(Type city name or full address, e.g. 'Dallas, TX')"
        )
        return WAITING_LOCATION
    except Exception as e:
        logger.exception("File processing error")
        await msg.reply_text("❌ Error reading file: " + str(e))
        return ConversationHandler.END


async def receive_location(update, context):
    current_location = update.message.text.strip()
    load_data = context.user_data.get("load_data", {})
    await update.message.reply_text("🗺 Calculating miles...")
    try:
        empty_miles, loaded_miles = await calculate_miles(current_location, load_data)
        final_message = await format_message(load_data, empty_miles, loaded_miles)
        await update.message.reply_text(final_message)
    except Exception as e:
        logger.exception("Miles calculation error")
        await update.message.reply_text("❌ Error: " + str(e))
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Send a new load document whenever you're ready.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.ALL, receive_file),
            MessageHandler(filters.PHOTO, receive_file),
        ],
        states={
            WAITING_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_location)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(conv_handler)
    logger.info("✅ Bot running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
