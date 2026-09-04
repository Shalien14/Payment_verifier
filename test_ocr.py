from app import parse_payment_details


def test_extracts_masked_account_and_inr_amount():
    account, amount, transfer_status = parse_payment_details("A/C No: XXXX-XXXX-0028\nAmount Paid: ₹ 1,250.00")
    assert account == "XXXXXXXX0028"
    assert amount == "INR 1250.00"
    assert transfer_status is None


def test_extracts_gbp_amount_and_account_number():
    account, amount, transfer_status = parse_payment_details("Account number: 1234 5678 0028\nPayment: GBP 10.00")
    assert account == "123456780028"
    assert amount == "GBP 10.00"
    assert transfer_status is None


def test_does_not_use_unlabelled_numbers_as_amounts():
    account, amount, transfer_status = parse_payment_details("Reference 20260904\nAccount: XX0028")
    assert account == "XX0028"
    assert amount is None
    assert transfer_status is None


def test_prioritises_beneficiary_account_and_reads_lkr_transfer():
    account, amount, transfer_status = parse_payment_details(
        "Sender A/C Details\nAccount Number : **0139\n"
        "Beneficiary A/C Details\nAccount No : 0007396275\n"
        "Transaction Amount : LKR 1,430.00\nTransaction Status : SUCCESS"
    )
    assert account == "0007396275"
    assert amount == "LKR 1430.00"
    assert transfer_status == "SUCCESS"
