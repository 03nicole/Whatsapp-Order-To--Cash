"""
Persistence tests for Phase 7 ("warehouse fulfillment") - the
fulfilled_at/fulfillment_note migration on `orders` and the
fulfillable_orders/order_lines_for/mark_order_fulfilled helpers.
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


# --- migration -------------------------------------------------------------

def test_connect_adds_fulfillment_columns_to_a_pre_existing_orders_table(tmp_path):
    """Simulates a real business's database that already had orders in it
    before Phase 7 shipped - CREATE TABLE IF NOT EXISTS alone would never
    add these columns to an existing table."""
    db_path = tmp_path / "existing.db"

    # Connect once with the OLD schema shape (no fulfillment columns) to
    # simulate a database that predates Phase 7.
    old_conn = sqlite3.connect(db_path)
    old_conn.executescript("""
        CREATE TABLE orders (
            business TEXT NOT NULL, order_id TEXT NOT NULL, customer_name TEXT,
            customer_phone TEXT, placed_at TEXT NOT NULL, status TEXT NOT NULL,
            raw_message TEXT, invoice_id TEXT, flag_reason TEXT,
            PRIMARY KEY (business, order_id)
        );
    """)
    old_conn.execute(
        "INSERT INTO orders (business, order_id, placed_at, status) VALUES (?, ?, ?, ?)",
        ("biz", "old-order-1", "2026-01-01T00:00:00", "confirmed"),
    )
    old_conn.commit()
    old_conn.close()

    # Now open it through db.connect(), same as a real upgrade would.
    conn = db.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    assert "fulfilled_at" in columns
    assert "fulfillment_note" in columns
    # the pre-existing row survives the migration
    row = conn.execute("SELECT order_id FROM orders WHERE business = 'biz'").fetchone()
    assert row == ("old-order-1",)
    conn.close()


def test_connect_is_idempotent_on_an_already_migrated_database(conn, tmp_path):
    """Calling connect() twice (every request does this) must not error
    on ALTER TABLE ADD COLUMN for a column that's already there."""
    conn.close()
    conn2 = db.connect(tmp_path / "test.db")
    columns = {row[1] for row in conn2.execute("PRAGMA table_info(orders)").fetchall()}
    assert "fulfilled_at" in columns
    conn2.close()


# --- fulfillable_orders / order_lines_for / mark_order_fulfilled ----------

def test_fulfillable_orders_lists_confirmed_unfulfilled_orders(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    result = db.fulfillable_orders(conn, "biz")
    assert list(result["order_id"]) == ["order-1"]


def test_fulfillable_orders_excludes_flagged_orders(conn):
    flagged = {"order_id": "bad-1", "customer_name": "B", "customer_phone": "",
               "placed_at": "2026-01-01T10:00:00", "status": "flagged",
               "raw_message": "10 Guinness", "invoice_id": None, "flag_reason": "unknown_product"}
    db.save_order(conn, "biz", flagged, [])
    assert db.fulfillable_orders(conn, "biz").empty


def test_fulfillable_orders_excludes_already_fulfilled_orders(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    db.mark_order_fulfilled(conn, "biz", "order-1")
    assert db.fulfillable_orders(conn, "biz").empty


def test_fulfillable_orders_orders_oldest_first(conn):
    _save_confirmed_order(conn, "biz", "order-2", placed_at="2026-01-02T10:00:00")
    _save_confirmed_order(conn, "biz", "order-1", placed_at="2026-01-01T10:00:00")
    result = db.fulfillable_orders(conn, "biz")
    assert list(result["order_id"]) == ["order-1", "order-2"]


def test_fulfillable_orders_is_scoped_by_business(conn):
    _save_confirmed_order(conn, "biz-a", "order-1")
    assert len(db.fulfillable_orders(conn, "biz-a")) == 1
    assert len(db.fulfillable_orders(conn, "biz-b")) == 0


def test_order_lines_for_returns_the_picking_list(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    lines = db.order_lines_for(conn, "biz", "order-1")
    assert len(lines) == 1
    assert lines.iloc[0]["product_id"] == "COKE-24"
    assert lines.iloc[0]["quantity_requested"] == 10


def test_mark_order_fulfilled_sets_status_and_timestamp(conn):
    _save_confirmed_order(conn, "biz", "order-1")
    db.mark_order_fulfilled(conn, "biz", "order-1", note="picked by Joseph",
                             fulfilled_at="2026-01-02T09:00:00")

    row = conn.execute(
        "SELECT status, fulfilled_at, fulfillment_note FROM orders "
        "WHERE business = 'biz' AND order_id = 'order-1'",
    ).fetchone()
    assert row == ("fulfilled", "2026-01-02T09:00:00", "picked by Joseph")


def test_mark_order_fulfilled_only_affects_confirmed_orders(conn):
    """A flagged order can't be marked fulfilled through this path - it
    has no invoice/stock consumption behind it yet."""
    flagged = {"order_id": "bad-1", "customer_name": "B", "customer_phone": "",
               "placed_at": "2026-01-01T10:00:00", "status": "flagged",
               "raw_message": "10 Guinness", "invoice_id": None, "flag_reason": "unknown_product"}
    db.save_order(conn, "biz", flagged, [])
    db.mark_order_fulfilled(conn, "biz", "bad-1")

    row = conn.execute(
        "SELECT status, fulfilled_at FROM orders WHERE business = 'biz' AND order_id = 'bad-1'",
    ).fetchone()
    assert row == ("flagged", None)  # untouched
