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


def extract_payment_details(image_path: Path) -> dict:
    """Run local OCR with a few receipt-friendly image treatments.

    OCR engines are sensitive to small text, low contrast, and the varied layouts used
    by banking apps.  We read a small set of conservative variants and merge their
    output.  The decision logic below still requires every expected field to match.
    """
    try:
        from PIL import Image, ImageEnhance, ImageOps, UnidentifiedImageError
        import pytesseract
        with Image.open(image_path) as image:
            original = image.convert("RGB")
            grayscale = ImageOps.grayscale(original)
            enlarged = grayscale.resize(
                (grayscale.width * 2, grayscale.height * 2), Image.Resampling.LANCZOS
            )
            enhanced = ImageEnhance.Contrast(enlarged).enhance(1.8)
            thresholded = enhanced.point(lambda pixel: 255 if pixel > 175 else 0)
            variants = (original, enhanced, thresholded)
            raw_text = "\n".join(
                pytesseract.image_to_string(variant, config="--psm 6")
                for variant in variants
            )
    except ImportError:
        return {"status": "unavailable", "raw_text": None, "account_number": None, "amount": None, "transfer_status": None,
                "message": "OCR Python packages are not installed yet. Run pip install -r requirements.txt."}
    except pytesseract.TesseractNotFoundError:
        return {"status": "unavailable", "raw_text": None, "account_number": None, "amount": None, "transfer_status": None,
                "message": "OCR is not installed yet. Install Tesseract, then upload again."}
    except (UnidentifiedImageError, OSError) as error:
        return {"status": "failed", "raw_text": None, "account_number": None, "amount": None, "transfer_status": None,
                "message": f"Could not read this image: {error}"}

    account_number, amount, transfer_status = parse_payment_details(raw_text)
    return {"status": "completed", "raw_text": raw_text, "account_number": account_number, "amount": amount, "transfer_status": transfer_status,
            "message": "OCR completed. Confirm these values before approving a payment."}


def parse_payment_details(raw_text: str) -> tuple[str | None, str | None, str | None]:
    """Extract a labelled account number and amount from OCR text.

    Payment slips often intentionally mask accounts (for example, XXXXXXXXXXXX0028),
    so an account value is allowed to contain letters as well as digits.
    """
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    account_number = None
    account_pattern = re.compile(
        r"\b(?:account|a\s*/?\s*c)\s*(?:no\.?|number)?\s*[:#=-]\s*"
        r"([A-Z0-9*X][A-Z0-9*X -]{2,}[A-Z0-9*X])",
        re.IGNORECASE,
    )
    beneficiary_accounts: list[str] = []
    other_accounts: list[str] = []
    in_beneficiary_section = False
    for line in lines:
        if re.search(r"\b(?:beneficiary|recipient|payee)\b", line, re.IGNORECASE):
            in_beneficiary_section = True
        match = account_pattern.search(line)
        if match:
            candidate = re.sub(r"[\s-]", "", match.group(1)).upper()
            # Headings such as "Sender A/C Details" are not account values.
            if len(candidate) >= 4 and re.search(r"[0-9*X]", candidate):
                (beneficiary_accounts if in_beneficiary_section else other_accounts).append(candidate)
    if beneficiary_accounts:
        account_number = beneficiary_accounts[0]
    elif other_accounts:
        account_number = other_accounts[0]

    currency_aliases = {
        "RS": "INR", "RS.": "INR", "₹": "INR", "$": "USD", "£": "GBP", "€": "EUR",
    }
    amount = None
    amount_pattern = re.compile(
        r"\b(?:total\s+)?(?:amount(?:\s+paid)?|paid\s+amount|payment(?:\s+amount)?|"
        r"credited|debited|sent|received)\b"
        r"[^\d₹$£€A-Z]{0,20}"
        r"(?:(LKR|INR|GBP|USD|EUR|AUD|CAD|RS\.?|₹|\$|£|€)\s*)?"
        r"([\d,]+(?:\.\d{1,2})?)",
        re.IGNORECASE,
    )
    for line in lines:
        match = amount_pattern.search(line)
        if not match:
            continue
        currency = (match.group(1) or "").upper()
        currency = currency_aliases.get(currency, currency)
        value = match.group(2).replace(",", "")
        try:
            # Do not treat IDs, dates, or a bare integer as a payment amount.
            if Decimal(value) < 0 or ("." not in value and not currency):
                continue
        except InvalidOperation:
            continue
        amount = f"{currency} {value}".strip()
        break

    status_match = re.search(
        r"\b(?:transaction|transfer|payment)\s*status\s*[:#=-]?\s*(SUCCESS|COMPLETED|FAILED|PENDING)\b",
        raw_text,
        re.IGNORECASE,
    )
    transfer_status = status_match.group(1).upper() if status_match else None
    return account_number, amount, transfer_status


def decide_payment(order: sqlite3.Row, ocr: dict) -> tuple[str, str]:
    """Make a transparent MVP decision; incomplete OCR always needs human review."""
    if ocr["status"] != "completed":
        return "NEEDS_REVIEW", "OCR did not complete, so the payment cannot be verified."
    if not ocr["account_number"] or not ocr["amount"]:
        return "NEEDS_REVIEW", "The account number or payment amount could not be read."
    if ocr["transfer_status"] and ocr["transfer_status"] not in {"SUCCESS", "COMPLETED"}:
        return "REJECTED", f"The receipt shows a {ocr['transfer_status'].lower()} transfer, not a completed payment."
    if not ocr["account_number"].endswith(BUSINESS_ACCOUNT_ENDING):
        return "REJECTED", f"The payment was not sent to the business account ending {BUSINESS_ACCOUNT_ENDING}."

    try:
        found_currency, found_value = ocr["amount"].rsplit(" ", 1)
        amounts_match = Decimal(found_value) == Decimal(order["expected_amount"])
    except (ValueError, InvalidOperation):
        return "NEEDS_REVIEW", "The OCR amount could not be compared safely."
    if found_currency != order["currency"] or not amounts_match:
        return "REJECTED", f"Expected {order['currency']} {order['expected_amount']}; found {ocr['amount']}."
    return "APPROVED", "The business account, currency, and amount match the order."


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
        add_column_if_missing(connection, "extracted_transfer_status", "TEXT")
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
        ocr = {"status": "not_supported", "raw_text": None, "account_number": None, "amount": None, "transfer_status": None,
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
                 extracted_transfer_status, order_id, decision, decision_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (upload_id, file.filename or "unnamed-upload", stored_filename, file.content_type, size_bytes, uploaded_at,
             ocr["status"], ocr["raw_text"], ocr["account_number"], ocr["amount"], ocr["transfer_status"], normalised_id,
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
                   ocr_status, extracted_account_number, extracted_amount, extracted_transfer_status, decision, decision_reason
            FROM payment_slips ORDER BY uploaded_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
