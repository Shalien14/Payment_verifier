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


def extract_payment_details(image_path: Path) -> dict:
    """Run several conservative local OCR passes and flag conflicting extractions."""
    try:
        from PIL import Image, UnidentifiedImageError
        import pytesseract

        with Image.open(image_path) as image:
            variants = _preprocess_ocr_images(image)
            passes = []
            for variant_name, variant in variants:
                for psm in (6, 11):
                    text, confidence = _ocr_text_and_confidence(pytesseract, variant, psm)
                    account_number, amount = parse_payment_details(text)
                    passes.append({
                        "variant": variant_name,
                        "psm": psm,
                        "raw_text": text,
                        "confidence": confidence,
                        "account_number": account_number,
                        "amount": amount,
                    })
    except ImportError:
        return {
            "status": "unavailable", "raw_text": None, "account_number": None, "amount": None,
            "confidence": None,
            "message": "OCR Python packages are not installed yet. Run pip install -r requirements.txt.",
        }
    except pytesseract.TesseractNotFoundError:
        return {
            "status": "unavailable", "raw_text": None, "account_number": None, "amount": None,
            "confidence": None,
            "message": "OCR is not installed yet. Install Tesseract, then upload again.",
        }
    except (UnidentifiedImageError, OSError) as error:
        return {
            "status": "failed", "raw_text": None, "account_number": None, "amount": None,
            "confidence": None,
            "message": f"Could not read this image: {error}",
        }

    # Prefer a pass that found both fields, then confidence. If different OCR
    # passes disagree about a field, do not silently pick the most convenient one.
    complete = [item for item in passes if item["account_number"] and item["amount"]]
    candidates = complete or [item for item in passes if item["account_number"] or item["amount"]]
    if not candidates:
        best = max(passes, key=lambda item: item["confidence"] if item["confidence"] is not None else -1)
        return {
            "status": "completed", "raw_text": best["raw_text"],
            "account_number": None, "amount": None, "confidence": best["confidence"],
            "message": "OCR ran, but required payment fields were not found. Manual review is required.",
        }

    best = max(candidates, key=lambda item: item["confidence"] if item["confidence"] is not None else -1)
    account_values = {item["account_number"] for item in candidates if item["account_number"]}
    amount_values = {item["amount"] for item in candidates if item["amount"]}
    account_number = best["account_number"] if len(account_values) <= 1 else None
    amount = best["amount"] if len(amount_values) <= 1 else None
    conflict = len(account_values) > 1 or len(amount_values) > 1

    return {
        "status": "completed",
        "raw_text": best["raw_text"],
        "account_number": account_number,
        "amount": amount,
        "confidence": round(best["confidence"], 2) if best["confidence"] is not None else None,
        "message": (
            "OCR passes disagree about one or more fields. Manual review is required."
            if conflict else
            "OCR completed. Confirm extracted values before approving a payment."
        ),
    }


def parse_payment_details(raw_text: str) -> tuple[str | None, str | None]:
    """Extract labelled account and amount values without guessing ambiguous currencies.

    This parser intentionally prefers missing data over selecting an unlabelled
    balance or silently treating an ambiguous 'Rs.' symbol as a specific currency.
    """
    account_patterns = (
        r"\baccount\s*(?:no\.?|number|#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9 -]*[A-Z0-9])",
        r"\b(?:beneficiary|recipient)\s+account\s*[:#-]?\s*([A-Z0-9][A-Z0-9 -]*[A-Z0-9])",
    )
    account_number = None
    for line in raw_text.splitlines():
        for pattern in account_patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                candidate_text = match.group(1)
                # If several labelled fields share a line, stop before the next label.
                candidate_text = re.split(
                    r"\s+(?:transaction|reference|amount|date|time|balance|status)\b",
                    candidate_text,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0]
                candidate = re.sub(r"[ -]", "", candidate_text).upper()
                if len(candidate) >= 4:
                    account_number = candidate
                    break
        if account_number:
            break

    amount_pattern = re.compile(
        r"\b(?:total\s+)?(?:amount|amount\s+paid|paid|credited|debited|transfer\s+amount)\b"
        r"\s*[:=\-]?\s*"
        r"(?P<currency>lkr|sri\s*lankan\s*rupees|inr|gbp|usd|eur|aud|cad|rs\.?|₹|\$|£|€)?\s*"
        r"(?P<value>\d[\d,]*(?:\.\d{1,2})?)",
        re.IGNORECASE,
    )
    amount_match = amount_pattern.search(raw_text)
    amount = None
    if amount_match:
        raw_currency = (amount_match.group("currency") or "").strip().lower()
        value_text = amount_match.group("value").replace(",", "")
        try:
            value = Decimal(value_text)
            if value < 0 or not value.is_finite():
                return account_number, None
        except InvalidOperation:
            return account_number, None

        currency_map = {
            "lkr": "LKR",
            "sri lankan rupees": "LKR",
            "inr": "INR",
            "gbp": "GBP",
            "usd": "USD",
            "eur": "EUR",
            "aud": "AUD",
            "cad": "CAD",
            "₹": "INR",
            "$": "USD",
            "£": "GBP",
            "€": "EUR",
        }
        # "Rs." alone is ambiguous across countries; it must not be assumed to mean INR.
        currency = "UNKNOWN" if raw_currency in {"rs", "rs."} or not raw_currency else currency_map.get(raw_currency, "UNKNOWN")
        amount = f"{currency} {value:.2f}"
    return account_number, amount


def decide_payment(order: sqlite3.Row, ocr: dict) -> tuple[str, str]:
    """Make a transparent MVP decision; incomplete OCR always needs human review."""
    if ocr["status"] != "completed":
        return "NEEDS_REVIEW", "OCR did not complete, so the payment cannot be verified."
    if not ocr["account_number"] or not ocr["amount"]:
        return "NEEDS_REVIEW", "The account number or payment amount could not be read."
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
    if file.content_type == "application/pdf":
        ocr = {"status": "not_supported", "raw_text": None, "account_number": None, "amount": None,
               "message": "PDF saved. OCR currently supports image uploads only."}
    else:
        ocr = extract_payment_details(destination)
    decision, decision_reason = decide_payment(order, ocr)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO payment_slips
                (id, original_filename, stored_filename, content_type, size_bytes, uploaded_at,
                 ocr_status, ocr_raw_text, extracted_account_number, extracted_amount,
                 order_id, decision, decision_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (upload_id, file.filename or "unnamed-upload", stored_filename, file.content_type, size_bytes, uploaded_at,
             ocr["status"], ocr["raw_text"], ocr["account_number"], ocr["amount"], normalised_id,
             decision, decision_reason),
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
                   ocr_status, extracted_account_number, extracted_amount, decision, decision_reason
            FROM payment_slips ORDER BY uploaded_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
