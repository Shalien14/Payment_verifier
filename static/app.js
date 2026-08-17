const form = document.querySelector('#upload-form');
const fileInput = document.querySelector('#payment-slip');
const fileLabel = document.querySelector('#file-label');
const status = document.querySelector('#status');
const ocrResult = document.querySelector('#ocr-result');
const accountNumber = document.querySelector('#account-number');
const paymentAmount = document.querySelector('#payment-amount');
const ocrNote = document.querySelector('#ocr-note');

fileInput.addEventListener('change', () => {
  fileLabel.textContent = fileInput.files[0]?.name || 'Choose a JPG, PNG, WebP, or PDF';
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  status.textContent = 'Saving payment slip…';
  status.className = '';
  ocrResult.hidden = true;
  const formData = new FormData();
  formData.append('file', file);

  try {
    const response = await fetch('/api/payment-slips', { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Upload failed.');
    status.textContent = `Saved ${data.filename} successfully.`;
    status.className = 'success';
    accountNumber.textContent = data.ocr.account_number || 'Not found';
    paymentAmount.textContent = data.ocr.amount || 'Not found';
    ocrNote.textContent = data.ocr.message;
    ocrResult.hidden = false;
    form.reset();
    fileLabel.textContent = 'Choose a JPG, PNG, WebP, or PDF';
  } catch (error) {
    status.textContent = error.message;
    status.className = 'error';
  }
});
