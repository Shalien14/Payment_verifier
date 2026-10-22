import sqlite3
import unittest

from app import parse_payment_details, decide_payment


class ParsePaymentDetailsTests(unittest.TestCase):
    def test_extracts_gbp_amount(self):
        self.assertEqual(parse_payment_details("Amount: GBP 10.00"), (None, "GBP 10.00"))

    def test_extracts_masked_account_and_amount(self):
        text = "Account No: XXXX XXXX 0028\nAmount: GBP 10.00"
        self.assertEqual(parse_payment_details(text), ("XXXXXXXX0028", "GBP 10.00"))

    def test_rs_currency_is_not_assumed_to_be_inr(self):
        self.assertEqual(parse_payment_details("Amount: Rs. 1,250.00")[1], "UNKNOWN 1250.00")

    def test_unlabelled_balance_is_not_selected_as_amount(self):
        text = "Balance: GBP 500.00"
        self.assertIsNone(parse_payment_details(text)[1])

    def test_amount_without_currency_is_marked_unknown(self):
        self.assertEqual(parse_payment_details("Amount: 10.00")[1], "UNKNOWN 10.00")

    def test_ignores_following_label_on_same_account_line(self):
        text = "Account No: 12345678 Amount: GBP 10.00"
        self.assertEqual(parse_payment_details(text), ("12345678", "GBP 10.00"))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.order = {
            "expected_amount": "10.00",
            "currency": "GBP",
        }

    def test_ambiguous_currency_needs_review(self):
        decision, _ = decide_payment(self.order, {
            "status": "completed",
            "account_number": "XXXX0028",
            "amount": "UNKNOWN 10.00",
        })
        self.assertEqual(decision, "NEEDS_REVIEW")

    def test_matching_details_do_not_auto_mark_order_paid(self):
        decision, reason = decide_payment(self.order, {
            "status": "completed",
            "account_number": "XXXX0028",
            "amount": "GBP 10.00",
        })
        self.assertEqual(decision, "NEEDS_REVIEW")
        self.assertIn("cannot prove", reason)

    def test_incomplete_ocr_needs_review(self):
        decision, _ = decide_payment(self.order, {
            "status": "completed",
            "account_number": None,
            "amount": None,
        })
        self.assertEqual(decision, "NEEDS_REVIEW")


    def test_boc_receipt_prefers_beneficiary_account_and_extracts_fields(self):
        from app import parse_receipt_details
        text = """Fund Transfer
Transaction Details
Transaction Amount : LKR 400.00
Transaction Date & Time : 06-10-2026 01:44 PM
Transaction ID : FX10740464500000
Transaction Status : SUCCESS
Sender A/C Details
Account Number : **6193
Sender's Bank : Bank of Ceylon
Beneficiary A/C Details
Account No : 0070804435
Beneficiary Bank : Bank of Ceylon"""
        fields = parse_receipt_details(text)
        self.assertEqual(fields["amount"], "LKR 400.00")
        self.assertEqual(fields["transaction_id"], "FX10740464500000")
        self.assertEqual(fields["transfer_status"], "SUCCESS")
        self.assertEqual(fields["beneficiary_account"], "0070804435")
        self.assertEqual(fields["beneficiary_bank"], "Bank of Ceylon")

    def test_boc_transfer_to_other_bank(self):
        from app import parse_receipt_details
        text = """Transaction Amount : LKR 20,000.00
Transaction ID : FX11225309700000
Transaction Status : SUCCESS
Sender A/C Details
Account Number : **6193
Beneficiary A/C Details
Account No : 118020027668
Beneficiary Bank : Hatton National Bank"""
        fields = parse_receipt_details(text)
        self.assertEqual(fields["amount"], "LKR 20000.00")
        self.assertEqual(fields["beneficiary_account"], "118020027668")
        self.assertEqual(fields["beneficiary_bank"], "Hatton National Bank")


if __name__ == "__main__":
    unittest.main()
