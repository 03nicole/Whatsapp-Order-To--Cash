"""
Web-app-level tests for Phase 10 ("live MoMo webhook integration") -
the /momo/webhook route. Kept separate the same way each phase's app
tests are kept in their own file.
"""

import io
import json

import app as app_module


def _zm_payload(amount, charge_id=285959875, sender_phone="260977111111",
                 sender_name="ABC Traders", status="successful", payment_type="mobilemoneyzm"):
    return {
        "event": "charge.completed",
        "data": {
            "id": charge_id,
            "tx_ref": f"tx-{charge_id}",
            "amount": amount,
            "currency": "ZMW",
            "status": status,
            "payment_type": payment_type,
            "created_at": "2026-09-15T14:31:43.000Z",
            "customer": {"name": sender_name, "phone_number": sender_phone},
        },
    }


def _post_webhook(client, payload, business="WABiz", secret="test-secret"):
    return client.post(
        "/momo/webhook", query_string={"business": business},
        data=json.dumps(payload), content_type="application/json",
        headers={"verif-hash": secret},
    )


def _upload_invoice(client, business="WABiz", invoice_id="INV-1", amount=1750,
                     customer="ABC Traders", phone="0977111111"):
    csv = (
        "Invoice No,Customer,Phone,Amount,Date\n"
        f"{invoice_id},{customer},{phone},{amount},2026-09-01\n"
    )
    momo_csv = "Txn ID,Date,Amount,Sender,Sender Phone,Narration\n"  # empty, just to seed invoices
    return client.post("/reconcile", data={
        "business": business, "date_window": "45",
        "invoices": (io.BytesIO(csv.encode()), "invoices.csv"),
        "momo_statement": (io.BytesIO(momo_csv.encode()), "momo.csv"),
    }, content_type="multipart/form-data")


def test_webhook_rejects_a_missing_signature(client):
    r = client.post("/momo/webhook", query_string={"business": "WABiz"},
                     data=json.dumps(_zm_payload(1750)), content_type="application/json")
    assert r.status_code == 401


def test_webhook_rejects_a_wrong_signature(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    r = client.post("/momo/webhook", query_string={"business": "WABiz"},
                     data=json.dumps(_zm_payload(1750)), content_type="application/json",
                     headers={"verif-hash": "wrong-secret"})
    assert r.status_code == 401


def test_webhook_accepts_a_correctly_signed_request(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    r = _post_webhook(client, _zm_payload(1750))
    assert r.status_code == 200


def test_webhook_reconciles_a_matching_invoice(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    _upload_invoice(client, amount=1750)

    r = _post_webhook(client, _zm_payload(1750))
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert balances["INV-1"] == 0.0  # paid off


def test_webhook_ignores_a_card_payment_on_the_same_account(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    _upload_invoice(client, amount=1750)

    r = _post_webhook(client, _zm_payload(1750, payment_type="card"))
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert balances["INV-1"] == 1750.0  # untouched - never reconciled


def test_webhook_ignores_a_pending_charge(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    _upload_invoice(client, amount=1750)

    r = _post_webhook(client, _zm_payload(1750, status="pending"))
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert balances["INV-1"] == 1750.0


def test_webhook_does_not_double_process_a_retried_delivery(client, monkeypatch):
    """Flutterwave (like most webhook providers) may retry delivery -
    the same charge id posted twice must not be reconciled twice."""
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    _upload_invoice(client, amount=1750)
    # Overpay scenario would reveal double-processing: a second
    # identical delivery re-matching the same (already-zeroed) invoice
    # would either no-op correctly or wrongly go negative/re-flag.
    _post_webhook(client, _zm_payload(1750))
    _post_webhook(client, _zm_payload(1750))  # retry, same charge id

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    transactions = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE business='WABiz'"
    ).fetchone()[0]
    conn.close()
    assert balances["INV-1"] == 0.0
    assert transactions == 1  # not recorded twice


def test_webhook_sends_a_whatsapp_payment_confirmation_on_a_full_match(client, monkeypatch):
    """The other half of the order-confirmation promise ("Pay via MoMo
    and we'll confirm once it's received") - a live webhook payment that
    fully settles an invoice should actually tell the customer."""
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    sent = []

    class RecordingClient:
        def send_text(self, to_phone, message):
            sent.append((to_phone, message))

    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: RecordingClient())
    _upload_invoice(client, amount=1750, phone="0977111111")

    r = _post_webhook(client, _zm_payload(1750, sender_phone="260977111111"))
    assert r.status_code == 200

    assert len(sent) == 1
    to_phone, message = sent[0]
    assert to_phone == "0977111111"  # the invoice's own customer_phone, not the payer's raw sender_phone
    assert "INV-1" in message
    assert "1,750" in message or "1750" in message


def test_webhook_does_not_notify_on_an_unmatched_payment(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    sent = []

    class RecordingClient:
        def send_text(self, to_phone, message):
            sent.append((to_phone, message))

    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: RecordingClient())
    _post_webhook(client, _zm_payload(999))  # no invoices at all for WABiz

    assert sent == []


def test_webhook_leaves_an_unmatched_payment_for_review(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    r = _post_webhook(client, _zm_payload(999))  # no invoices at all for WABiz
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    txn = conn.execute(
        "SELECT outcome FROM transactions WHERE business='WABiz' AND transaction_id='285959875'"
    ).fetchone()
    conn.close()
    assert txn[0] == "unmatched"
