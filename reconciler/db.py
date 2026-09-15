"""
SQLite persistence — Phase 2 ("accounts receivable").

The reconciliation engine itself (loaders.py/matcher.py) stays stateless and
file-in/file-out on purpose — this module is what sits on top of it across
runs. Two problems only a real data store can solve:

  1. A business re-exports its FULL invoice list every time, including ones
     a previous run already partially paid down. load_invoices() always sets
     balance = amount, so without merging in the persisted balance first,
     re-running reconcile() on the same invoice file would silently undo
     every prior partial payment.
  2. "Who owes me money right now" (accounts receivable) is only meaningful
     if invoice balances accumulate across runs instead of resetting to a
     blank slate each time — that's the whole premise of an aging view.

SQLite, not Postgres: this is one business's (or a few pilot businesses',
scoped by the `business` column) data on a laptop/VPS, not a multi-tenant
service — no reason to run a database server for that yet.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    business        TEXT NOT NULL,
    invoice_id      TEXT NOT NULL,
    customer_name   TEXT,
    customer_phone  TEXT,
    amount          REAL NOT NULL,
    date            TEXT NOT NULL,
    balance         REAL NOT NULL,
    PRIMARY KEY (business, invoice_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    business         TEXT NOT NULL,
    transaction_id   TEXT NOT NULL,
    date             TEXT NOT NULL,
    amount           REAL NOT NULL,
    sender_name      TEXT,
    sender_phone     TEXT,
    reference        TEXT,
    matched_invoice  TEXT,
    customer         TEXT,
    outcome          TEXT NOT NULL,   -- matched | partial | needs_review | unmatched
    match_rule       TEXT,
    details          TEXT,            -- free text: remaining balance, candidate invoices, etc.
    processed_at     TEXT NOT NULL,
    PRIMARY KEY (business, transaction_id)
);

-- Phase 5 ("order capture"): a single-warehouse product catalog + stock
-- ledger, and the orders/order_lines that get parsed out of an incoming
-- WhatsApp message. See reconciler/orders.py. An order that resolves
-- cleanly writes a row into `invoices` above through the same path a
-- manually-uploaded invoice file would - this schema produces invoices,
-- it doesn't duplicate them.
CREATE TABLE IF NOT EXISTS products (
    business          TEXT NOT NULL,
    product_id        TEXT NOT NULL,  -- short code the catalog/order parser matches on, e.g. "COKE-24"
    name               TEXT NOT NULL,
    unit               TEXT,          -- e.g. box, crate, each
    unit_price         REAL NOT NULL,
    quantity_on_hand   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (business, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
    business        TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    customer_name   TEXT,
    customer_phone  TEXT,
    placed_at       TEXT NOT NULL,
    status          TEXT NOT NULL,   -- pending | confirmed | flagged | fulfilled
    raw_message     TEXT,            -- original WhatsApp text, kept for audit
    invoice_id      TEXT,            -- set once this order generates an invoice
    flag_reason     TEXT,            -- why a 'flagged' order needs a human, if any
    PRIMARY KEY (business, order_id)
);

CREATE TABLE IF NOT EXISTS order_lines (
    business             TEXT NOT NULL,
    order_id             TEXT NOT NULL,
    line_no              INTEGER NOT NULL,
    product_id           TEXT,             -- NULL if this line couldn't be resolved to a product
    raw_text             TEXT NOT NULL,     -- the original segment of the message this line came from
    quantity_requested   INTEGER,
    unit_price           REAL,              -- snapshot at order time, not a live catalog reference
    line_total           REAL,
    resolution           TEXT NOT NULL,     -- exact_code | unique_name | ambiguous | unknown_product
    PRIMARY KEY (business, order_id, line_no)
);

-- Phase 6 ("stock management"): every change to quantity_on_hand outside
-- a full catalog re-import goes through adjust_stock() below and is
-- logged here - order consumption (reason 'order:<order_id>') and manual
-- corrections (received stock, recount) alike. Same "every automated
-- decision is traceable" principle as match_rule on transactions, applied
-- to stock instead of money.
CREATE TABLE IF NOT EXISTS stock_adjustments (
    business        TEXT NOT NULL,
    adjustment_id   TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    delta           INTEGER NOT NULL,
    reason          TEXT,
    adjusted_at     TEXT NOT NULL,
    PRIMARY KEY (business, adjustment_id)
);
"""

# Aging buckets, in days-outstanding order. Standard AR convention.
AGING_BUCKETS = [(0, 30, "0-30"), (31, 60, "31-60"), (61, 90, "61-90"), (91, None, "90+")]

DETAIL_FIELDS = ["invoice_total", "remaining_balance", "invoice_balance", "candidate_invoices"]


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Adds any of `columns` (name -> SQL type) missing from `table`, in
    one pass. CREATE TABLE IF NOT EXISTS only helps on a brand-new
    database - a business's existing reconciliation.db already has rows
    in `table`, so a new column added to SCHEMA's CREATE TABLE text would
    silently never apply to it. This is the migration path used instead
    whenever a later phase needs to add columns to a table earlier phases
    already created."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, coltype in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Phase 7 ("warehouse fulfillment"): added after `orders` already
    # shipped in Phase 5, so these go through _ensure_columns rather than
    # SCHEMA's CREATE TABLE, which a real business's existing database
    # would just skip.
    _ensure_columns(conn, "orders", {"fulfilled_at": "TEXT", "fulfillment_note": "TEXT"})
    conn.commit()
    return conn


def known_balances(conn: sqlite3.Connection, business: str) -> dict[str, float]:
    """invoice_id -> persisted balance, for every invoice already on record
    for this business (from any previous run)."""
    rows = conn.execute(
        "SELECT invoice_id, balance FROM invoices WHERE business = ?", (business,)
    ).fetchall()
    return dict(rows)


def known_businesses(conn: sqlite3.Connection) -> list[str]:
    """Every distinct business name with at least one persisted run - lets a
    UI offer a dropdown instead of asking the user to remember/retype the
    exact string they used last time."""
    rows = conn.execute(
        "SELECT DISTINCT business FROM invoices "
        "UNION SELECT DISTINCT business FROM transactions ORDER BY business"
    ).fetchall()
    return [r[0] for r in rows]


def known_transaction_ids(conn: sqlite3.Connection, business: str) -> set[str]:
    rows = conn.execute(
        "SELECT transaction_id FROM transactions WHERE business = ?", (business,)
    ).fetchall()
    return {r[0] for r in rows}


def merge_persisted_balances(invoices_df: pd.DataFrame, balances: dict[str, float]) -> pd.DataFrame:
    """Overrides `balance` with the persisted value for any invoice_id already
    known to the DB, leaving genuinely new invoices at balance == amount.
    Does not mutate `invoices_df`."""
    merged = invoices_df.copy()
    known_mask = merged["invoice_id"].isin(balances)
    merged.loc[known_mask, "balance"] = merged.loc[known_mask, "invoice_id"].map(balances)
    return merged


def combine_with_open_invoices(conn: sqlite3.Connection, business: str,
                                invoices_df: pd.DataFrame) -> pd.DataFrame:
    """merge_persisted_balances() only overrides the balance of invoices
    already present in `invoices_df` - it can't surface an invoice that
    exists purely in the database with no accompanying upload, which is
    exactly what an order-generated invoice is (see orders.py /
    record_order_invoice). Without this, a business using WhatsApp order
    capture would have to keep re-uploading a matching invoice file for
    invoices that never came from a file in the first place, just so a
    later MoMo statement could be matched against them.

    Applies the usual persisted-balance override, then appends any
    currently-open invoice this business has on record that isn't part
    of `invoices_df` at all. Does not mutate `invoices_df`."""
    merged = merge_persisted_balances(invoices_df, known_balances(conn, business))
    already_present = set(merged["invoice_id"])
    other_open = open_invoices(conn, business)
    extra = other_open[~other_open["invoice_id"].isin(already_present)]
    if extra.empty:
        return merged
    return pd.concat([merged, extra], ignore_index=True)


def filter_new_transactions(momo_df: pd.DataFrame, seen_ids: set[str]) -> tuple[pd.DataFrame, int]:
    """Drops rows whose transaction_id has already been processed in a prior
    run for this business, so re-sending an overlapping statement export
    doesn't double-count a payment. Returns (new_rows, skipped_count)."""
    is_new = ~momo_df["transaction_id"].isin(seen_ids)
    return momo_df[is_new].copy(), int((~is_new).sum())


def _row_details(row: dict) -> str | None:
    parts = [f"{f}={row[f]}" for f in DETAIL_FIELDS if f in row and pd.notna(row[f])]
    return "; ".join(parts) if parts else None


def save_run(conn: sqlite3.Connection, results: dict[str, pd.DataFrame], business: str,
             processed_at: str | None = None) -> None:
    """Persists one reconcile() call: the resulting invoice balances (matched
    invoices included, since `all_invoices` — unlike `open_invoices` — still
    has them at balance 0) and every transaction that was actually processed,
    tagged with its outcome."""
    processed_at = processed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")

    inv = results["all_invoices"]
    conn.executemany(
        """INSERT INTO invoices (business, invoice_id, customer_name, customer_phone,
                                  amount, date, balance)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (business, invoice_id) DO UPDATE SET
               customer_name = excluded.customer_name,
               customer_phone = excluded.customer_phone,
               amount = excluded.amount,
               date = excluded.date,
               balance = excluded.balance""",
        [(business, r.invoice_id, r.customer_name, r.customer_phone,
          float(r.amount), r.date.date().isoformat(), float(r.balance))
         for r in inv.itertuples()],
    )

    txn_rows = []
    for outcome in ("matched", "partial", "needs_review", "unmatched"):
        df = results[outcome]
        for r in df.to_dict(orient="records"):
            txn_rows.append((
                business, r["transaction_id"], pd.Timestamp(r["date"]).date().isoformat(),
                float(r["amount"]), r.get("sender_name"), r.get("sender_phone"),
                r.get("reference"), r.get("matched_invoice"), r.get("customer"),
                outcome, r.get("match_rule"), _row_details(r), processed_at,
            ))
    conn.executemany(
        """INSERT INTO transactions (business, transaction_id, date, amount, sender_name,
                                      sender_phone, reference, matched_invoice, customer,
                                      outcome, match_rule, details, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (business, transaction_id) DO UPDATE SET
               outcome = excluded.outcome,
               match_rule = excluded.match_rule,
               details = excluded.details,
               processed_at = excluded.processed_at""",
        txn_rows,
    )
    conn.commit()


def open_invoices(conn: sqlite3.Connection, business: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT invoice_id, customer_name, customer_phone, amount, date, balance "
        "FROM invoices WHERE business = ? AND balance > 0 ORDER BY date",
        conn, params=(business,),
    )
    # Not parse_dates= at the query level: a business whose invoices all
    # happen to share one "shape" (e.g. every invoice this run came from
    # a WhatsApp order) can get a column pandas infers as uniformly
    # tz-aware or uniformly naive depending on what's in it, and mixing
    # that with a differently-shaped value elsewhere raises or silently
    # NaTs rather than comparing cleanly. format="mixed" + utc=True +
    # tz_localize(None) normalizes any mix of "2026-09-01" (a manually-
    # uploaded invoice's date-only string) and a full ISO timestamp to
    # one consistent naive dtype regardless of what's actually present.
    df["date"] = pd.to_datetime(df["date"], format="mixed", utc=True).dt.tz_localize(None)
    return df


def _bucket(days: int) -> str:
    # A negative day count (clock/timezone skew between when an invoice's
    # date was stamped and when as_of is evaluated, or a literal future-
    # dated invoice) isn't matched by any (lo, hi) range above, and used
    # to fall through the loop to the *last* bucket ("90+") by accident -
    # exactly backwards, since a not-yet-due invoice is the least
    # overdue thing on the books, not the most. Clamp to 0 first: found
    # live via the analytics dashboard (Phase 9), not by inspection - a
    # demo invoice from earlier the same day as `as_of` was showing as
    # "90+ days overdue" instead of "0-30".
    days = max(days, 0)
    for lo, hi, label in AGING_BUCKETS:
        if hi is None or days <= hi:
            if days >= lo:
                return label
    return AGING_BUCKETS[-1][2]


def aging_report(conn: sqlite3.Connection, business: str, as_of: date | None = None) -> pd.DataFrame:
    """One row per open invoice: days outstanding + aging bucket, as of `as_of`
    (default today). This is what a pilot business would check instead of
    maintaining their own 'who owes me money' spreadsheet."""
    as_of = as_of or date.today()
    df = open_invoices(conn, business)
    if df.empty:
        df["days_outstanding"] = pd.Series(dtype=int)
        df["bucket"] = pd.Series(dtype=str)
        return df
    df["days_outstanding"] = (pd.Timestamp(as_of) - df["date"]).dt.days
    df["bucket"] = df["days_outstanding"].apply(_bucket)
    return df.sort_values("days_outstanding", ascending=False).reset_index(drop=True)


def save_catalog(conn: sqlite3.Connection, catalog_df: pd.DataFrame, business: str) -> None:
    """Upserts a product catalog (product_id, name, unit, unit_price,
    quantity_on_hand). Re-importing the same catalog later updates price/
    stock rather than duplicating products - same upsert-by-business-scoped-
    key pattern as save_run() uses for invoices."""
    conn.executemany(
        """INSERT INTO products (business, product_id, name, unit, unit_price, quantity_on_hand)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (business, product_id) DO UPDATE SET
               name = excluded.name,
               unit = excluded.unit,
               unit_price = excluded.unit_price,
               quantity_on_hand = excluded.quantity_on_hand""",
        [(business, r.product_id, r.name, r.unit, float(r.unit_price), int(r.quantity_on_hand))
         for r in catalog_df.itertuples()],
    )
    conn.commit()


def get_catalog(conn: sqlite3.Connection, business: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT product_id, name, unit, unit_price, quantity_on_hand "
        "FROM products WHERE business = ? ORDER BY product_id",
        conn, params=(business,),
    )


def adjust_stock(conn: sqlite3.Connection, business: str, product_id: str, delta: int,
                  reason: str | None = None, adjusted_at: str | None = None) -> bool:
    """Applies `delta` (negative to consume stock) to one product's
    quantity_on_hand and logs it to stock_adjustments - used both when an
    order is confirmed (reason `order:<order_id>`) and for a manual
    correction/stock receipt (reason from the person making it). Every
    change to stock outside a full catalog re-import goes through here,
    so stock_adjustments is a complete audit trail, not a partial one.

    Returns False (and logs nothing) if `product_id` doesn't exist for
    this business, rather than requiring the caller to check existence
    separately first - the UPDATE's own WHERE clause is the one place
    that condition needs to live."""
    cursor = conn.execute(
        "UPDATE products SET quantity_on_hand = quantity_on_hand + ? "
        "WHERE business = ? AND product_id = ?",
        (delta, business, product_id),
    )
    if cursor.rowcount == 0:
        return False
    adjusted_at = adjusted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO stock_adjustments (business, adjustment_id, product_id, delta, reason, adjusted_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (business, uuid.uuid4().hex, product_id, delta, reason, adjusted_at),
    )
    conn.commit()
    return True


def stock_history(conn: sqlite3.Connection, business: str, product_id: str | None = None) -> pd.DataFrame:
    """Every logged stock_adjustments row for a business, newest first -
    optionally filtered to one product. Read-only view onto adjust_stock's
    audit trail. Ties on adjusted_at (timespec="seconds" - two changes in
    the same second are entirely plausible) break on rowid, SQLite's
    implicit insertion-order column, so "newest first" stays deterministic
    instead of depending on unspecified tie behavior."""
    query = ("SELECT product_id, delta, reason, adjusted_at FROM stock_adjustments "
             "WHERE business = ?")
    params: list = [business]
    if product_id:
        query += " AND product_id = ?"
        params.append(product_id)
    query += " ORDER BY adjusted_at DESC, rowid DESC"
    return pd.read_sql_query(query, conn, params=params)


def save_order(conn: sqlite3.Connection, business: str, order: dict, lines: list[dict]) -> None:
    """Persists one parsed order and its lines. `order` and each entry of
    `lines` are plain dicts shaped like the `orders`/`order_lines` columns -
    see reconciler/orders.py, which builds them."""
    conn.execute(
        """INSERT INTO orders (business, order_id, customer_name, customer_phone,
                                placed_at, status, raw_message, invoice_id, flag_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (business, order_id) DO UPDATE SET
               status = excluded.status,
               invoice_id = excluded.invoice_id,
               flag_reason = excluded.flag_reason""",
        (business, order["order_id"], order.get("customer_name"), order.get("customer_phone"),
         order["placed_at"], order["status"], order.get("raw_message"),
         order.get("invoice_id"), order.get("flag_reason")),
    )
    conn.executemany(
        """INSERT INTO order_lines (business, order_id, line_no, product_id, raw_text,
                                     quantity_requested, unit_price, line_total, resolution)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (business, order_id, line_no) DO UPDATE SET
               product_id = excluded.product_id,
               quantity_requested = excluded.quantity_requested,
               unit_price = excluded.unit_price,
               line_total = excluded.line_total,
               resolution = excluded.resolution""",
        [(business, order["order_id"], i, l.get("product_id"), l["raw_text"],
          l.get("quantity_requested"), l.get("unit_price"), l.get("line_total"), l["resolution"])
         for i, l in enumerate(lines)],
    )
    conn.commit()


def record_order_invoice(conn: sqlite3.Connection, business: str, invoice: dict) -> None:
    """Writes one invoice generated from a confirmed order into the SAME
    `invoices` table a manually-uploaded invoice file lands in - this is
    the whole point of Phase 5: a new invoice *producer*, not a parallel
    reconciliation path. `invoice` is a plain dict with invoice_id,
    customer_name, customer_phone, amount, date (ISO string)."""
    conn.execute(
        """INSERT INTO invoices (business, invoice_id, customer_name, customer_phone,
                                  amount, date, balance)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (business, invoice_id) DO NOTHING""",
        (business, invoice["invoice_id"], invoice.get("customer_name"),
         invoice.get("customer_phone"), float(invoice["amount"]), invoice["date"],
         float(invoice["amount"])),
    )
    conn.commit()


def flagged_orders(conn: sqlite3.Connection, business: str) -> pd.DataFrame:
    """Orders that couldn't be auto-confirmed (ambiguous/unknown product,
    insufficient stock) - the order-capture equivalent of the
    reconciliation engine's needs_review sheet. A human resolves these."""
    return pd.read_sql_query(
        "SELECT order_id, customer_name, customer_phone, placed_at, raw_message, flag_reason "
        "FROM orders WHERE business = ? AND status = 'flagged' ORDER BY placed_at",
        conn, params=(business,),
    )


def fulfillable_orders(conn: sqlite3.Connection, business: str) -> pd.DataFrame:
    """Confirmed orders (invoice already generated, stock already
    consumed - see orders.py) that haven't been marked fulfilled yet.
    Phase 7's picking list: oldest first, same FIFO convention used
    elsewhere in this module (aging, duplicate-invoice resolution)."""
    return pd.read_sql_query(
        "SELECT order_id, customer_name, customer_phone, placed_at, invoice_id "
        "FROM orders WHERE business = ? AND status = 'confirmed' AND fulfilled_at IS NULL "
        "ORDER BY placed_at",
        conn, params=(business,),
    )


def order_lines_for(conn: sqlite3.Connection, business: str, order_id: str) -> pd.DataFrame:
    """The product/quantity lines for one order - what a warehouse
    picker actually needs to read off a picking list."""
    return pd.read_sql_query(
        "SELECT product_id, quantity_requested, line_total FROM order_lines "
        "WHERE business = ? AND order_id = ? ORDER BY line_no",
        conn, params=(business, order_id),
    )


def mark_order_fulfilled(conn: sqlite3.Connection, business: str, order_id: str,
                          note: str | None = None, fulfilled_at: str | None = None) -> bool:
    """Records that a confirmed order has been picked/packed. Deliberately
    one step, not a multi-stage picking/packing workflow - per
    ROADMAP.md's own Phase 7 entry, there's no real operational data yet
    to design finer-grained stages against, so this names the one thing
    that's actually known to matter (has it left the warehouse-readiness
    stage or not) rather than inventing states nobody's validated.

    Returns False if `order_id` isn't a confirmed, not-yet-fulfilled
    order for this business - the UPDATE's own WHERE clause is the one
    place that eligibility rule needs to live, rather than callers
    re-deriving it via a separate fulfillable_orders() lookup first."""
    fulfilled_at = fulfilled_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = conn.execute(
        "UPDATE orders SET status = 'fulfilled', fulfilled_at = ?, fulfillment_note = ? "
        "WHERE business = ? AND order_id = ? AND status = 'confirmed'",
        (fulfilled_at, note, business, order_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def aging_summary_by_customer(aging_df: pd.DataFrame) -> pd.DataFrame:
    """Aging report rolled up per customer — total owed and a column per
    bucket. Empty buckets show as 0 rather than being omitted."""
    if aging_df.empty:
        return pd.DataFrame(columns=["customer_name", "total_owed"] + [b[2] for b in AGING_BUCKETS])
    pivot = aging_df.pivot_table(index="customer_name", columns="bucket", values="balance",
                                  aggfunc="sum", fill_value=0)
    for _, _, label in AGING_BUCKETS:
        if label not in pivot.columns:
            pivot[label] = 0.0
    pivot = pivot[[b[2] for b in AGING_BUCKETS]]
    pivot.insert(0, "total_owed", pivot.sum(axis=1))
    return pivot.reset_index().sort_values("total_owed", ascending=False).reset_index(drop=True)


# --- Phase 9 ("analytics") -------------------------------------------------
#
# Deliberately built from numbers this tool has always tracked and already
# treats as meaningful - the same matched/partial/needs_review/unmatched
# breakdown report.py's Summary sheet has shown since Phase 1, and the same
# aging buckets the aging view already computes - rather than inventing new
# metrics with no real usage behind them yet. See docs/ROADMAP.md's Phase 9
# entry: this was built ahead of its own stated validation gate (no real
# reconciled data exists yet, only demo data), a deliberate accepted risk.

def reconciliation_summary(conn: sqlite3.Connection, business: str) -> dict:
    """Transaction counts and total amounts by outcome, across every
    persisted reconcile() run for this business - the live-dashboard
    version of the Excel Summary sheet's own breakdown."""
    rows = conn.execute(
        "SELECT outcome, COUNT(*), COALESCE(SUM(amount), 0) FROM transactions "
        "WHERE business = ? GROUP BY outcome",
        (business,),
    ).fetchall()
    by_outcome = {outcome: {"count": count, "amount": amount} for outcome, count, amount in rows}
    for outcome in ("matched", "partial", "needs_review", "unmatched"):
        by_outcome.setdefault(outcome, {"count": 0, "amount": 0.0})
    total_count = sum(v["count"] for v in by_outcome.values())
    total_amount = sum(v["amount"] for v in by_outcome.values())
    match_rate = (by_outcome["matched"]["count"] / total_count) if total_count else 0.0
    return {"by_outcome": by_outcome, "total_count": total_count,
            "total_amount": total_amount, "match_rate": match_rate}


def aging_bucket_totals(aging_df: pd.DataFrame) -> dict:
    """Total outstanding balance per aging bucket, in bucket order,
    zero-filled for empty buckets - the same rollup
    aging_summary_by_customer() does per-customer, collapsed to one
    number per bucket for a dashboard tile."""
    totals = {label: 0.0 for _, _, label in AGING_BUCKETS}
    if not aging_df.empty:
        totals.update(aging_df.groupby("bucket")["balance"].sum().to_dict())
    return {label: totals[label] for _, _, label in AGING_BUCKETS}


def order_summary(conn: sqlite3.Connection, business: str) -> dict:
    """Order counts by status, and the total invoice value of every
    order that resolved automatically (confirmed or since fulfilled) -
    the order-capture equivalent of reconciliation_summary()."""
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM orders WHERE business = ? GROUP BY status",
        (business,),
    ).fetchall()
    by_status = {status: count for status, count in rows}
    for status in ("confirmed", "flagged", "fulfilled"):
        by_status.setdefault(status, 0)
    confirmed_value = conn.execute(
        "SELECT COALESCE(SUM(i.amount), 0) FROM orders o "
        "JOIN invoices i ON i.business = o.business AND i.invoice_id = o.invoice_id "
        "WHERE o.business = ? AND o.status IN ('confirmed', 'fulfilled')",
        (business,),
    ).fetchone()[0]
    return {"by_status": by_status, "confirmed_value": confirmed_value}


def lowest_stock(conn: sqlite3.Connection, business: str, limit: int = 5) -> pd.DataFrame:
    """The `limit` products with the least quantity_on_hand for this
    business. Deliberately not filtered by an invented "low stock"
    threshold - there's no real data yet on what a sensible reorder
    point looks like for any given product - just ranked so a human can
    judge, the same "surface the number, let a person decide" approach
    the reconciliation waterfall itself uses whenever it's unsure."""
    return pd.read_sql_query(
        "SELECT product_id, name, quantity_on_hand FROM products "
        "WHERE business = ? ORDER BY quantity_on_hand ASC LIMIT ?",
        conn, params=(business, limit),
    )
