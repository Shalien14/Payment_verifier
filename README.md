# BuildStart Payment Verification MVP

A FastAPI application for uploading payment slips, extracting a few labelled fields with local Tesseract OCR, and comparing them with demo orders.

## Requirements

- Python 3.10+
- Tesseract OCR installed on the machine
- Python packages in `requirements.txt`

On macOS with Homebrew:

```bash
brew install tesseract
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000.

## Run tests

From this project directory:

```bash
python -m unittest discover -s tests -v
```

The current tests cover labelled amount extraction, masked account numbers, ambiguous currency handling, and conservative verification decisions. They do not yet measure OCR accuracy against a representative set of real bank slips.

## Current OCR behavior

- Tries the original image plus grayscale/autocontrast, sharpened, and thresholded variants.
- Tries Tesseract page segmentation modes 6 and 11.
- Shows the mean Tesseract word-confidence estimate for the selected OCR pass. This is not a payment-validity score.
- If OCR passes disagree on extracted account or amount values, the conflicting field is left empty so the result requires review.
- Does not assume that `Rs.` means INR; ambiguous currency is marked `UNKNOWN`.
- PDF files can be uploaded but are not yet OCR-processed.

## Important safety limitation

A screenshot is not proof that funds have been received. Even if the account, currency, and amount match an order, this version returns `NEEDS_REVIEW`. Confirm payment independently using a trusted bank/payment-provider transaction record before marking an order paid.

This is an initial improvement, not production-ready payment processing. Before real deployment, add server-side file-content validation, bounded streaming uploads, authentication and authorization, rate limiting, transactional order updates, duplicate transaction-reference checks, data-retention controls, and integration with a trusted payment confirmation source.

## Data privacy

The `data/` directory is created at runtime and may contain sensitive uploads and a local SQLite database. Do not commit real payment slips or the database to source control. Use synthetic or redacted slips for tests.


## BOC PDF receipt support (v2)

- Text-based PDFs are parsed directly with PyMuPDF, avoiding OCR errors when embedded text is available.
- BOC receipt fields include transaction amount, transaction ID, transaction date/time, transfer status, beneficiary account, and beneficiary bank.
- The parser prioritizes the beneficiary account over the sender account.
- Image uploads continue to use multiple Tesseract preprocessing/configuration passes.
- Scanned/image-only PDFs render the first page and use the image OCR pipeline as a fallback.
- A receipt's printed SUCCESS status is not independent confirmation that funds were received.
