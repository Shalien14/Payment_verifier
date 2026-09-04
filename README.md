# BuildStart payment verification MVP

This first milestone accepts a payment-slip image or PDF, stores the upload in `data/uploads/`, and records basic metadata in SQLite at `data/payments.db`.

## Run it

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
   The image OCR also needs the Tesseract application. On macOS with Homebrew:
   ```bash
   brew install tesseract
   ```
3. Start the app:
   ```bash
   uvicorn app:app --reload
   ```
4. Open http://127.0.0.1:8000.

The uploaded files should be treated as sensitive payment data. `data/` is intentionally ignored by Git.

## Next: OCR, after uploads work

Use Python throughout. Add OCR as a second endpoint/service so upload storage stays reliable and easy to test.

- **Fastest local option:** `pytesseract` plus the Tesseract binary. Good for printed, clean slips; install it separately on the machine.
- **More robust API option:** Google Cloud Vision, AWS Textract, or Azure AI Vision. They tend to handle varied receipt layouts better, but require credentials and can cost money.

This version uses Tesseract for image uploads and stores raw OCR text, the first detected account number, amount, and OCR status. It reads the original image alongside enlarged, contrast-enhanced, and high-contrast variants to make phone screenshots and low-contrast slips easier to read. Its extraction patterns are intentionally conservative and are not enough to approve payments automatically without the existing exact account, currency, and amount checks.
