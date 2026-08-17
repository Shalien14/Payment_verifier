"""BuildStart payment-verification MVP: upload files and record metadata."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import sqlite3
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE_PATH = DATA_DIR / "payments.db"
STATIC_DIR = BASE_DIR / "static"

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


def add_column_if_missing(connection: sqlite3.Connection, name: str, definition: str) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(payment_slips)")}
    if name not in columns:
        connection.execute(f"ALTER TABLE payment_slips ADD COLUMN {name} {definition}")


def extract_payment_details(image_path: Path) -> dict:
    """Run local OCR and make conservative first-pass extractions from its text."""
    try:
        from PIL import Image, UnidentifiedImageError
        import pytesseract
        with Image.open(image_path) as image:
            raw_text = pytesseract.image_to_string(image.convert("RGB"))
    except ImportError:
        return {"status": "unavailable", "raw_text": None, "account_number": None, "amount": None,
                "message": "OCR Python packages are not installed yet. Run pip install -r requirements.txt."}
    except pytesseract.TesseractNotFoundError:
        return {"status": "unavailable", "raw_text": None, "account_number": None, "amount": None,
                "message": "OCR is not installed yet. Install Tesseract, then upload again."}
    except (UnidentifiedImageError, OSError) as error:
        return {"status": "failed", "raw_text": None, "account_number": None, "amount": None,
                "message": f"Could not read this image: {error}"}

    account_number, amount = parse_payment_details(raw_text)
    return {"status": "completed", "raw_text": raw_text, "account_number": account_number, "amount": amount,
            "message": "OCR completed. Confirm these values before approving a payment."}


def parse_payment_details(raw_text: str) -> tuple[str | None, str | None]:
    """Extract a labelled account number and amount from Tesseract text.

    Payment slips often intentionally mask accounts (for example, XXXXXXXXXXXX0028),
    so an account value is allowed to contain letters as well as digits.
    """
    account_match = re.search(
        r"\baccount\s*(?:no\.?|number)\s*[:#-]?\s*([A-Z0-9][A-Z0-9 -]*[A-Z0-9])",
        raw_text,
        re.IGNORECASE,
    )
    account_number = re.sub(r"[ -]", "", account_match.group(1)) if account_match else None

    amount_match = re.search(
        r"\b(?:amount|paid|credited|debited)\s*[:=-]?\s*"
        r"(?:(rs\.?|inr|gbp|usd|eur|aud|cad|₹|\$|£|€)\s*)?"
        r"([\d,]+(?:\.\d{1,2})?)",
        raw_text,
        re.IGNORECASE,
    )
    if amount_match:
        currency = (amount_match.group(1) or "").upper().replace("RS.", "INR")
        value = amount_match.group(2).replace(",", "")
        amount = f"{currency} {value}".strip()
    else:
        amount = None
    return account_number, amount


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


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialise_database()
    yield


app = FastAPI(title="BuildStart Payment Verification", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/payment-slips", status_code=201)
async def upload_payment_slip(file: UploadFile = File(...)) -> dict:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(415, "Upload a JPG, PNG, WebP image, or PDF.")

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
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO payment_slips
                (id, original_filename, stored_filename, content_type, size_bytes, uploaded_at,
                 ocr_status, ocr_raw_text, extracted_account_number, extracted_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (upload_id, file.filename or "unnamed-upload", stored_filename, file.content_type, size_bytes, uploaded_at,
             ocr["status"], ocr["raw_text"], ocr["account_number"], ocr["amount"]),
        )

    return {
        "id": upload_id,
        "filename": file.filename,
        "content_type": file.content_type,
        "size_bytes": size_bytes,
        "uploaded_at": uploaded_at,
        "message": "Payment slip saved successfully.",
        "ocr": ocr,
    }


@app.get("/api/payment-slips")
async def list_payment_slips() -> list[dict]:
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, original_filename, content_type, size_bytes, uploaded_at,
                   ocr_status, extracted_account_number, extracted_amount
            FROM payment_slips ORDER BY uploaded_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
