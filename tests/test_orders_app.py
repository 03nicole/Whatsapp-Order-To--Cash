"""
Web-app-level tests for Phase 5 ("order capture") - catalog import,
the WhatsApp webhook routes, and the flagged-orders review page. Kept
separate from test_app.py (Phase 4, reconcile/aging routes) the same
way test_orders_db.py is kept separate from test_db.py.
"""

import io
import json
from pathlib import Path

import pytest

import app as app_module

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"

CATALOG_CSV = (
    "SKU,Product Name,UOM,Price,Stock\n"
    "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
    "FANTA-24,Fanta Orange 300ml 24-pack,box,110,30\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "WHATSAPP_VERIFY_TOKEN", "test-verify-token")
    app_module.UPLOAD_DIR.mkdir()
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _import_catalog(client, business="WABiz"):
    return client.post(
        "/catalog/import",
        data={"business": business, "catalog": (io.BytesIO(CATALOG_CSV.encode()), "catalog.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )


def _webhook_payload(text, sender_phone="260977111111", sender_name="ABC Traders", message_id="wamid.1"):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"wa_id": sender_phone, "profile": {"name": sender_name}}],
                    "messages": [{"from": sender_phone, "id": message_id, "type": "text",
                                  "text": {"body": text}}],
                }
            }]
        }]
    }


# --- catalog import ---------------------------------------------------------

def test_import_catalog_persists_products(client):
    r = _import_catalog(client)
    assert "Imported 2 product(s)" in r.get_data(as_text=True)


def test_import_catalog_rejects_missing_file(client):
    r = client.post("/catalog/import", data={"business": "WABiz"},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "Choose a catalog file" in r.get_data(as_text=True)


# --- webhook verification (GET) ---------------------------------------------

def test_webhook_verification_succeeds_with_correct_token(client):
    r = client.get("/whatsapp/webhook", query_string={
        "hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "12345",
    })
    assert r.status_code == 200
    assert r.get_data(as_text=True) == "12345"


def test_webhook_verification_rejects_wrong_token(client):
    r = client.get("/whatsapp/webhook", query_string={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345",
    })
    assert r.status_code == 403


# --- webhook order flow (POST) -----------------------------------------------

def test_confirmed_order_creates_an_invoice_in_the_existing_table(client):
    _import_catalog(client)
    r = client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                     data=json.dumps(_webhook_payload("10 COKE-24, 5 FANTA-24")),
                     content_type="application/json")
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert len(balances) == 1
    invoice_id, balance = next(iter(balances.items()))
    assert invoice_id.startswith("ORD-")
    assert balance == 10 * 120.0 + 5 * 110.0


def test_confirmed_order_decrements_stock(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(_webhook_payload("10 COKE-24")),
                content_type="application/json")

    conn = app_module.db.connect(app_module.DB_PATH)
    catalog = app_module.db.get_catalog(conn, "WABiz")
    conn.close()
    coke = catalog[catalog["product_id"] == "COKE-24"].iloc[0]
    assert coke["quantity_on_hand"] == 40  # 50 - 10


def test_ambiguous_order_is_flagged_not_confirmed(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(_webhook_payload("10 Nonexistent Product")),
                content_type="application/json")

    conn = app_module.db.connect(app_module.DB_PATH)
    flagged = app_module.db.flagged_orders(conn, "WABiz")
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert len(flagged) == 1
    assert balances == {}  # no invoice created


def test_insufficient_stock_flags_and_does_not_touch_stock(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(_webhook_payload("999 COKE-24")),
                content_type="application/json")

    conn = app_module.db.connect(app_module.DB_PATH)
    flagged = app_module.db.flagged_orders(conn, "WABiz")
    catalog = app_module.db.get_catalog(conn, "WABiz")
    conn.close()
    assert len(flagged) == 1
    assert "insufficient stock" in flagged.iloc[0]["flag_reason"]
    coke = catalog[catalog["product_id"] == "COKE-24"].iloc[0]
    assert coke["quantity_on_hand"] == 50  # untouched


def test_confirmed_order_sends_a_whatsapp_confirmation(client, monkeypatch):
    sent = []

    class RecordingClient:
        def send_text(self, to_phone, message):
            sent.append((to_phone, message))

    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: RecordingClient())
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(_webhook_payload("10 COKE-24")),
                content_type="application/json")

    assert len(sent) == 1
    to_phone, message = sent[0]
    assert to_phone == "260977111111"
    assert "confirmed" in message.lower()


# --- /orders review page -----------------------------------------------------

def test_orders_review_lists_flagged_orders(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(_webhook_payload("10 Nonexistent Product")),
                content_type="application/json")

    r = client.get("/orders", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "Nonexistent Product" in body
    assert "unknown_product" in body


def test_orders_review_with_no_business_shows_the_picker(client):
    r = client.get("/orders")
    assert r.status_code == 200
    assert "View" in r.get_data(as_text=True)


# --- end-to-end: order -> invoice -> flows through the EXISTING reconciler --

def test_order_generated_invoice_reconciles_against_a_real_momo_payment(client):
    """The whole point of Phase 5: an invoice created from a WhatsApp
    order is indistinguishable, to the reconciliation engine, from one
    that came from a manually-uploaded invoice file - proven here by
    driving the actual /reconcile web route a real pilot business would
    use, not by calling reconciler.matcher directly."""
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(_webhook_payload("10 COKE-24", message_id="wamid.order1")),
                content_type="application/json")

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    invoice_id = next(iter(balances))
    amount = balances[invoice_id]

    # A business that orders entirely through WhatsApp may never maintain
    # a separate invoice export - an empty-but-valid invoices file (headers
    # only) stands in for "we have nothing new to upload", and
    # db.combine_with_open_invoices is what makes the order-generated
    # invoice available to match against anyway.
    empty_invoices_csv = "Invoice No,Customer,Phone,Amount,Date\n"
    momo_csv = (
        "Txn ID,Date,Amount,Sender,Sender Phone,Narration\n"
        f"TXN1,2026-01-05,{amount:.0f},ABC Traders,260977111111,{invoice_id} payment\n"
    )
    r = client.post(
        "/reconcile",
        data={
            "business": "WABiz", "date_window": "45",
            "invoices": (io.BytesIO(empty_invoices_csv.encode()), "invoices.csv"),
            "momo_statement": (io.BytesIO(momo_csv.encode()), "momo.csv"),
        },
        content_type="multipart/form-data", follow_redirects=True,
    )
    body = r.get_data(as_text=True)
    assert "Reconciliation complete" in body
    assert ">1<" in body  # one matched transaction

    conn = app_module.db.connect(app_module.DB_PATH)
    remaining_balance = app_module.db.known_balances(conn, "WABiz")[invoice_id]
    conn.close()
    assert remaining_balance == 0  # the order-generated invoice was paid off
