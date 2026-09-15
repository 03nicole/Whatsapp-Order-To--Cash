"""
Web-app-level tests for Phase 7 ("warehouse fulfillment") - the
/warehouse picking list and /warehouse/fulfill action. Kept separate
from test_orders_app.py the same way test_warehouse_db.py is kept
separate from test_orders_db.py.
"""

import io
import json

import pytest

import app as app_module

CATALOG_CSV = (
    "SKU,Product Name,UOM,Price,Stock\n"
    "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "uploads")
    app_module.UPLOAD_DIR.mkdir()
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _confirm_an_order_via_webhook(client, business="WABiz", message_id="wamid.1"):
    client.post(
        "/catalog/import",
        data={"business": business, "catalog": (io.BytesIO(CATALOG_CSV.encode()), "catalog.csv")},
        content_type="multipart/form-data",
    )
    payload = {
        "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "260977111111", "profile": {"name": "ABC Traders"}}],
            "messages": [{"from": "260977111111", "id": message_id, "type": "text",
                          "text": {"body": "10 COKE-24"}}],
        }}]}]
    }
    client.post("/whatsapp/webhook", query_string={"business": business},
                data=json.dumps(payload), content_type="application/json")


def test_warehouse_page_with_no_business_shows_the_picker(client):
    r = client.get("/warehouse")
    assert r.status_code == 200
    assert "View" in r.get_data(as_text=True)


def test_warehouse_lists_a_confirmed_order_with_its_lines(client):
    _confirm_an_order_via_webhook(client)
    r = client.get("/warehouse", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "wamid.1" in body
    assert "ABC Traders" in body
    assert "COKE-24" in body
    assert "10" in body


def test_warehouse_shows_empty_state_with_no_pending_orders(client):
    r = client.get("/warehouse", query_string={"business": "NoOrdersBiz"})
    assert "Nothing awaiting fulfillment" in r.get_data(as_text=True)


def test_fulfill_order_marks_it_fulfilled_and_removes_it_from_the_list(client):
    _confirm_an_order_via_webhook(client)
    r = client.post("/warehouse/fulfill", data={
        "business": "WABiz", "order_id": "wamid.1", "note": "picked by Joseph",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "marked fulfilled" in body
    assert "picked by Joseph" in body
    assert "Nothing awaiting fulfillment" in body  # no longer on the picking list


def test_fulfill_order_rejects_unknown_order(client):
    _confirm_an_order_via_webhook(client)
    r = client.post("/warehouse/fulfill", data={
        "business": "WABiz", "order_id": "does-not-exist",
    }, follow_redirects=True)
    assert "awaiting fulfillment" in r.get_data(as_text=True)


def test_fulfill_order_rejects_double_fulfillment(client):
    _confirm_an_order_via_webhook(client)
    client.post("/warehouse/fulfill", data={"business": "WABiz", "order_id": "wamid.1"},
                 follow_redirects=True)
    r = client.post("/warehouse/fulfill", data={"business": "WABiz", "order_id": "wamid.1"},
                     follow_redirects=True)
    assert "awaiting fulfillment" in r.get_data(as_text=True)


def test_warehouse_never_lists_a_flagged_order(client):
    _confirm_an_order_via_webhook(client)  # sets up catalog
    payload = {
        "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": "260977222222", "profile": {"name": "XYZ Co"}}],
            "messages": [{"from": "260977222222", "id": "wamid.flagged", "type": "text",
                          "text": {"body": "5 Nonexistent Product"}}],
        }}]}]
    }
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(payload), content_type="application/json")

    r = client.get("/warehouse", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "wamid.flagged" not in body
    assert "Nonexistent Product" not in body
