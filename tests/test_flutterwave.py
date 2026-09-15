import pandas as pd

from reconciler.flutterwave import parse_webhook_payload, to_momo_dataframe, verify_signature

# Real documented shape (https://developer.flutterwave.com/docs/webhooks,
# https://developer.flutterwave.com/v3.0/docs/zambia-mobile-money), not a
# guessed one.
ZM_MOBILE_MONEY_PAYLOAD = {
    "event": "charge.completed",
    "data": {
        "id": 285959875,
        "tx_ref": "Links-616626414629",
        "flw_ref": "ABCTraders/FLW270177170",
        "amount": 1750,
        "currency": "ZMW",
        "charged_amount": 1750,
        "app_fee": 24.5,
        "merchant_fee": 0,
        "processor_response": "Approved",
        "status": "successful",
        "payment_type": "mobilemoneyzm",
        "created_at": "2026-09-15T14:31:43.000Z",
        "customer": {
            "id": 215604089,
            "name": "ABC Traders",
            "phone_number": "260977111111",
            "email": "abctraders@example.com",
            "created_at": "2026-09-15T14:31:43.000Z",
        },
    },
}


# --- verify_signature --------------------------------------------------

def test_verify_signature_accepts_a_matching_hash():
    assert verify_signature({"verif-hash": "s3cr3t"}, "s3cr3t") is True


def test_verify_signature_rejects_a_mismatched_hash():
    assert verify_signature({"verif-hash": "wrong"}, "s3cr3t") is False


def test_verify_signature_rejects_a_missing_header():
    assert verify_signature({}, "s3cr3t") is False


def test_verify_signature_rejects_when_no_secret_is_configured():
    """No secret configured means signature verification can't possibly
    have been set up - fail closed, never treat an unconfigured secret
    as "anything goes"."""
    assert verify_signature({"verif-hash": "anything"}, "") is False


# --- parse_webhook_payload -----------------------------------------------

def test_parses_a_completed_zambia_mobile_money_payment():
    payment = parse_webhook_payload(ZM_MOBILE_MONEY_PAYLOAD)
    assert payment is not None
    assert payment.transaction_id == "285959875"
    assert payment.amount == 1750.0
    assert payment.sender_name == "ABC Traders"
    assert payment.sender_phone == "260977111111"
    assert payment.date == "2026-09-15T14:31:43.000Z"


def test_ignores_a_non_charge_completed_event():
    payload = {"event": "transfer.completed", "data": ZM_MOBILE_MONEY_PAYLOAD["data"]}
    assert parse_webhook_payload(payload) is None


def test_ignores_a_non_zambia_mobile_money_payment_type():
    """A Flutterwave merchant account can accept cards, other countries'
    mobile money, etc. through the same webhook URL - only ZM mobile
    money collections get reconciled here."""
    payload = {"event": "charge.completed",
               "data": {**ZM_MOBILE_MONEY_PAYLOAD["data"], "payment_type": "card"}}
    assert parse_webhook_payload(payload) is None


def test_ignores_a_pending_or_failed_charge():
    for status in ("pending", "failed"):
        payload = {"event": "charge.completed",
                   "data": {**ZM_MOBILE_MONEY_PAYLOAD["data"], "status": status}}
        assert parse_webhook_payload(payload) is None


def test_tolerates_a_missing_customer_object():
    payload = {"event": "charge.completed",
               "data": {**ZM_MOBILE_MONEY_PAYLOAD["data"], "customer": None}}
    payment = parse_webhook_payload(payload)
    assert payment is not None
    assert payment.sender_name == ""
    assert payment.sender_phone == ""


def test_empty_payload_returns_none():
    assert parse_webhook_payload({}) is None


# --- to_momo_dataframe -----------------------------------------------------

def test_to_momo_dataframe_shapes_the_row_reconcile_expects():
    payment = parse_webhook_payload(ZM_MOBILE_MONEY_PAYLOAD)
    df = to_momo_dataframe(payment)
    assert list(df.columns) == ["transaction_id", "date", "amount", "sender_name",
                                 "sender_phone", "reference"]
    assert len(df) == 1
    assert df.loc[0, "transaction_id"] == "285959875"
    assert df.loc[0, "amount"] == 1750.0
    assert df.loc[0, "sender_phone"] == "260977111111"
    assert isinstance(df.loc[0, "date"], pd.Timestamp)
