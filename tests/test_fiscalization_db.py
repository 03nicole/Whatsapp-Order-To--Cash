"""
Persistence tests for Phase 11's follow-up: mark_order_fiscalized(),
mark_order_fiscalization_failed(), orders_needing_fiscalization_retry(),
and the fiscalization_* migration on `orders`. Kept separate from
test_orders_db.py (which already covers zra_settings and the
vat_category_code/item_class_code columns themselves) the same way
test_warehouse_db.py is kept separate from test_orders_db.py.
"""

import sqlite3

import pytest

from reconciler import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _save_confirmed_order(conn, business, order_id, placed_at="2026-01-01T10:00:00"):
    order = {
        "order_id": order_id, "customer_name": "ABC Traders", "customer_phone": "0977111111",
        "placed_at": placed_at, "status": "confirmed", "raw_message": "10 COKE-24",
        "invoice_id": f"ORD-{order_id}", "flag_reason": None,
    }
    lines = [{"product_id": "COKE-24", "raw_text": "10 COKE-24", "quantity_requested": 10,
               "unit_price": 120.0, "line_total": 1200.0, "resolution": "exact_code"}]
    db.save_order(conn, business, order, lines)


# --- migration ---------------------------------------------------------------

def test_connect_adds_fiscalization_columns_to_a_pre_existing_orders_table(tmp_path):
    """Same real-world scenario as Phase 7's own migration test: a
    business's database already has order rows in it from before this
    phase existed."""
    db_path = tmp_path / "existing.db"
    old_conn = sqlite3.connect(db_path)
    old_conn.executescript("""
        CREATE TABLE orders (
            business TEXT NOT NULL, order_id TEXT NOT NULL, customer_name TEXT,
            customer_phone TEXT, placed_at TEXT NOT NULL, status TEXT NOT NULL,
            raw_message TEXT, invoice_id TEXT, flag_reason TEXT,
            fulfilled_at TEXT, fulfillment_note TEXT,
            PRIMARY KEY (business, order_id)
        );
        INSERT INTO orders (business, order_id, placed_at, status)
        VALUES ('biz', 'order-1', '2026-01-01T10:00:00', 'confirmed');
    """)
    old_conn.commit()
    old_conn.close()

    conn = db.connect(db_path)  # should migrate, not error
    row = conn.execute(
        "SELECT fiscalization_status, fiscalization_error, fiscalization_response "
        "FROM orders WHERE order_id = 'order-1'"
    ).fetchone()
    assert row == (None, None, None)
    conn.close()


# --- mark_order_fiscalized / mark_order_fiscalization_failed -----------------

def test_mark_order_fiscalized_sets_status_and_stores_response(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    db.mark_order_fiscalized(conn, "biz", "order-1", {"Result": {"resultCd": "000"}})

    row = conn.execute(
        "SELECT fiscalization_status, fiscalization_error, fiscalization_response "
        "FROM orders WHERE business='biz' AND order_id='order-1'"
    ).fetchone()
    assert row[0] == "fiscalized"
    assert row[1] is None
    assert "resultCd" in row[2]


def test_mark_order_fiscalization_failed_sets_status_and_error(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    db.mark_order_fiscalization_failed(conn, "biz", "order-1", "ZRA rejected: missing TPIN")

    row = conn.execute(
        "SELECT fiscalization_status, fiscalization_error "
        "FROM orders WHERE business='biz' AND order_id='order-1'"
    ).fetchone()
    assert row == ("failed", "ZRA rejected: missing TPIN")


def test_a_retry_that_succeeds_clears_the_previous_error(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    db.mark_order_fiscalization_failed(conn, "biz", "order-1", "ZRA unreachable")
    db.mark_order_fiscalized(conn, "biz", "order-1", {"Result": {"resultCd": "000"}})

    row = conn.execute(
        "SELECT fiscalization_status, fiscalization_error "
        "FROM orders WHERE business='biz' AND order_id='order-1'"
    ).fetchone()
    assert row == ("fiscalized", None)


def test_marking_an_unknown_order_never_raises(conn):
    """Deliberately silent, unlike mark_order_fulfilled's False return -
    this is called from a best-effort path that must never itself become
    a new failure mode (see app.py's _fiscalize_order())."""
    db.mark_order_fiscalized(conn, "biz", "not-a-real-order", {"ok": True})
    db.mark_order_fiscalization_failed(conn, "biz", "not-a-real-order", "some error")
    # No assertion needed beyond "did not raise" - the point is it's a no-op.


# --- orders_needing_fiscalization_retry --------------------------------------

def test_orders_needing_fiscalization_retry_lists_only_failed_ones(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    _save_confirmed_order(conn, "biz", "order-2")
    db.mark_order_fiscalization_failed(conn, "biz", "order-1", "boom")
    db.mark_order_fiscalized(conn, "biz", "order-2", {"ok": True})

    retry_queue = db.orders_needing_fiscalization_retry(conn, "biz")
    assert list(retry_queue["order_id"]) == ["order-1"]
    assert retry_queue.iloc[0]["fiscalization_error"] == "boom"


def test_orders_needing_fiscalization_retry_excludes_never_attempted_orders(conn):
    _save_confirmed_order(conn, "biz", "order-1")  # never fiscalized at all
    assert db.orders_needing_fiscalization_retry(conn, "biz").empty


def test_orders_needing_fiscalization_retry_is_scoped_by_business(conn):
    _save_confirmed_order(conn, "biz-a", "order-1")
    db.mark_order_fiscalization_failed(conn, "biz-a", "order-1", "boom")
    assert len(db.orders_needing_fiscalization_retry(conn, "biz-a")) == 1
    assert len(db.orders_needing_fiscalization_retry(conn, "biz-b")) == 0
