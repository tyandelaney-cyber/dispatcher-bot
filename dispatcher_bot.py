import os
import re
import json
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
      "address_line1": "street address only e.g. 14901 N Beach St",
      "address_line2": "city, state, zip e.g. Fort Worth, TX, 76177",
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
      "address_line1": "street address only e.g. 124 Davis St",
      "address_line2": "city, state, zip e.g. Portland, TN, 37148",
      "date": "MM/DD/YY",
      "time": "time or ASAP",
      "instruction": "LIVE UNLOAD or DROP or empty",
      "commodity": "commodity description or empty"
    }
  ],
  "special_instructions": "ONLY critical warnings: deductions, late fees, required apps, trailer requirements, confirmation requirements, photo requirements. Do NOT include legal boilerplate, payment instructions, invoice instructions, broker-carrier agreement text, or email addresses. If none, return empty string."
}
Rules:
- Include ALL pickup and delivery stops
- facility: location name/code
- address_line1: street only
- address_line2: city, state, zip
- special_instructions: ONLY warnings/deductions/penalties. Ignore legal/payment boilerplate.
- Return ONLY the JSON, nothing else"""


def get_mime(filename):
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def build_message(load_data, empty_miles, loaded_miles):
    lines = []

    # Header
    lines.append(f"📌Broker:  {load_data.get('broker', '')}")
    lines.append("Al Amin Express Inc")
    lines.append(f"Load:    {load_data.get('load_number', '')}")

    # Pickups
    for pu in load_data.get("pickups", []):
        lines.append("")
        lines.append(f"🟢PU {pu['number']} :")
        if pu.get("facility"):
            lines.append(pu["facility"])
        if pu.get("address_line1"):
            lines.append(pu["address_line1"])
        if pu.get("address_line2"):
            lines.append(pu["address_line2"])
        lines.append(f"📅Date:{pu.get('date', '')}")
        lines.append(f"🕔time :    {pu.get('time', '')}")
        lines.append(f"```")
        lines.append(f"🚛 Instruction:{pu.get('instruction', '')}")
        lines.append(f"📤Commodity: {pu.get('commodity', '')}")
        lines.append(f"❕VRID#")
        lines.append(f"❕PU#:")
        lines.append(f"❕BOL#")
        lines.append(f"❕Appt#")
        lines.append(f"❕PO#")
        lines.append(f"```")

    # Deliveries
    for do_ in load_data.get("deliveries", []):
        lines.append("")
        lines.append(f"🔴DO {do_['number']}:")
        if do_.get("facility"):
            lines.append(do_["facility"])
        if do_.get("address_line1"):
            lines.append(do_["address_line1"])
        if do_.get("address_line2"):
            lines.append(do_["address_line2"])
        lines.append(f"📅Date:{do_.get('date', '')}")
        lines.append(f"🕔time : {do_.get('time', '')}")
        lines.append(f"```")
        lines.append(f"🚛 Instruction:{do_.get('instruction', '')}")
        lines.append(f"📤Commodity: {do_.get('commodity', '')}")
        lines.append(f"❕VRID#")
        lines.append(f"❕PU#:")
        lines.append(f"❕BOL#")
        lines.append(f"❕Appt#")
        lines.append(f"❕PO#")
        lines.append(f"```")

    # Miles
    lines.append("")
    lines.append(f"Empty :  {empty_miles} mile")
    lines.append(f"Loaded :  {loaded_miles} mile")

    # Special instructions
    special = load_data.get("special_instructions", "").strip()
    if special:
       lines.append("")
lines.append("❌MUST SEND TRAILER PICTURES, TRAILER REGISTRATION PAPER, TRAILER REGISTRATION PAPER, TO THE GROUP AND WAIT FOR THE GOOD TO GO CONFIRMATION BY THE ONLY DISPATCHER/UPDATER.")
lines.append("DO NOT DEPART WITHOUT CONFIRMATION!!!")
lines.append("")
lines.append("❌LATE PICKUP $500 DEDUCTION!!!")
lines.append("")
lines.append("❌LATE DELIVERY $700 DEDUCTION!!!")
lines.append("")
lines.append("❌MUST USE AMAZON RELAY")

    return "\n".join(lines)


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
    all_stops = []
    for pu in load_data.get("pickups", []):
        addr = pu.get("address_line1", "") + ", " + pu.get("address_line2", "")
        all_stops.append(addr)
    for do_ in load_data.get("deliveries", []):
        addr = do_.get("address_line1", "") + ", " + do_.get("address_line2", "")
        all_stops.append(addr)
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
        final_message = build_message(load_data, empty_miles, loaded_miles)
        await update.message.reply_text(final_message, parse_mode="Markdown")
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
