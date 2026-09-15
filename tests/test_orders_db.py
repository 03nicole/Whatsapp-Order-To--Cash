"""
Persistence tests for Phase 5 ("order capture") — the products/orders/
order_lines tables and helpers added to reconciler/db.py. Kept in a
separate file from test_db.py since that file's existing tests are the
Phase 2 persistence contract (invoice balances, transaction dedup) and
shouldn't grow unrelated fixtures; this file adds its own.
"""

from datetime import date

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


def test_catalog_without_description_or_image_url_columns_saves_as_none(conn, catalog_df):
    """catalog_df (this fixture) predates description/image_url - the same
    shape a hand-built DataFrame or an old-style catalog CSV import would
    have. save_catalog() must not blow up on a missing attribute."""
    db.save_catalog(conn, catalog_df, "biz")
    result = db.get_catalog(conn, "biz")
    assert result["description"].isna().all()
    coke = result[result["product_id"] == "COKE-24"].iloc[0]
    assert coke["description"] is None
    assert coke["image_url"] is None


def test_catalog_round_trips_description_and_image_url(conn, catalog_df):
    with_details = catalog_df.copy()
    with_details["description"] = ["Refreshing cola 300ml x24", None]
    with_details["image_url"] = ["https://example.com/coke.jpg", None]
    db.save_catalog(conn, with_details, "biz")

    result = db.get_catalog(conn, "biz")
    coke = result[result["product_id"] == "COKE-24"].iloc[0]
    fanta = result[result["product_id"] == "FANTA-24"].iloc[0]
    assert coke["description"] == "Refreshing cola 300ml x24"
    assert coke["image_url"] == "https://example.com/coke.jpg"
    assert fanta["description"] is None
    assert fanta["image_url"] is None


def test_update_product_details_sets_description_and_image_url(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    applied = db.update_product_details(
        conn, "biz", "COKE-24", "Refreshing cola 300ml x24", "https://example.com/coke.jpg",
    )
    assert applied is True

    coke = db.get_catalog(conn, "biz")
    coke = coke[coke["product_id"] == "COKE-24"].iloc[0]
    assert coke["description"] == "Refreshing cola 300ml x24"
    assert coke["image_url"] == "https://example.com/coke.jpg"


def test_update_product_details_blank_string_clears_the_field(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    db.update_product_details(conn, "biz", "COKE-24", "some description", "https://example.com/x.jpg")
    db.update_product_details(conn, "biz", "COKE-24", "", "")

    coke = db.get_catalog(conn, "biz")
    coke = coke[coke["product_id"] == "COKE-24"].iloc[0]
    assert coke["description"] is None
    assert coke["image_url"] is None


def test_update_product_details_returns_false_for_an_unknown_product(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    applied = db.update_product_details(conn, "biz", "NOT-A-PRODUCT", "x", "y")
    assert applied is False


def test_adjust_stock_applies_delta(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    db.adjust_stock(conn, "biz", "COKE-24", -10)
    result = db.get_catalog(conn, "biz")
    assert result.loc[result["product_id"] == "COKE-24", "quantity_on_hand"].iloc[0] == 40


def test_adjust_stock_returns_true_on_success(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    assert db.adjust_stock(conn, "biz", "COKE-24", -10) is True


def test_adjust_stock_returns_false_and_logs_nothing_for_an_unknown_product(conn, catalog_df):
    """Regression guard: adjust_stock used to always insert a
    stock_adjustments row even when the UPDATE matched nothing, which
    would have logged a bogus audit entry for a change that never
    happened. The route layer masked this by pre-checking existence
    itself - this test exercises adjust_stock() directly so the
    guarantee holds regardless of what calls it."""
    db.save_catalog(conn, catalog_df, "biz")
    result = db.adjust_stock(conn, "biz", "NOPE-1", -10, reason="should not be logged")
    assert result is False
    assert db.stock_history(conn, "biz").empty


def test_adjust_stock_can_increase_quantity(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    db.adjust_stock(conn, "biz", "COKE-24", 20, reason="received new stock")
    result = db.get_catalog(conn, "biz")
    assert result.loc[result["product_id"] == "COKE-24", "quantity_on_hand"].iloc[0] == 70


def test_adjust_stock_logs_every_change(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    # Explicit, distinct timestamps - adjust_stock's default (now(), second
    # precision) could tie two calls in the same test and make "newest
    # first" ordering non-deterministic.
    db.adjust_stock(conn, "biz", "COKE-24", -10, reason="order:abc123",
                     adjusted_at="2026-01-01T10:00:00")
    db.adjust_stock(conn, "biz", "COKE-24", 25, reason="received new stock",
                     adjusted_at="2026-01-01T11:00:00")

    history = db.stock_history(conn, "biz")
    assert len(history) == 2
    # newest first
    assert history.iloc[0]["reason"] == "received new stock"
    assert history.iloc[0]["delta"] == 25
    assert history.iloc[1]["reason"] == "order:abc123"
    assert history.iloc[1]["delta"] == -10


def test_stock_history_reports_a_missing_reason_as_none_not_nan(conn, catalog_df):
    """Regression test: found live, by actually looking at the catalog
    page's rendered history table. A NULL reason reads back from SQLite
    as float NaN even in pandas' nullable string dtype - and NaN is
    truthy in Python, so a template's `{{ h.reason or '' }}` rendered
    the literal text "nan" in the browser instead of a blank cell."""
    db.save_catalog(conn, catalog_df, "biz")
    db.adjust_stock(conn, "biz", "COKE-24", -5)  # no reason given

    reason = db.stock_history(conn, "biz").iloc[0]["reason"]
    assert reason is None
    assert not (isinstance(reason, float))  # specifically not NaN


def test_stock_history_filters_by_product(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz")
    db.adjust_stock(conn, "biz", "COKE-24", -5)
    db.adjust_stock(conn, "biz", "FANTA-24", -3)

    history = db.stock_history(conn, "biz", product_id="COKE-24")
    assert len(history) == 1
    assert history.iloc[0]["product_id"] == "COKE-24"


def test_stock_history_is_scoped_by_business(conn, catalog_df):
    db.save_catalog(conn, catalog_df, "biz-a")
    db.adjust_stock(conn, "biz-a", "COKE-24", -5)
    assert len(db.stock_history(conn, "biz-a")) == 1
    assert len(db.stock_history(conn, "biz-b")) == 0


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


def test_open_invoices_handles_a_mix_of_timezone_aware_and_naive_dates(conn):
    """Regression test: found live, not by inspection. A manually-
    uploaded invoice's date is date-only ("2026-01-01"); an order-
    generated invoice's `placed_at` is a full ISO timestamp. Storing the
    latter directly (a bug fixed in orders.build_invoice - now it stores
    only the date portion) made a business's invoices column come back
    entirely timezone-aware whenever every one of its invoices happened
    to be order-generated, and pd.Timestamp(as_of) - df["date"] in
    aging_report() raised TypeError the moment that column was compared
    against a naive timestamp. This test stores a raw tz-aware string
    directly (bypassing build_invoice's fix) so open_invoices() itself -
    the actual defense - is what's being verified, not just the
    call site that happens not to trigger it anymore."""
    db.record_order_invoice(conn, "biz", {
        "invoice_id": "ORD-1", "customer_name": "A", "customer_phone": "",
        "amount": 100.0, "date": "2026-09-01",
    })
    db.record_order_invoice(conn, "biz", {
        "invoice_id": "ORD-2", "customer_name": "B", "customer_phone": "",
        "amount": 200.0, "date": "2026-09-15T14:31:43+00:00",
    })

    open_df = db.open_invoices(conn, "biz")
    assert open_df["date"].dt.tz is None  # normalized, not mixed
    assert len(open_df) == 2

    aging_df = db.aging_report(conn, "biz", as_of=date(2026, 9, 20))
    assert not aging_df.empty
    days = dict(zip(aging_df["invoice_id"], aging_df["days_outstanding"]))
    assert days["ORD-1"] == 19
    assert days["ORD-2"] == 4
