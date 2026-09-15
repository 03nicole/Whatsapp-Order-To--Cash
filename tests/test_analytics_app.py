"""
Web-app-level tests for Phase 9 ("analytics") - the /analytics dashboard.
Kept separate the same way each phase's app tests are kept in their own
file.
"""

import io
import json

from conftest import webhook_payload

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


def test_analytics_page_with_no_business_shows_the_picker(client):
    r = client.get("/analytics")
    assert r.status_code == 200
    assert "Built from the same numbers" in r.get_data(as_text=True)


def test_analytics_page_for_a_business_with_no_data_shows_empty_states(client):
    r = client.get("/analytics", query_string={"business": "EmptyBiz"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "No reconciled transactions yet" in body
    assert "No open invoices" in body
    assert "No products in the catalog" in body


def test_analytics_page_reflects_a_confirmed_order(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(webhook_payload("10 COKE-24")),
                content_type="application/json")

    r = client.get("/analytics", query_string={"business": "WABiz"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "K1,200" in body  # total outstanding
    assert "FANTA-24" in body  # shows up in lowest-stock (only 5 on hand, untouched)


def test_analytics_page_reflects_reconciliation_outcomes(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(webhook_payload("10 COKE-24", message_id="wamid.order1")),
                content_type="application/json")

    empty_invoices_csv = "Invoice No,Customer,Phone,Amount,Date\n"
    momo_csv = (
        "Txn ID,Date,Amount,Sender,Sender Phone,Narration\n"
        "TXN1,2026-01-05,1200,ABC Traders,260977111111,ORD-wamid.order1 payment\n"
    )
    client.post("/reconcile", data={
        "business": "WABiz", "date_window": "45",
        "invoices": (io.BytesIO(empty_invoices_csv.encode()), "invoices.csv"),
        "momo_statement": (io.BytesIO(momo_csv.encode()), "momo.csv"),
    }, content_type="multipart/form-data")

    r = client.get("/analytics", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "100%" in body  # match rate
    assert "segment-matched" in body


def test_analytics_page_shows_warehouse_backlog(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(webhook_payload("10 COKE-24")),
                content_type="application/json")

    r = client.get("/analytics", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "Warehouse backlog" in body
    # one confirmed, not-yet-fulfilled order
    backlog_section = body.split("Warehouse backlog")[1][:200]
    assert ">1<" in backlog_section


def test_analytics_page_shows_flagged_order_link(client):
    _import_catalog(client)
    client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                data=json.dumps(webhook_payload("10 Nonexistent Product")),
                content_type="application/json")

    r = client.get("/analytics", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "flagged orders" in body.lower()
