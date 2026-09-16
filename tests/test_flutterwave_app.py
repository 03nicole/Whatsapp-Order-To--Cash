"""
Web-app-level tests for Phase 10 ("live MoMo webhook integration") -
the /momo/webhook route. Kept separate the same way each phase's app
tests are kept in their own file.
"""

import io
import json
from datetime import datetime, timedelta, timezone

import app as app_module
from conftest import webhook_payload

CATALOG_CSV = (
    "SKU,Product Name,UOM,Price,Stock\n"
    "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
)


def _place_order_via_whatsapp(client, business="WABiz", text="10 COKE-24",
                               sender_phone="260977111111", sender_name="ABC Traders"):
    """A real order-generated invoice, with a real orders row (and
    placed_at) behind it - unlike _upload_invoice() below, which has no
    order at all. Needed to test the within-the-24h-window free-text
    path, since that path only exists for invoices with a known
    last-customer-contact time."""
    client.post("/catalog/import", data={
        "business": business, "catalog": (io.BytesIO(CATALOG_CSV.encode()), "catalog.csv"),
    }, content_type="multipart/form-data")
    client.post("/whatsapp/webhook", query_string={"business": business},
                data=json.dumps(webhook_payload(text, sender_phone=sender_phone, sender_name=sender_name)),
                content_type="application/json")


def _backdate_order(business, order_id, hours_ago):
    conn = app_module.db.connect(app_module.DB_PATH)
    placed_at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn.execute("UPDATE orders SET placed_at = ? WHERE business = ? AND order_id = ?",
                 (placed_at, business, order_id))
    conn.commit()
    conn.close()


def _zm_payload(amount, charge_id=285959875, sender_phone="260977111111",
                 sender_name="ABC Traders", status="successful", payment_type="mobilemoneyzm"):
    # created_at defaults to "now" rather than a hardcoded date - a
    # fixed past date bit-rots the moment real time passes it, since
    # reconcile()'s date-ordering rule (an invoice can't be paid before
    # it existed) then excludes any invoice dated "today" from matching
    # at all. Found live by this exact failure once the real date
    # rolled past the previously-hardcoded "2026-09-15".
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "event": "charge.completed",
        "data": {
            "id": charge_id,
            "tx_ref": f"tx-{charge_id}",
            "amount": amount,
            "currency": "ZMW",
            "status": status,
            "payment_type": payment_type,
            "created_at": created_at,
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


class _RecordingClient:
    def __init__(self):
        self.sent = []
        self.sent_templates = []

    def send_text(self, to_phone, message):
        self.sent.append((to_phone, message))

    def send_template(self, to_phone, template_name, language_code, parameters):
        self.sent_templates.append((to_phone, template_name, language_code, parameters))


def test_webhook_uses_a_template_for_a_manually_uploaded_invoice_with_no_known_contact_time(client, monkeypatch):
    """_upload_invoice() creates an invoice with no backing `orders` row
    at all - this system has no record of this customer ever messaging
    in, so it must fail closed to "outside the window" (a template),
    never assume a free-text-eligible conversation it has no evidence
    for."""
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    recorder = _RecordingClient()
    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: recorder)
    _upload_invoice(client, amount=1750, phone="0977111111")

    r = _post_webhook(client, _zm_payload(1750, sender_phone="260977111111"))
    assert r.status_code == 200

    assert recorder.sent == []
    assert len(recorder.sent_templates) == 1
    to_phone, template_name, language_code, parameters = recorder.sent_templates[0]
    assert to_phone == "0977111111"  # the invoice's own customer_phone, not the payer's raw sender_phone
    assert template_name == app_module.WHATSAPP_PAYMENT_TEMPLATE_NAME
    assert language_code == app_module.WHATSAPP_PAYMENT_TEMPLATE_LANG
    assert parameters[0] == "INV-1"
    assert "1,750" in parameters[1] or "1750" in parameters[1]


def test_webhook_sends_free_text_for_an_order_placed_within_the_last_24_hours(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    recorder = _RecordingClient()
    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: recorder)
    _place_order_via_whatsapp(client)  # placed_at is "now" - well inside the window

    r = _post_webhook(client, _zm_payload(1200, sender_phone="260977111111"))  # 10 x K120
    assert r.status_code == 200

    # recorder.sent[0] is already the order-confirmation message from
    # placing the order itself - the payment notification is the second.
    assert recorder.sent_templates == []
    assert len(recorder.sent) == 2
    to_phone, message = recorder.sent[1]
    assert to_phone == "260977111111"
    assert "settled" in message


def test_webhook_uses_a_template_for_an_order_placed_over_24_hours_ago(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    recorder = _RecordingClient()
    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: recorder)
    _place_order_via_whatsapp(client)
    _backdate_order("WABiz", "wamid.1", hours_ago=30)

    r = _post_webhook(client, _zm_payload(1200, sender_phone="260977111111"))
    assert r.status_code == 200

    # recorder.sent[0] is the order-confirmation message, sent before the
    # backdate - the payment notification (after) uses a template instead.
    assert len(recorder.sent) == 1
    assert len(recorder.sent_templates) == 1
    assert recorder.sent_templates[0][0] == "260977111111"


def test_webhook_does_not_notify_on_an_unmatched_payment(client, monkeypatch):
    monkeypatch.setattr(app_module, "FLUTTERWAVE_SECRET_HASH", "test-secret")
    recorder = _RecordingClient()
    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: recorder)
    _post_webhook(client, _zm_payload(999))  # no invoices at all for WABiz

    assert recorder.sent == []
    assert recorder.sent_templates == []


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
