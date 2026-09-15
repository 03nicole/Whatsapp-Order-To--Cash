"""
Persistence tests for Phase 5 ("order capture") — the products/orders/
order_lines tables and helpers added to reconciler/db.py. Kept in a
separate file from test_db.py since that file's existing tests are the
Phase 2 persistence contract (invoice balances, transaction dedup) and
shouldn't grow unrelated fixtures; this file adds its own.
"""

import pandas as pd
import pytest

from reconciler import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def catalog_df():
    return pd.DataFrame([
        {"product_id": "COKE-24", "name": "Coca-Cola 300ml 24-pack", "unit": "box",
         "unit_price": 120.0, "quantity_on_hand": 50},
        {"product_id": "FANTA-24", "name": "Fanta Orange 300ml 24-pack", "unit": "box",
         "unit_price": 110.0, "quantity_on_hand": 30},
    ])


# --- catalog ---------------------------------------------------------------

def test_save_and_get_catalog_round_trips(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    result = db.get_catalog(conn, "biz")
    assert len(result) == 2
    assert set(result["product_id"]) == {"COKE-24", "FANTA-24"}
    assert result.loc[result["product_id"] == "COKE-24", "quantity_on_hand"].iloc[0] == 50


def test_save_catalog_upserts_rather_than_duplicates(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    updated = catalog_df.copy()
    updated.loc[updated["product_id"] == "COKE-24", "unit_price"] = 130.0
    updated.loc[updated["product_id"] == "COKE-24", "quantity_on_hand"] = 40
    db.save_catalog(conn, updated, "biz")

    result = db.get_catalog(conn, "biz")
    assert len(result) == 2  # not duplicated
    coke = result[result["product_id"] == "COKE-24"].iloc[0]
    assert coke["unit_price"] == 130.0
    assert coke["quantity_on_hand"] == 40


def test_catalog_is_scoped_by_business(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz-a")
    assert len(db.get_catalog(conn, "biz-a")) == 2
    assert len(db.get_catalog(conn, "biz-b")) == 0


def test_adjust_stock_applies_delta(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    db.adjust_stock(conn, "biz", "COKE-24", -10)
    result = db.get_catalog(conn, "biz")
    assert result.loc[result["product_id"] == "COKE-24", "quantity_on_hand"].iloc[0] == 40


# --- orders -----------------------------------------------------------------

def test_save_order_persists_order_and_lines(conn):
    order = {
        "order_id": "abc123", "customer_name": "ABC Traders", "customer_phone": "0977111111",
        "placed_at": "2026-01-01T10:00:00", "status": "confirmed", "raw_message": "10 COKE-24",
        "invoice_id": "ORD-abc123", "flag_reason": None,
    }
    lines = [{"product_id": "COKE-24", "raw_text": "10 COKE-24", "quantity_requested": 10,
               "unit_price": 120.0, "line_total": 1200.0, "resolution": "exact_code"}]
    db.save_order(conn, "biz", order, lines)

    row = conn.execute(
        "SELECT status, invoice_id FROM orders WHERE business = ? AND order_id = ?",
        ("biz", "abc123"),
    ).fetchone()
    assert row == ("confirmed", "ORD-abc123")

    line_row = conn.execute(
        "SELECT product_id, quantity_requested, resolution FROM order_lines "
        "WHERE business = ? AND order_id = ?", ("biz", "abc123"),
    ).fetchone()
    assert line_row == ("COKE-24", 10, "exact_code")


def test_flagged_orders_lists_only_flagged_status(conn):
    confirmed = {"order_id": "ok1", "customer_name": "A", "customer_phone": "0977111111",
                 "placed_at": "2026-01-01T10:00:00", "status": "confirmed",
                 "raw_message": "10 COKE-24", "invoice_id": "ORD-ok1", "flag_reason": None}
    flagged = {"order_id": "bad1", "customer_name": "B", "customer_phone": "0977222222",
               "placed_at": "2026-01-02T10:00:00", "status": "flagged",
               "raw_message": "10 Guinness", "invoice_id": None,
               "flag_reason": "could not resolve: 'Guinness' -> unknown_product"}
    db.save_order(conn, "biz", confirmed, [])
    db.save_order(conn, "biz", flagged, [])

    result = db.flagged_orders(conn, "biz")
    assert len(result) == 1
    assert result.iloc[0]["order_id"] == "bad1"
    assert "unknown_product" in result.iloc[0]["flag_reason"]


def test_combine_with_open_invoices_surfaces_a_db_only_invoice(conn, make_invoices):
    """An order-generated invoice has no accompanying upload at all - it
    must still show up as a reconciliation candidate."""
    invoice = {"invoice_id": "ORD-abc123", "customer_name": "ABC Traders",
               "customer_phone": "0977111111", "amount": 1200.0, "date": "2026-01-01"}
    db.record_order_invoice(conn, "biz", invoice)

    uploaded = make_invoices([dict(invoice_id="INV-1", customer_name="Other Co", amount=500)])
    combined = db.combine_with_open_invoices(conn, "biz", uploaded)

    assert set(combined["invoice_id"]) == {"INV-1", "ORD-abc123"}
    assert combined.loc[combined["invoice_id"] == "ORD-abc123", "balance"].iloc[0] == 1200.0


def test_combine_with_open_invoices_does_not_duplicate_an_invoice_present_in_both(conn, make_invoices):
    invoice = {"invoice_id": "INV-1", "customer_name": "ABC", "customer_phone": "0977111111",
               "amount": 1000.0, "date": "2026-01-01"}
    db.record_order_invoice(conn, "biz", invoice)

    uploaded = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    combined = db.combine_with_open_invoices(conn, "biz", uploaded)
    assert len(combined) == 1  # not duplicated


def test_combine_with_open_invoices_does_not_mutate_input(conn, make_invoices):
    db.record_order_invoice(conn, "biz", {"invoice_id": "ORD-1", "customer_name": "A",
                                            "customer_phone": "", "amount": 100.0,
                                            "date": "2026-01-01"})
    uploaded = make_invoices([dict(invoice_id="INV-1", amount=500)])
    db.combine_with_open_invoices(conn, "biz", uploaded)
    assert list(uploaded["invoice_id"]) == ["INV-1"]  # untouched


def test_record_order_invoice_writes_into_the_existing_invoices_table(conn):
    """This is the whole point of Phase 5: an order-generated invoice
    lands in the SAME table load_invoices()/save_run() use, not a
    parallel one."""
    invoice = {"invoice_id": "ORD-abc123", "customer_name": "ABC Traders",
               "customer_phone": "0977111111", "amount": 1200.0, "date": "2026-01-01"}
    db.record_order_invoice(conn, "biz", invoice)

    balances = db.known_balances(conn, "biz")
    assert balances == {"ORD-abc123": 1200.0}

    open_df = db.open_invoices(conn, "biz")
    assert open_df.loc[0, "invoice_id"] == "ORD-abc123"
    assert open_df.loc[0, "customer_name"] == "ABC Traders"
    assert open_df.loc[0, "balance"] == 1200.0
