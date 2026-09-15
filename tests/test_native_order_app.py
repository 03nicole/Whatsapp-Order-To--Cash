"""
Web-app-level tests for Phase 5b ("WhatsApp native Catalog/Cart
checkout") - the /whatsapp/webhook route's handling of "order"-type
messages. Kept separate from test_orders_app.py (Phase 5's free-text
path) the same way each phase's app tests are kept in their own file.
"""

import io
import json

import app as app_module

CATALOG_CSV = (
    "SKU,Product Name,UOM,Price,Stock\n"
    "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
    "FANTA-24,Fanta Orange 300ml 24-pack,box,110,5\n"
)


def _import_catalog(client, business="WABiz"):
    return client.post(
        "/catalog/import",
        data={"business": business, "catalog": (io.BytesIO(CATALOG_CSV.encode()), "catalog.csv")},
        content_type="multipart/form-data",
    )


def _order_payload(product_items, sender_phone="260977111111", sender_name="ABC Traders",
                    message_id="wamid.order1", note=""):
    return {
        "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": sender_phone, "profile": {"name": sender_name}}],
            "messages": [{
                "from": sender_phone, "id": message_id, "type": "order",
                "order": {"catalog_id": "104954523425094", "product_items": product_items,
                          "text": note},
            }],
        }}]}]
    }


def _post_order(client, product_items, business="WABiz", **kwargs):
    return client.post("/whatsapp/webhook", query_string={"business": business},
                        data=json.dumps(_order_payload(product_items, **kwargs)),
                        content_type="application/json")


def test_native_order_creates_an_invoice_in_the_existing_table(client):
    _import_catalog(client)
    r = _post_order(client, [
        {"product_retailer_id": "COKE-24", "quantity": 10, "item_price": 120.0, "currency": "ZMW"},
    ])
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert len(balances) == 1
    invoice_id, balance = next(iter(balances.items()))
    assert invoice_id.startswith("ORD-")
    assert balance == 1200.0


def test_native_order_decrements_stock(client):
    _import_catalog(client)
    _post_order(client, [
        {"product_retailer_id": "COKE-24", "quantity": 10, "item_price": 120.0, "currency": "ZMW"},
    ])
    conn = app_module.db.connect(app_module.DB_PATH)
    catalog = app_module.db.get_catalog(conn, "WABiz")
    conn.close()
    coke = catalog[catalog["product_id"] == "COKE-24"].iloc[0]
    assert coke["quantity_on_hand"] == 40  # 50 - 10


def test_native_order_with_unknown_retailer_id_is_flagged_not_confirmed(client):
    """A mismatched Meta-catalog-to-our-catalog sync (or a stale/removed
    product) shouldn't silently ship a wrong or partial order."""
    _import_catalog(client)
    r = _post_order(client, [
        {"product_retailer_id": "SOME-OTHER-SKU", "quantity": 2, "item_price": 50.0, "currency": "ZMW"},
    ])
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    flagged = app_module.db.flagged_orders(conn, "WABiz")
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert len(flagged) == 1
    assert "SOME-OTHER-SKU" in flagged.iloc[0]["flag_reason"]
    assert balances == {}


def test_native_order_with_insufficient_stock_is_flagged_and_stock_untouched(client):
    _import_catalog(client)
    r = _post_order(client, [
        {"product_retailer_id": "FANTA-24", "quantity": 999, "item_price": 110.0, "currency": "ZMW"},
    ])  # only 5 on hand
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    flagged = app_module.db.flagged_orders(conn, "WABiz")
    catalog = app_module.db.get_catalog(conn, "WABiz")
    conn.close()
    assert len(flagged) == 1
    assert "insufficient stock" in flagged.iloc[0]["flag_reason"]
    fanta = catalog[catalog["product_id"] == "FANTA-24"].iloc[0]
    assert fanta["quantity_on_hand"] == 5  # untouched


def test_native_order_sends_a_whatsapp_confirmation(client, monkeypatch):
    sent = []

    class RecordingClient:
        def send_text(self, to_phone, message):
            sent.append((to_phone, message))

    monkeypatch.setattr(app_module.whatsapp, "get_client", lambda: RecordingClient())
    _import_catalog(client)
    _post_order(client, [
        {"product_retailer_id": "COKE-24", "quantity": 10, "item_price": 120.0, "currency": "ZMW"},
    ])

    assert len(sent) == 1
    to_phone, message = sent[0]
    assert to_phone == "260977111111"
    assert "confirmed" in message.lower()
    assert "1,200" in message


def test_native_order_raw_message_is_recorded_for_the_orders_review_page(client):
    """A flagged native order should still be legible to a human on
    /orders, not just a bare error - the raw_message stand-in describes
    what was actually ordered."""
    _import_catalog(client)
    _post_order(client, [
        {"product_retailer_id": "NOT-A-REAL-SKU", "quantity": 3, "item_price": 50.0, "currency": "ZMW"},
    ], note="please deliver by Friday")

    r = client.get("/orders", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "WhatsApp catalog order" in body
    assert "NOT-A-REAL-SKU" in body
    assert "deliver by Friday" in body


def test_a_text_message_in_the_same_webhook_batch_is_still_handled_as_free_text(client):
    """The two order-capture paths (free text and native cart) run over
    the same webhook payload independently - a text-type message must
    still resolve via the code/name waterfall even if an order-type
    message from a different customer arrives in the same batch."""
    _import_catalog(client)
    payload = {
        "entry": [{"changes": [{"value": {
            "contacts": [
                {"wa_id": "260977111111", "profile": {"name": "ABC Traders"}},
                {"wa_id": "260977222222", "profile": {"name": "XYZ Co"}},
            ],
            "messages": [
                {"from": "260977111111", "id": "wamid.text1", "type": "text",
                 "text": {"body": "10 COKE-24"}},
                {"from": "260977222222", "id": "wamid.order1", "type": "order",
                 "order": {"catalog_id": "cat1",
                           "product_items": [{"product_retailer_id": "FANTA-24", "quantity": 5,
                                               "item_price": 110.0, "currency": "ZMW"}],
                           "text": ""}},
            ],
        }}]}]
    }
    r = client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                     data=json.dumps(payload), content_type="application/json")
    assert r.status_code == 200

    conn = app_module.db.connect(app_module.DB_PATH)
    balances = app_module.db.known_balances(conn, "WABiz")
    conn.close()
    assert len(balances) == 2
    assert sorted(balances.values()) == [550.0, 1200.0]  # 5*110 and 10*120
