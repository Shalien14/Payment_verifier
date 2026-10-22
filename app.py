"""BuildStart payment-verification MVP: upload files and record metadata."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE_PATH = DATA_DIR / "payments.db"
STATIC_DIR = BASE_DIR / "static"

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
BUSINESS_ACCOUNT_ENDING = "0028"


def add_column_if_missing(connection: sqlite3.Connection, name: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(payment_slips)")}
    if name not in columns:
        connection.execute(f"ALTER TABLE payment_slips ADD COLUMN {name} {definition}")


def _preprocess_ocr_images(image):
    """Create a small set of OCR-friendly variants without changing the saved upload."""
    from PIL import ImageEnhance, ImageFilter, ImageOps

    rgb = image.convert("RGB")
    variants = [("original", rgb)]

    gray = ImageOps.grayscale(rgb)
    gray = ImageOps.autocontrast(gray)
    # Small text in screenshots is often the main reason Tesseract misses fields.
    if max(gray.size) < 1800:
        scale = min(2.0, 1800 / max(gray.size))
        gray = gray.resize((max(1, int(gray.width * scale)),
                            max(1, int(gray.height * scale))))
    variants.append(("grayscale_autocontrast", gray))

    sharpened = gray.filter(ImageFilter.SHARPEN)
    variants.append(("sharpened", sharpened))

    # Thresholding can help clean scans, but can hurt coloured/anti-aliased text,
    # so keep it as an alternative rather than the only OCR input.
    threshold = gray.point(lambda pixel: 255 if pixel > 165 else 0)
    variants.append(("threshold", threshold))
    return variants


def _ocr_text_and_confidence(pytesseract, image, psm: int) -> tuple[str, float | None]:
    """Return OCR text and mean confidence for recognized non-empty words."""
    from pytesseract import Output

    config = f"--oem 3 --psm {psm}"
    data = pytesseract.image_to_data(image, config=config, output_type=Output.DICT)
    words = []
    confidences = []
    for word, confidence in zip(data.get("text", []), data.get("conf", [])):
        word = word.strip()
        if not word:
            continue
        words.append(word)
        try:
            score = float(confidence)
            if score >= 0:
                confidences.append(score)
        except (TypeError, ValueError):
            continue
    # Rebuild lines from image_to_data so text and confidence come from one OCR pass.
    line_parts = {}
    for index, word in enumerate(data.get("text", [])):
        word = word.strip()
        if not word:
            continue
        key = (
            data.get("block_num", [0] * len(data["text"]))[index],
            data.get("par_num", [0] * len(data["text"]))[index],
            data.get("line_num", [0] * len(data["text"]))[index],
        )
        line_parts.setdefault(key, []).append(word)
    text = "\\n".join(" ".join(parts) for parts in line_parts.values())
    mean_confidence = sum(confidences) / len(confidences) if confidences else None
    return text, mean_confidence


def _empty_ocr(status: str, message: str) -> dict:
    return {
        "status": status,
        "raw_text": None,
        "account_number": None,
        "amount": None,
        "confidence": None,
        "transaction_id": None,
        "transaction_date": None,
        "transfer_status": None,
        "beneficiary_bank": None,
        "beneficiary_account": None,
        "message": message,
    }


def _extract_pdf_text(path: Path) -> str:
    """Extract embedded PDF text. Returns an empty string for image-only PDFs."""
    import fitz  # PyMuPDF

    pieces = []
    with fitz.open(path) as document:
        if document.page_count > 20:
            raise ValueError("PDF has too many pages (maximum 20).")
        for page in document:
            pieces.append(page.get_text("text"))
    return "\n".join(pieces).strip()


def _ocr_image_file(image_path: Path) -> dict:
    from PIL import Image, UnidentifiedImageError
    import pytesseract

    with Image.open(image_path) as image:
        variants = _preprocess_ocr_images(image)
        passes = []
        for variant_name, variant in variants:
            for psm in (6, 11):
                text, confidence = _ocr_text_and_confidence(pytesseract, variant, psm)
                fields = parse_receipt_details(text)
                passes.append({
                    "variant": variant_name,
                    "psm": psm,
                    "raw_text": text,
                    "confidence": confidence,
                    **fields,
                })

    candidates = [
        item for item in passes
        if item["beneficiary_account"] or item["amount"] or item["transaction_id"]
    ]
    if not candidates:
        best = max(passes, key=lambda item: item["confidence"] if item["confidence"] is not None else -1)
        return {
            **_empty_ocr("completed", "OCR ran, but required payment fields were not found. Manual review is required."),
            "raw_text": best["raw_text"],
            "confidence": best["confidence"],
        }

    best = max(candidates, key=lambda item: item["confidence"] if item["confidence"] is not None else -1)
    conflicts = {}
    for field in ("beneficiary_account", "amount", "transaction_id", "transfer_status"):
        values = {item.get(field) for item in candidates if item.get(field)}
        conflicts[field] = len(values) > 1

    result = {
        **_empty_ocr("completed", "OCR completed. Confirm the extracted values before making a payment decision."),
        "raw_text": best["raw_text"],
        "confidence": round(best["confidence"], 2) if best["confidence"] is not None else None,
    }
    for field in ("beneficiary_account", "amount", "transaction_id", "transaction_date",
                  "transfer_status", "beneficiary_bank"):
        result[field] = None if conflicts.get(field, False) else best.get(field)
    result["account_number"] = result["beneficiary_account"]
    if any(conflicts.values()):
        result["message"] = "OCR passes disagree about one or more fields. Manual review is required."
    return result


def extract_payment_details(file_path: Path) -> dict:
    """Extract receipt details from images or PDFs, preferring embedded PDF text."""
    try:
        if file_path.suffix.lower() == ".pdf":
            try:
                text = _extract_pdf_text(file_path)
            except ImportError:
                return _empty_ocr("unavailable", "PDF support requires PyMuPDF. Run pip install -r requirements.txt.")

            if text:
                fields = parse_receipt_details(text)
                found = bool(fields["amount"] or fields["beneficiary_account"] or fields["transaction_id"])
                return {
                    **_empty_ocr(
                        "completed" if found else "failed",
                        "PDF text extracted. Confirm the details before making a payment decision."
                        if found else
                        "No usable text was found in this PDF. OCR fallback is required for scanned PDFs.",
                    ),
                    "raw_text": text,
                    **fields,
                    "account_number": fields["beneficiary_account"],
                    "confidence": None,
                }
            # Scanned PDFs have no embedded text. Render the first page and use
            # the same OCR pipeline as image uploads.
            try:
                import fitz
                import tempfile
                with fitz.open(file_path) as document:
                    if document.page_count == 0:
                        return _empty_ocr("failed", "The PDF contains no pages.")
                    page = document.load_page(0)
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    with tempfile.TemporaryDirectory() as temp_dir:
                        rendered_path = Path(temp_dir) / "page-1.png"
                        pixmap.save(str(rendered_path))
                        result = _ocr_image_file(rendered_path)
                        result["message"] = (
                            "No embedded PDF text was found; OCR was run on the first page. "
                            + result.get("message", "")
                        )
                        return result
            except ImportError:
                return _empty_ocr("unavailable", "Scanned PDF OCR requires PyMuPDF and Tesseract.")

        try:
            return _ocr_image_file(file_path)
        except ImportError:
            return _empty_ocr("unavailable", "OCR Python packages are not installed. Run pip install -r requirements.txt.")
        except Exception as error:
            # Tesseract missing is reported clearly, while avoiding exposing internals to users.
            try:
                import pytesseract
                if isinstance(error, pytesseract.TesseractNotFoundError):
                    return _empty_ocr("unavailable", "Tesseract is not installed. Install it, then upload again.")
            except ImportError:
                pass
            from PIL import UnidentifiedImageError
            if isinstance(error, (UnidentifiedImageError, OSError)):
                return _empty_ocr("failed", "Could not read this image. Try a valid JPG, PNG, or WebP file.")
            raise
    except ValueError as error:
        return _empty_ocr("failed", str(error))
    except Exception:
        return _empty_ocr("failed", "The file could not be processed. Try another file or request manual review.")


def parse_receipt_details(raw_text: str) -> dict:
    """Extract common payment fields, preferring the beneficiary/recipient account.

    The parser uses labels and section context. It intentionally avoids guessing
    currency for ambiguous 'Rs.' values and doesn't treat the sender account as
    the beneficiary when both are present.
    """
    raw_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    # Some bank PDFs put each label and its value on separate lines, with the
    # colon starting the value line. Join those pairs before parsing.
    lines = []
    index = 0
    while index < len(raw_lines):
        current = raw_lines[index]
        if index + 1 < len(raw_lines) and re.match(r"^\s*:\s*", raw_lines[index + 1]):
            value_part = re.sub(r"^\s*:\s*", "", raw_lines[index + 1]).strip()
            lines.append(f"{current} : {value_part}")
            index += 2
        else:
            lines.append(current)
            index += 1
    section = ""
    beneficiary_accounts = []
    generic_accounts = []
    for line in lines:
        if re.search(r"\bbeneficiary\b|\brecipient\b|\bpayee\b", line, re.IGNORECASE):
            section = "beneficiary"
        elif re.search(r"\bsender\b|\bremitter\b", line, re.IGNORECASE):
            section = "sender"

        account_match = re.search(
            r"\b(?:account(?:\s*(?:no\.?|number))?|a/c\s*(?:no\.?|number)?)\s*[:#=-]?\s*"
            r"([A-Z0-9*X][A-Z0-9*X -]{2,}[A-Z0-9*X])",
            line, re.IGNORECASE
        )
        if account_match:
            candidate = re.sub(r"[\s-]", "", account_match.group(1)).upper()
            candidate = re.split(r"(?:TRANSACTION|REFERENCE|AMOUNT|DATE|TIME|BALANCE|STATUS)", candidate)[0]
            if len(candidate) >= 4 and re.search(r"[0-9*X]", candidate):
                if section == "beneficiary":
                    beneficiary_accounts.append(candidate)
                elif section != "sender":
                    generic_accounts.append(candidate)

    beneficiary_account = beneficiary_accounts[0] if beneficiary_accounts else (
        generic_accounts[0] if generic_accounts else None
    )

    currency_aliases = {
        "LKR": "LKR", "INR": "INR", "GBP": "GBP", "USD": "USD",
        "EUR": "EUR", "AUD": "AUD", "CAD": "CAD", "₹": "INR",
        "$": "USD", "£": "GBP", "€": "EUR",
    }
    amount = None
    amount_pattern = re.compile(
        r"\b(?:transaction\s+amount|total\s+amount|amount(?:\s+paid)?|paid\s+amount|"
        r"payment(?:\s+amount)?|credited|debited|sent|received)\b"
        r"\s*[:=#-]?\s*"
        r"(?P<currency>LKR|INR|GBP|USD|EUR|AUD|CAD|Rs\.?|₹|\$|£|€)?\s*"
        r"(?P<value>\d[\d,]*(?:\.\d{1,2})?)",
        re.IGNORECASE,
    )
    for line in lines:
        match = amount_pattern.search(line)
        if not match:
            continue
        raw_currency = (match.group("currency") or "").strip()
        value = match.group("value").replace(",", "")
        try:
            decimal_value = Decimal(value)
            if not decimal_value.is_finite() or decimal_value < 0:
                continue
            if "." not in value and not raw_currency:
                continue
        except InvalidOperation:
            continue
        currency_key = raw_currency.upper()
        if currency_key in {"RS", "RS."} or not currency_key:
            currency = "UNKNOWN"
        else:
            currency = currency_aliases.get(currency_key, "UNKNOWN")
        amount = f"{currency} {decimal_value:.2f}"
        break

    def find_label(pattern: str) -> str | None:
        match = re.search(pattern, raw_text, re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else None

    transaction_id = find_label(
        r"^\s*(?:transaction\s*(?:id|no\.?|number|reference)|reference\s*(?:id|no\.?|number))\s*[:#=-]\s*([A-Z0-9-]+)\s*$"
    )
    transaction_date = find_label(
        r"^\s*(?:transaction\s+date(?:\s*&\s*time)?|date(?:\s*&\s*time)?)\s*[:#=-]\s*(.+?)\s*$"
    )
    status_match = re.search(
        r"\b(?:transaction|transfer|payment)\s+status\s*[:#=-]?\s*(SUCCESS|SUCCESSFUL|COMPLETED|FAILED|PENDING)\b",
        raw_text, re.IGNORECASE
    )
    transfer_status = status_match.group(1).upper() if status_match else None

    bank_match = re.search(
        r"^\s*beneficiary\s+bank\s*[:#=-]\s*(.+?)\s*$",
        raw_text, re.IGNORECASE | re.MULTILINE
    )
    beneficiary_bank = bank_match.group(1).strip() if bank_match else None

    return {
        "beneficiary_account": beneficiary_account,
        "amount": amount,
        "transaction_id": transaction_id,
        "transaction_date": transaction_date,
        "transfer_status": transfer_status,
        "beneficiary_bank": beneficiary_bank,
    }


def parse_payment_details(raw_text: str) -> tuple[str | None, str | None]:
    """Backward-compatible helper returning recipient account and amount."""
    fields = parse_receipt_details(raw_text)
    return fields["beneficiary_account"], fields["amount"]


def decide_payment(order: sqlite3.Row, ocr: dict) -> tuple[str, str]:
    """Make a transparent MVP decision; incomplete OCR always needs human review."""
    if ocr["status"] != "completed":
        return "NEEDS_REVIEW", "OCR did not complete, so the payment cannot be verified."
    if not ocr["account_number"] or not ocr["amount"]:
        return "NEEDS_REVIEW", "The beneficiary account or payment amount could not be read."
    transfer_status = (ocr.get("transfer_status") or "").upper()
    if transfer_status in {"FAILED"}:
        return "REJECTED", "The receipt indicates that the transfer failed."
    if transfer_status in {"PENDING"}:
        return "NEEDS_REVIEW", "The receipt indicates a pending transfer; confirm its final status independently."
    if not ocr["account_number"].endswith(BUSINESS_ACCOUNT_ENDING):
        return "REJECTED", f"The payment was not sent to the business account ending {BUSINESS_ACCOUNT_ENDING}."

    try:
        found_currency, found_value = ocr["amount"].rsplit(" ", 1)
        if found_currency == "UNKNOWN":
            return "NEEDS_REVIEW", "The currency could not be identified safely from the slip."
        amounts_match = Decimal(found_value) == Decimal(order["expected_amount"])
    except (ValueError, InvalidOperation):
        return "NEEDS_REVIEW", "The OCR amount could not be compared safely."
    if found_currency != order["currency"] or not amounts_match:
        return "REJECTED", f"Expected {order['currency']} {order['expected_amount']}; found {ocr['amount']}."
    return (
        "NEEDS_REVIEW",
        "The extracted account, currency, and amount match the order, but a screenshot cannot prove that funds were received. Confirm the transaction using a trusted bank or payment-provider record before marking the order paid.",
    )


def initialise_database() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_slips (
                id TEXT PRIMARY KEY,
                original_filename TEXT NOT NULL,
                stored_filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """
        )
        add_column_if_missing(connection, "ocr_status", "TEXT")
        add_column_if_missing(connection, "ocr_raw_text", "TEXT")
        add_column_if_missing(connection, "extracted_account_number", "TEXT")
        add_column_if_missing(connection, "extracted_amount", "TEXT")
        add_column_if_missing(connection, "extracted_transaction_id", "TEXT")
        add_column_if_missing(connection, "extracted_transaction_date", "TEXT")
        add_column_if_missing(connection, "extracted_transfer_status", "TEXT")
        add_column_if_missing(connection, "extracted_beneficiary_bank", "TEXT")
        add_column_if_missing(connection, "order_id", "TEXT")
        add_column_if_missing(connection, "decision", "TEXT")
        add_column_if_missing(connection, "decision_reason", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                expected_amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'AWAITING_PAYMENT'
            )
            """
        )
        # Demo orders for the challenge. Later these can come from a real order system.
        connection.executemany(
            """
            INSERT OR IGNORE INTO orders (order_id, customer_name, expected_amount, currency)
            VALUES (?, ?, ?, ?)
            """,
            [("ORD-1001", "Mark & Spencer", "10.00", "GBP"),
             ("ORD-1002", "Ava Patel", "250.00", "GBP"),
             ("ORD-1003", "Jordan Lee", "1250.00", "INR")],
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialise_database()
    yield


app = FastAPI(title="BuildStart Payment Verification", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/orders/{order_id}")
async def get_order(order_id: str) -> dict:
    normalised_id = order_id.strip().upper()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        order = connection.execute("SELECT * FROM orders WHERE order_id = ?", (normalised_id,)).fetchone()
    if not order:
        raise HTTPException(404, "Order not found. Check the Order ID and try again.")
    return {
        "order_id": order["order_id"], "customer_name": order["customer_name"],
        "expected_amount": order["expected_amount"], "currency": order["currency"],
        "status": order["status"], "business_account_ending": BUSINESS_ACCOUNT_ENDING,
    }


@app.post("/api/payment-slips", status_code=201)
async def upload_payment_slip(order_id: str = Form(...), file: UploadFile = File(...)) -> dict:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "Upload a JPG, PNG, WebP image, or PDF.")
    normalised_id = order_id.strip().upper()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        order = connection.execute("SELECT * FROM orders WHERE order_id = ?", (normalised_id,)).fetchone()
    if not order:
        raise HTTPException(404, "Order not found. Find an order before uploading a payment slip.")
    if order["status"] == "PAID":
        raise HTTPException(409, "This order has already been paid and cannot accept another slip.")

    suffix = Path(file.filename or "upload").suffix.lower()
    upload_id = str(uuid4())
    stored_filename = f"{upload_id}{suffix}"
    destination = UPLOAD_DIR / stored_filename

    with destination.open("wb") as output:
        shutil.copyfileobj(file.file, output)

    size_bytes = destination.stat().st_size
    if size_bytes == 0 or size_bytes > MAX_FILE_SIZE:
        destination.unlink(missing_ok=True)
        detail = "The uploaded file is empty." if size_bytes == 0 else "Files must be 10 MB or smaller."
        raise HTTPException(413, detail)

    uploaded_at = datetime.now(timezone.utc).isoformat()
    ocr = extract_payment_details(destination)
    decision, decision_reason = decide_payment(order, ocr)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO payment_slips
                (id, original_filename, stored_filename, content_type, size_bytes, uploaded_at,
                 ocr_status, ocr_raw_text, extracted_account_number, extracted_amount,
                 extracted_transaction_id, extracted_transaction_date, extracted_transfer_status,
                 extracted_beneficiary_bank, order_id, decision, decision_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (upload_id, file.filename or "unnamed-upload", stored_filename, file.content_type, size_bytes, uploaded_at,
             ocr["status"], ocr["raw_text"], ocr["account_number"], ocr["amount"],
             ocr.get("transaction_id"), ocr.get("transaction_date"), ocr.get("transfer_status"),
             ocr.get("beneficiary_bank"), normalised_id, decision, decision_reason),
        )
        if decision == "APPROVED":
            connection.execute("UPDATE orders SET status = 'PAID' WHERE order_id = ?", (normalised_id,))

    return {
        "id": upload_id,
        "filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": size_bytes,
        "uploaded_at": uploaded_at,
        "message": "Payment slip saved successfully.",
        "ocr": ocr,
        "order": {"order_id": order["order_id"], "customer_name": order["customer_name"],
                  "expected_amount": order["expected_amount"], "currency": order["currency"]},
        "decision": decision,
        "decision_reason": decision_reason,
    }


@app.get("/api/payment-slips")
async def list_payment_slips() -> list[dict]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, order_id, original_filename, content_type, size_bytes, uploaded_at,
                   ocr_status, extracted_account_number, extracted_amount, extracted_transaction_id,
                   extracted_transaction_date, extracted_transfer_status, extracted_beneficiary_bank,
                   decision, decision_reason
            FROM payment_slips ORDER BY uploaded_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
