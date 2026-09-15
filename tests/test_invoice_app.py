"""
Web-app-level tests for the /invoice/<invoice_id> route - the first
actual invoice *document* this system renders, as opposed to the
`invoices` ledger row that's always driven reconciliation.
"""

import io
import json

import app as app_module
from conftest import webhook_payload

CATALOG_CSV = (
    "SKU,Product Name,UOM,Price,Stock\n"
    "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
)


def _import_catalog(client, business="WABiz"):
    return client.post(
        "/catalog/import",
        data={"business": business, "catalog": (io.BytesIO(CATALOG_CSV.encode()), "catalog.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )


def _place_order(client, text="10 COKE-24", business="WABiz"):
    return client.post("/whatsapp/webhook", query_string={"business": business},
                        data=json.dumps(webhook_payload(text)), content_type="application/json")


def test_invoice_page_shows_real_line_items_for_an_order_generated_invoice(client):
    _import_catalog(client)
    _place_order(client)
    r = client.get("/invoice/ORD-wamid.1", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "ORD-wamid.1" in body
    assert "Coca-Cola 300ml 24-pack" in body
    assert "COKE-24" in body
    assert "1,200.00" in body  # 10 x K120


def test_invoice_page_shows_awaiting_payment_status_when_unpaid(client):
    _import_catalog(client)
    _place_order(client)
    r = client.get("/invoice/ORD-wamid.1", query_string={"business": "WABiz"})
    assert "Awaiting payment" in r.get_data(as_text=True)


def test_invoice_page_shows_paid_in_full_once_balance_is_zero(client):
    _import_catalog(client)
    _place_order(client)

    conn = app_module.db.connect(app_module.DB_PATH)
    conn.execute("UPDATE invoices SET balance = 0 WHERE invoice_id = 'ORD-wamid.1'")
    conn.commit()
    conn.close()

    r = client.get("/invoice/ORD-wamid.1", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "Paid in full" in body


def test_invoice_page_shows_partially_paid(client):
    _import_catalog(client)
    _place_order(client)  # K1,200 invoice

    conn = app_module.db.connect(app_module.DB_PATH)
    conn.execute("UPDATE invoices SET balance = 600 WHERE invoice_id = 'ORD-wamid.1'")
    conn.commit()
    conn.close()

    r = client.get("/invoice/ORD-wamid.1", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "Partially paid" in body
    assert "600.00" in body  # balance due


def test_invoice_page_rejects_an_unknown_invoice(client):
    r = client.get("/invoice/NOT-REAL", query_string={"business": "WABiz"}, follow_redirects=True)
    assert "No invoice" in r.get_data(as_text=True)


def test_invoice_page_requires_a_business(client):
    r = client.get("/invoice/ORD-wamid.1", follow_redirects=True)
    assert "Business name is required" in r.get_data(as_text=True)


def test_invoice_page_is_linked_from_warehouse(client):
    _import_catalog(client)
    _place_order(client)
    r = client.get("/warehouse", query_string={"business": "WABiz"})
    assert 'href="/invoice/ORD-wamid.1?business=WABiz"' in r.get_data(as_text=True)


def test_invoice_page_notes_missing_line_items_for_a_manually_uploaded_invoice(client):
    csv = "Invoice No,Customer,Phone,Amount,Date\nINV-1,ABC Traders,0977111111,1000,2026-01-01\n"
    momo_csv = "Txn ID,Date,Amount,Sender,Sender Phone,Narration\n"
    client.post("/reconcile", data={
        "business": "WABiz", "date_window": "45",
        "invoices": (io.BytesIO(csv.encode()), "invoices.csv"),
        "momo_statement": (io.BytesIO(momo_csv.encode()), "momo.csv"),
    }, content_type="multipart/form-data")

    r = client.get("/invoice/INV-1", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "available for this invoice" in body
