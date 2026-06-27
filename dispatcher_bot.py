all_stops = [pu["address"] for pu in load_data.get("pickups", [])]
    all_stops += [do["address"] for do in load_data.get("deliveries", [])]
    if not all_stops:
        return 0, 0
    empty = await get_distance_miles(current_location, all_stops[0])
    loaded = 0
    for i in range(len(all_stops) - 1):
        loaded += await get_distance_miles(all_stops[i], all_stops[i + 1])
    return empty, loaded


async def extract_load_data(file_bytes: bytes, filename: str, caption: str = "") -> dict:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = get_mime(filename)
    content = []

    if mime.startswith("image/"):
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encode_b64(file_bytes)}},
            {"type": "text", "text": "Extract all load information from this rate confirmation."},
        ]
    elif mime == "application/pdf" or ext == "pdf":
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encode_b64(file_bytes)}},
            {"type": "text", "text": "Extract all load information from this rate confirmation PDF."},
        ]
    elif ext in ("docx", "doc"):
        try:
            import docx as _docx
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(file_bytes); tmp_path = tmp.name
            doc = _docx.Document(tmp_path); os.unlink(tmp_path)
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as e:
            text = f"[Could not parse: {e}]"
        content = [{"type": "text", "text": f"Extract all load information:\n{text}"}]
    elif ext in ("xlsx", "xls", "csv"):
        try:
            if ext == "csv":
                text = file_bytes.decode("utf-8", errors="replace")[:8000]
            else:
                import openpyxl
                with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                    tmp.write(file_bytes); tmp_path = tmp.name
                wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True); os.unlink(tmp_path)
                rows = []
                for sheet in wb.worksheets:
                    rows.append(f"=== {sheet.title} ===")
                    for row in sheet.iter_rows(values_only=True):
                        rows.append("\t".join(str(c) if c is not None else "" for c in row))
                text = "\n".join(rows)[:8000]
        except Exception as e:
            text = f"[Could not parse: {e}]"
        content = [{"type": "text", "text": f"Extract all load information:\n{text}"}]
    else:
        text = file_bytes.decode("utf-8", errors="replace")[:8000]
        content = [{"type": "text", "text": f"Extract all load information:\n{text}"}]

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=EXTRACT_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = re.sub(r"^```json\s*", "", resp.content[0].text.strip())
    raw = re.sub(r"```$", "", raw).strip()
    return json.loads(raw)


async def format_message(load_data: dict, empty_miles: int, loaded_miles: int) -> str:
    prompt = f"Format this load data:\n{json.dumps(load_data, indent=2)}\n\nEmpty miles: {empty_miles}\nLoaded miles: {loaded_miles}\n\nOutput ONLY the formatted dispatcher message."
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=FORMAT_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Dispatcher Bot ready!\n\n"
        "Send me a load confirmation (photo, PDF, Word, Excel) and I'll format it.\n"
        "I'll also ask your current location to calculate empty & loaded miles.\n\n"
        "/help for more info."
    )
    return ConversationHandler.END

async def
