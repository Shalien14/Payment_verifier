const form = document.querySelector('#upload-form');
const orderForm = document.querySelector('#order-form');
const orderIdInput = document.querySelector('#order-id');
const orderResult = document.querySelector('#order-result');
const uploadSection = document.querySelector('#upload-section');
const customerName = document.querySelector('#customer-name');
const expectedPayment = document.querySelector('#expected-payment');
const businessAccount = document.querySelector('#business-account');
const fileInput = document.querySelector('#payment-slip');
const fileLabel = document.querySelector('#file-label');
const status = document.querySelector('#status');
const ocrResult = document.querySelector('#ocr-result');
const accountNumber = document.querySelector('#account-number');
const paymentAmount = document.querySelector('#payment-amount');
const ocrNote = document.querySelector('#ocr-note');
const decisionResult = document.querySelector('#decision-result');
const decisionTitle = document.querySelector('#decision-title');
const decisionNote = document.querySelector('#decision-note');
let selectedOrderId = null;

orderForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const orderId = orderIdInput.value.trim().toUpperCase();
  selectedOrderId = null;
  uploadSection.disabled = true;
  orderResult.hidden = true;
  decisionResult.hidden = true;
  status.textContent = 'Finding order…';
  status.className = '';
  try {
    const response = await fetch(`/api/orders/${encodeURIComponent(orderId)}`);
    const order = await response.json();
    if (!response.ok) throw new Error(order.detail || 'Order lookup failed.');
    if (order.status === 'PAID') throw new Error('This order is already paid. Use an unpaid order.');
    selectedOrderId = order.order_id;
    customerName.textContent = order.customer_name;
    expectedPayment.textContent = `${order.currency} ${order.expected_amount}`;
    businessAccount.textContent = order.business_account_ending;
    orderResult.hidden = false;
    uploadSection.disabled = false;
    status.textContent = 'Order ready for payment-slip upload.';
    status.className = 'success';
  } catch (error) {
    status.textContent = error.message;
    status.className = 'error';
  }
});

fileInput.addEventListener('change', () => {
  fileLabel.textContent = fileInput.files[0]?.name || 'Choose a JPG, PNG, WebP, or PDF';
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file || !selectedOrderId) return;

  status.textContent = 'Saving payment slip…';
  status.className = '';
  ocrResult.hidden = true;
  decisionResult.hidden = true;
  const formData = new FormData();
  formData.append('order_id', selectedOrderId);
  formData.append('file', file);

  try {
    const response = await fetch('/api/payment-slips', { method: 'POST', body: formData });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Upload failed.');
    status.textContent = `Saved ${data.filename} for ${data.order.order_id}.`;
    status.className = 'success';
    accountNumber.textContent = data.ocr.account_number || 'Not found';
    paymentAmount.textContent = data.ocr.amount || 'Not found';
    ocrNote.textContent = data.ocr.message;
    ocrResult.hidden = false;
    decisionTitle.textContent = data.decision.replace('_', ' ');
    decisionNote.textContent = data.decision_reason;
    decisionResult.className = data.decision.toLowerCase();
    decisionResult.hidden = false;
    if (data.decision === 'APPROVED') uploadSection.disabled = true;
    form.reset();
    fileLabel.textContent = 'Choose a JPG, PNG, WebP, or PDF';
  } catch (error) {
    status.textContent = error.message;
    status.className = 'error';
  }
});
