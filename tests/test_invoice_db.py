"""
Tests for db.get_invoice_detail() - the first "real, renderable invoice
document" this system has had (see app.py's /invoice/<id> route). Until
now `invoices` was only ever a ledger row used for reconciliation
matching.
"""

import pandas as pd
import pytest

from reconciler import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _save_catalog(conn, business="biz"):
    catalog = pd.DataFrame([
        {"product_id": "COKE-24", "name": "Coca-Cola 300ml 24-pack", "unit": "box",
         "unit_price": 120.0, "quantity_on_hand": 50},
        {"product_id": "SALT-1KG", "name": "Salt 1kg 12-pack", "unit": "pack",
         "unit_price": 48.0, "quantity_on_hand": 30},
    ])
    db.save_catalog(conn, catalog, business)


def _save_confirmed_order(conn, business, order_id, invoice_id, placed_at="2026-01-01T10:00:00"):
    order = {
        "order_id": order_id, "customer_name": "ABC Traders", "customer_phone": "0977111111",
        "placed_at": placed_at, "status": "confirmed", "raw_message": "10 COKE-24, 2 SALT-1KG",
        "invoice_id": invoice_id, "flag_reason": None,
    }
    lines = [
        {"product_id": "COKE-24", "raw_text": "10 COKE-24", "quantity_requested": 10,
         "unit_price": 120.0, "line_total": 1200.0, "resolution": "exact_code"},
        {"product_id": "SALT-1KG", "raw_text": "2 SALT-1KG", "quantity_requested": 2,
         "unit_price": 48.0, "line_total": 96.0, "resolution": "exact_code"},
    ]
    db.save_order(conn, business, order, lines)
    db.record_order_invoice(conn, business, {
        "invoice_id": invoice_id, "customer_name": "ABC Traders", "customer_phone": "0977111111",
        "amount": 1296.0, "date": placed_at[:10],
    })


def test_returns_none_for_an_unknown_invoice(conn):
    assert db.get_invoice_detail(conn, "biz", "NOPE-1") is None


def test_order_generated_invoice_includes_real_line_items(conn):
    _save_catalog(conn)
    _save_confirmed_order(conn, "biz", "order-1", "ORD-order-1")

    detail = db.get_invoice_detail(conn, "biz", "ORD-order-1")
    assert detail["invoice_id"] == "ORD-order-1"
    assert detail["customer_name"] == "ABC Traders"
    assert detail["amount"] == 1296.0
    assert detail["order_id"] == "order-1"
    assert len(detail["lines"]) == 2
    coke = next(l for l in detail["lines"] if l["product_id"] == "COKE-24")
    assert coke["name"] == "Coca-Cola 300ml 24-pack"
    assert coke["quantity"] == 10
    assert coke["unit_price"] == 120.0
    assert coke["line_total"] == 1200.0


def test_manually_uploaded_invoice_has_no_line_items_but_still_resolves(conn):
    """No matching `orders` row - an invoice a human uploaded via CSV,
    never generated from a WhatsApp order. Real amount/balance, but no
    line-item detail was ever captured, so none is invented."""
    db.save_run(conn, {
        "matched": pd.DataFrame(), "partial": pd.DataFrame(),
        "needs_review": pd.DataFrame(), "unmatched": pd.DataFrame(),
        "all_invoices": pd.DataFrame([{
            "invoice_id": "INV-1", "customer_name": "XYZ Traders", "customer_phone": "0966111111",
            "amount": 500.0, "date": pd.Timestamp("2026-01-01"), "balance": 500.0,
        }]),
    }, "biz")

    detail = db.get_invoice_detail(conn, "biz", "INV-1")
    assert detail["invoice_id"] == "INV-1"
    assert detail["order_id"] is None
    assert detail["lines"] == []


def test_line_item_uses_a_deleted_or_renamed_products_id_as_a_name_fallback(conn):
    """If the product row is ever gone by the time an old invoice is
    viewed (this system never actually deletes products today, but the
    LEFT JOIN should still degrade honestly rather than crash)."""
    order = {
        "order_id": "order-1", "customer_name": "ABC Traders", "customer_phone": "0977111111",
        "placed_at": "2026-01-01T10:00:00", "status": "confirmed", "raw_message": "1 GONE-1",
        "invoice_id": "ORD-order-1", "flag_reason": None,
    }
    lines = [{"product_id": "GONE-1", "raw_text": "1 GONE-1", "quantity_requested": 1,
              "unit_price": 10.0, "line_total": 10.0, "resolution": "exact_code"}]
    db.save_order(conn, "biz", order, lines)
    db.record_order_invoice(conn, "biz", {
        "invoice_id": "ORD-order-1", "customer_name": "ABC Traders", "customer_phone": "0977111111",
        "amount": 10.0, "date": "2026-01-01",
    })

    detail = db.get_invoice_detail(conn, "biz", "ORD-order-1")
    assert detail["lines"][0]["name"] == "GONE-1"  # falls back to the product_id itself


def test_is_scoped_by_business(conn):
    _save_catalog(conn, business="biz-a")
    _save_confirmed_order(conn, "biz-a", "order-1", "ORD-order-1")
    assert db.get_invoice_detail(conn, "biz-b", "ORD-order-1") is None
