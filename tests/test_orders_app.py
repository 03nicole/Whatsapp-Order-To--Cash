"""
Web-app-level tests for Phase 5 ("order capture") - catalog import,
the WhatsApp webhook routes, and the flagged-orders review page. Kept
separate from test_app.py (Phase 4, reconcile/aging routes) the same
way test_orders_db.py is kept separate from test_db.py.
"""

import io
import json
from pathlib import Path

import app as app_module
from conftest import webhook_payload

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"

CATALOG_CSV = (
    "SKU,Product Name,UOM,Price,Stock\n"
    "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
    "FANTA-24,Fanta Orange 300ml 24-pack,box,110,30\n"
)


def _import_catalog(client, business="WABiz"):
    return client.post(
        "/catalog/import",
        data={"business": business, "catalog": (io.BytesIO(CATALOG_CSV.encode()), "catalog.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )


# --- catalog import ---------------------------------------------------------

def test_import_catalog_persists_products(client):
    r = _import_catalog(client)
    assert "Imported 2 product(s)" in r.get_data(as_text=True)


def test_import_catalog_rejects_missing_file(client):
    r = client.post("/catalog/import", data={"business": "WABiz"},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "Choose a catalog file" in r.get_data(as_text=True)


# --- /catalog stock management ----------------------------------------------

def test_catalog_page_lists_products_and_no_history_initially(client):
    _import_catalog(client)
    r = client.get("/catalog", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "COKE-24" in body
    assert "No stock changes logged yet" in body


def test_adjust_stock_receives_new_stock(client):
    _import_catalog(client)
    r = client.post("/catalog/adjust", data={
        "business": "WABiz", "product_id": "COKE-24", "delta": "20", "reason": "new delivery",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Adjusted COKE-24 by +20" in body
    assert "new delivery" in body

    conn = app_module.db.connect(app_module.DB_PATH)
    catalog = app_module.db.get_catalog(conn, "WABiz")
    conn.close()
    coke = catalog[catalog["product_id"] == "COKE-24"].iloc[0]
    assert coke["quantity_on_hand"] == 70  # 50 + 20


def test_adjust_stock_can_correct_downward(client):
    _import_catalog(client)
    client.post("/catalog/adjust", data={
        "business": "WABiz", "product_id": "COKE-24", "delta": "-15", "reason": "recount",
    })
    conn = app_module.db.connect(app_module.DB_PATH)
    catalog = app_module.db.get_catalog(conn, "WABiz")
    conn.close()
    coke = catalog[catalog["product_id"] == "COKE-24"].iloc[0]
    assert coke["quantity_on_hand"] == 35  # 50 - 15


def test_adjust_stock_rejects_unknown_product(client):
    _import_catalog(client)
    r = client.post("/catalog/adjust", data={
        "business": "WABiz", "product_id": "NOPE-1", "delta": "10",
    }, follow_redirects=True)
    assert "No product" in r.get_data(as_text=True)
    assert "NOPE-1" in r.get_data(as_text=True)


def test_adjust_stock_rejects_non_integer_delta(client):
    _import_catalog(client)
    r = client.post("/catalog/adjust", data={
        "business": "WABiz", "product_id": "COKE-24", "delta": "not-a-number",
    }, follow_redirects=True)
    assert "whole number" in r.get_data(as_text=True)


def test_catalog_page_shows_adjustment_history_newest_first(client):
    # follow_redirects=True on each POST so its flash message is consumed
    # on that request's own redirect target, instead of queuing up in the
    # session and contaminating the later GET this test actually inspects.
    _import_catalog(client)
    client.post("/catalog/adjust", data={
        "business": "WABiz", "product_id": "COKE-24", "delta": "20", "reason": "first delivery",
    }, follow_redirects=True)
    client.post("/catalog/adjust", data={
        "business": "WABiz", "product_id": "COKE-24", "delta": "-5", "reason": "damaged stock",
    }, follow_redirects=True)
    r = client.get("/catalog", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    history_start = body.index("Recent stock changes")
    assert body.index("damaged stock", history_start) < body.index("first delivery", history_start)


# --- /catalog/edit (description/image) --------------------------------------

def test_edit_product_details_sets_description_and_image(client):
    _import_catalog(client)
    r = client.post("/catalog/edit", data={
        "business": "WABiz", "product_id": "COKE-24",
        "description": "Refreshing cola 300ml x24", "image_url": "https://example.com/coke.jpg",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Updated details for COKE-24" in body
    assert "Refreshing cola 300ml x24" in body
    assert 'src="https://example.com/coke.jpg"' in body


def test_edit_product_details_rejects_unknown_product(client):
    _import_catalog(client)
    r = client.post("/catalog/edit", data={
        "business": "WABiz", "product_id": "NOPE-1",
        "description": "x", "image_url": "https://example.com/x.jpg",
    }, follow_redirects=True)
    assert "No product" in r.get_data(as_text=True)
    assert "NOPE-1" in r.get_data(as_text=True)


def test_catalog_page_shows_placeholder_when_no_image_set(client):
    _import_catalog(client)
    r = client.get("/catalog", query_string={"business": "WABiz"})
    assert "product-thumb-empty" in r.get_data(as_text=True)


# --- /catalog/feed.csv (Meta Commerce Catalog export) ------------------------

def test_catalog_page_shows_a_copyable_feed_url(client):
    _import_catalog(client)
    r = client.get("/catalog", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "/catalog/feed.csv?business=WABiz&amp;token=" in body or "/catalog/feed.csv?business=WABiz&token=" in body


def test_feed_rejects_a_missing_token(client):
    _import_catalog(client)
    r = client.get("/catalog/feed.csv", query_string={"business": "WABiz"})
    assert r.status_code == 403


def test_feed_rejects_a_wrong_token(client):
    _import_catalog(client)
    r = client.get("/catalog/feed.csv", query_string={"business": "WABiz", "token": "wrong"})
    assert r.status_code == 403


def test_feed_serves_a_valid_csv_with_the_correct_token(client):
    _import_catalog(client)
    token = app_module.catalog_feed_token("WABiz")
    r = client.get("/catalog/feed.csv", query_string={"business": "WABiz", "token": token})
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    body = r.get_data(as_text=True)
    assert "COKE-24" in body
    assert "id,title,description,availability,condition,price,link,image_link,brand" in body


def test_feed_token_is_scoped_by_business(client):
    """A token that's valid for one business must not work for another -
    otherwise anyone who ever saw one business's feed URL could read
    every other business's product/price list too."""
    _import_catalog(client, business="WABiz")
    _import_catalog(client, business="OtherBiz")
    wabiz_token = app_module.catalog_feed_token("WABiz")
    r = client.get("/catalog/feed.csv", query_string={"business": "OtherBiz", "token": wabiz_token})
    assert r.status_code == 403


def test_order_consumption_also_appears_in_stock_history(client):
    """adjust_stock() is the single path both manual corrections and order
    confirmation go through - the audit trail should show both."""
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(webhook_payload("10 COKE-24")),
                content_type="application/json")

    conn = app_module.db.connect(app_module.DB_PATH)
    history = app_module.db.stock_history(conn, "WABiz")
    conn.close()
    assert len(history) == 1
    assert history.iloc[0]["delta"] == -10
    assert history.iloc[0]["reason"].startswith("order:")


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
                     data=json.dumps(webhook_payload("10 COKE-24, 5 FANTA-24")),
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
                data=json.dumps(webhook_payload("10 COKE-24")),
                content_type="application/json")

    conn = app_module.db.connect(app_module.DB_PATH)
    catalog = app_module.db.get_catalog(conn, "WABiz")
    conn.close()
    coke = catalog[catalog["product_id"] == "COKE-24"].iloc[0]
    assert coke["quantity_on_hand"] == 40  # 50 - 10


def test_ambiguous_order_is_flagged_not_confirmed(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(webhook_payload("10 Nonexistent Product")),
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
                data=json.dumps(webhook_payload("999 COKE-24")),
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
                data=json.dumps(webhook_payload("10 COKE-24")),
                content_type="application/json")

    assert len(sent) == 1
    to_phone, message = sent[0]
    assert to_phone == "260977111111"
    assert "confirmed" in message.lower()


# --- /orders review page -----------------------------------------------------

def test_orders_review_lists_flagged_orders(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(webhook_payload("10 Nonexistent Product")),
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
                data=json.dumps(webhook_payload("10 COKE-24", message_id="wamid.order1")),
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
