"""
Persistence tests for Phase 9 ("analytics") - reconciliation_summary(),
aging_bucket_totals(), order_summary(), and lowest_stock(). Kept separate
the same way each phase's db tests are kept in their own file.
"""

import pandas as pd
import pytest

from reconciler import db
from reconciler.matcher import reconcile


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


# --- reconciliation_summary --------------------------------------------

def test_reconciliation_summary_on_a_fresh_business_is_all_zero(conn):
    summary = db.reconciliation_summary(conn, "biz")
    assert summary["total_count"] == 0
    assert summary["match_rate"] == 0.0
    for outcome in ("matched", "partial", "needs_review", "unmatched"):
        assert summary["by_outcome"][outcome] == {"count": 0, "amount": 0.0}


def test_reconciliation_summary_counts_and_sums_by_outcome(conn, make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="A", amount=1000, date="2026-01-01"),
        dict(invoice_id="INV-2", customer_name="B", amount=500, date="2026-01-01"),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=1000, reference="INV-1 payment"),
        dict(transaction_id="T2", amount=42, reference="wallet topup"),
    ])
    results = reconcile(invoices, momo)
    db.save_run(conn, results, "biz")

    summary = db.reconciliation_summary(conn, "biz")
    assert summary["total_count"] == 2
    assert summary["by_outcome"]["matched"] == {"count": 1, "amount": 1000.0}
    assert summary["by_outcome"]["unmatched"] == {"count": 1, "amount": 42.0}
    assert summary["match_rate"] == 0.5


def test_reconciliation_summary_is_scoped_by_business(conn, make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", amount=1000, date="2026-01-01")])
    momo = make_momo([dict(transaction_id="T1", amount=1000, reference="INV-1 payment")])
    results = reconcile(invoices, momo)
    db.save_run(conn, results, "biz-a")

    assert db.reconciliation_summary(conn, "biz-a")["total_count"] == 1
    assert db.reconciliation_summary(conn, "biz-b")["total_count"] == 0


# --- aging_bucket_totals -------------------------------------------------

def test_aging_bucket_totals_zero_fills_empty_buckets():
    empty = pd.DataFrame(columns=["balance", "bucket"])
    totals = db.aging_bucket_totals(empty)
    assert totals == {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}


def test_aging_bucket_totals_sums_per_bucket():
    aging_df = pd.DataFrame([
        {"balance": 100.0, "bucket": "0-30"},
        {"balance": 50.0, "bucket": "0-30"},
        {"balance": 200.0, "bucket": "90+"},
    ])
    totals = db.aging_bucket_totals(aging_df)
    assert totals == {"0-30": 150.0, "31-60": 0.0, "61-90": 0.0, "90+": 200.0}


# --- order_summary ---------------------------------------------------------

def test_order_summary_counts_by_status(conn):
    db.save_order(conn, "biz", {
        "order_id": "ok1", "customer_name": "A", "customer_phone": "",
        "placed_at": "2026-01-01T10:00:00", "status": "confirmed",
        "raw_message": "10 COKE-24", "invoice_id": "ORD-ok1", "flag_reason": None,
    }, [])
    db.record_order_invoice(conn, "biz", {
        "invoice_id": "ORD-ok1", "customer_name": "A", "customer_phone": "",
        "amount": 1200.0, "date": "2026-01-01",
    })
    db.save_order(conn, "biz", {
        "order_id": "bad1", "customer_name": "B", "customer_phone": "",
        "placed_at": "2026-01-02T10:00:00", "status": "flagged",
        "raw_message": "10 Guinness", "invoice_id": None, "flag_reason": "unknown_product",
    }, [])

    summary = db.order_summary(conn, "biz")
    assert summary["by_status"] == {"confirmed": 1, "flagged": 1, "fulfilled": 0}
    assert summary["confirmed_value"] == 1200.0


def test_order_summary_includes_fulfilled_orders_in_confirmed_value(conn):
    db.save_order(conn, "biz", {
        "order_id": "ok1", "customer_name": "A", "customer_phone": "",
        "placed_at": "2026-01-01T10:00:00", "status": "confirmed",
        "raw_message": "10 COKE-24", "invoice_id": "ORD-ok1", "flag_reason": None,
    }, [])
    db.record_order_invoice(conn, "biz", {
        "invoice_id": "ORD-ok1", "customer_name": "A", "customer_phone": "",
        "amount": 1200.0, "date": "2026-01-01",
    })
    db.mark_order_fulfilled(conn, "biz", "ok1")

    summary = db.order_summary(conn, "biz")
    assert summary["by_status"]["fulfilled"] == 1
    assert summary["by_status"]["confirmed"] == 0
    assert summary["confirmed_value"] == 1200.0  # fulfilled orders still count


def test_order_summary_on_a_fresh_business_is_all_zero(conn):
    summary = db.order_summary(conn, "biz")
    assert summary["by_status"] == {"confirmed": 0, "flagged": 0, "fulfilled": 0}
    assert summary["confirmed_value"] == 0


# --- lowest_stock ------------------------------------------------------

def test_lowest_stock_returns_ascending_by_quantity(conn):
    catalog = pd.DataFrame([
        {"product_id": "A", "name": "Product A", "unit": "each", "unit_price": 10.0, "quantity_on_hand": 50},
        {"product_id": "B", "name": "Product B", "unit": "each", "unit_price": 10.0, "quantity_on_hand": 5},
        {"product_id": "C", "name": "Product C", "unit": "each", "unit_price": 10.0, "quantity_on_hand": 20},
    ])
    db.save_catalog(conn, catalog, "biz")

    result = db.lowest_stock(conn, "biz", limit=2)
    assert list(result["product_id"]) == ["B", "C"]


def test_lowest_stock_is_scoped_by_business(conn):
    catalog = pd.DataFrame([
        {"product_id": "A", "name": "Product A", "unit": "each", "unit_price": 10.0, "quantity_on_hand": 5},
    ])
    db.save_catalog(conn, catalog, "biz-a")
    assert len(db.lowest_stock(conn, "biz-a")) == 1
    assert len(db.lowest_stock(conn, "biz-b")) == 0
