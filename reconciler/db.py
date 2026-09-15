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
"""

# Aging buckets, in days-outstanding order. Standard AR convention.
AGING_BUCKETS = [(0, 30, "0-30"), (31, 60, "31-60"), (61, 90, "61-90"), (91, None, "90+")]

DETAIL_FIELDS = ["invoice_total", "remaining_balance", "invoice_balance", "candidate_invoices"]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def known_balances(conn: sqlite3.Connection, business: str) -> dict[str, float]:
    """invoice_id -> persisted balance, for every invoice already on record
    for this business (from any previous run)."""
    rows = conn.execute(
        "SELECT invoice_id, balance FROM invoices WHERE business = ?", (business,)
    ).fetchall()
    return dict(rows)


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
        conn, params=(business,), parse_dates=["date"],
    )
    return df


def _bucket(days: int) -> str:
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
