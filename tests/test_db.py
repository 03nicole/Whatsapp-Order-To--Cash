from datetime import date, timedelta

import pandas as pd
import pytest

from reconciler import db
from reconciler.matcher import reconcile


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def _reconcile_result(make_invoices, make_momo, invoice_rows, momo_rows):
    return reconcile(make_invoices(invoice_rows), make_momo(momo_rows))


# --- connect / schema -----------------------------------------------------

def test_connect_creates_tables(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"invoices", "transactions"} <= tables


def test_fresh_db_has_no_known_state(conn):
    assert db.known_balances(conn, "biz") == {}
    assert db.known_transaction_ids(conn, "biz") == set()


# --- merge_persisted_balances / filter_new_transactions -------------------

def test_merge_persisted_balances_overrides_only_known_invoices(make_invoices):
    invoices = make_invoices([
        dict(invoice_id="INV-1", amount=1000),
        dict(invoice_id="INV-2", amount=2000),
    ])
    merged = db.merge_persisted_balances(invoices, {"INV-1": 400})
    balances = dict(zip(merged["invoice_id"], merged["balance"]))
    assert balances["INV-1"] == 400  # persisted balance wins
    assert balances["INV-2"] == 2000  # genuinely new invoice stays at amount


def test_merge_persisted_balances_does_not_mutate_input(make_invoices):
    invoices = make_invoices([dict(invoice_id="INV-1", amount=1000)])
    db.merge_persisted_balances(invoices, {"INV-1": 400})
    assert invoices.loc[0, "balance"] == 1000


def test_filter_new_transactions_drops_seen_ids(make_momo):
    momo = make_momo([
        dict(transaction_id="T1", amount=100),
        dict(transaction_id="T2", amount=200),
    ])
    new_rows, skipped = db.filter_new_transactions(momo, {"T1"})
    assert list(new_rows["transaction_id"]) == ["T2"]
    assert skipped == 1


# --- save_run / open_invoices: the core persistence promise ---------------

def test_save_run_persists_invoice_balances_and_transactions(conn, make_invoices, make_momo):
    result = _reconcile_result(
        make_invoices, make_momo,
        [dict(invoice_id="INV-1", customer_name="ABC", amount=1000)],
        [dict(transaction_id="T1", amount=400, reference="INV-1 payment")],
    )
    db.save_run(conn, result, "biz")

    assert db.known_balances(conn, "biz") == {"INV-1": 600}
    assert db.known_transaction_ids(conn, "biz") == {"T1"}


def test_reupload_of_full_invoice_file_does_not_undo_prior_partial_payment(
        conn, make_invoices, make_momo):
    """This is the whole premise of persistence per README: a business
    re-exports its FULL invoice list every time, including ones a previous
    run already partially paid down."""
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    momo_run1 = make_momo([dict(transaction_id="T1", amount=400, reference="INV-1 payment")])
    result1 = reconcile(invoices, momo_run1)
    db.save_run(conn, result1, "biz")
    assert db.known_balances(conn, "biz")["INV-1"] == 600

    # Second run: same full invoice file re-uploaded (balance resets to
    # amount at load time), merged against persisted balances before matching.
    invoices_reloaded = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    merged = db.merge_persisted_balances(invoices_reloaded, db.known_balances(conn, "biz"))
    assert merged.loc[0, "balance"] == 600  # not silently reset to 1000

    momo_run2 = make_momo([dict(transaction_id="T2", amount=600, reference="INV-1 payment")])
    result2 = reconcile(merged, momo_run2)
    db.save_run(conn, result2, "biz")
    assert db.known_balances(conn, "biz")["INV-1"] == 0


def test_overlapping_statement_export_is_not_double_counted(conn, make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    momo = make_momo([dict(transaction_id="T1", amount=1000, reference="INV-1 payment")])
    result = reconcile(invoices, momo)
    db.save_run(conn, result, "biz")

    seen_ids = db.known_transaction_ids(conn, "biz")
    new_rows, skipped = db.filter_new_transactions(momo, seen_ids)
    assert skipped == 1
    assert new_rows.empty


def test_open_invoices_excludes_paid_off_ones(conn, make_invoices, make_momo):
    result = _reconcile_result(
        make_invoices, make_momo,
        [
            dict(invoice_id="INV-1", customer_name="A", amount=1000, date="2026-01-01"),
            dict(invoice_id="INV-2", customer_name="B", amount=500, date="2026-01-02"),
        ],
        [dict(transaction_id="T1", amount=1000, reference="INV-1 payment")],
    )
    db.save_run(conn, result, "biz")
    open_df = db.open_invoices(conn, "biz")
    assert list(open_df["invoice_id"]) == ["INV-2"]


def test_save_run_is_scoped_by_business(conn, make_invoices, make_momo):
    result = _reconcile_result(
        make_invoices, make_momo,
        [dict(invoice_id="INV-1", customer_name="A", amount=1000)],
        [dict(transaction_id="T1", amount=1000, reference="INV-1 payment")],
    )
    db.save_run(conn, result, "biz-a")
    assert db.known_balances(conn, "biz-a") != {}
    assert db.known_balances(conn, "biz-b") == {}


# --- aging_report / aging_summary_by_customer ------------------------------

def test_aging_report_empty_db_returns_empty_frame(conn):
    df = db.aging_report(conn, "biz")
    assert df.empty
    assert "bucket" in df.columns


def test_aging_bucket_boundaries(conn, make_invoices, make_momo):
    as_of = date(2026, 6, 1)
    rows = [
        dict(invoice_id="INV-30", amount=100, date=(as_of - timedelta(days=30)).isoformat()),
        dict(invoice_id="INV-31", amount=100, date=(as_of - timedelta(days=31)).isoformat()),
        dict(invoice_id="INV-90", amount=100, date=(as_of - timedelta(days=90)).isoformat()),
        dict(invoice_id="INV-91", amount=100, date=(as_of - timedelta(days=91)).isoformat()),
    ]
    result = _reconcile_result(make_invoices, make_momo, rows, [])
    db.save_run(conn, result, "biz")

    aging_df = db.aging_report(conn, "biz", as_of=as_of)
    buckets = dict(zip(aging_df["invoice_id"], aging_df["bucket"]))
    assert buckets["INV-30"] == "0-30"
    assert buckets["INV-31"] == "31-60"
    assert buckets["INV-90"] == "61-90"
    assert buckets["INV-91"] == "90+"


def test_aging_bucket_handles_a_future_dated_invoice_as_0_30_not_90_plus(conn, make_invoices, make_momo):
    """Regression test: found live via the analytics dashboard, not by
    inspection. A negative days-outstanding (clock/timezone skew, or a
    literal future-dated invoice) used to fall through _bucket()'s loop
    entirely and land in the last bucket by accident - exactly backwards,
    since a not-yet-due invoice is the LEAST overdue thing on the books."""
    as_of = date(2026, 6, 1)
    rows = [dict(invoice_id="INV-FUTURE", amount=100,
                 date=(as_of + timedelta(days=1)).isoformat())]
    result = _reconcile_result(make_invoices, make_momo, rows, [])
    db.save_run(conn, result, "biz")

    aging_df = db.aging_report(conn, "biz", as_of=as_of)
    assert aging_df.loc[0, "days_outstanding"] == -1
    assert aging_df.loc[0, "bucket"] == "0-30"


def test_aging_summary_by_customer_rolls_up_and_zero_fills_empty_buckets(conn, make_invoices, make_momo):
    as_of = date(2026, 6, 1)
    rows = [
        dict(invoice_id="INV-1", customer_name="ABC", amount=100, date=as_of.isoformat()),
        dict(invoice_id="INV-2", customer_name="ABC", amount=50,
             date=(as_of - timedelta(days=95)).isoformat()),
    ]
    result = _reconcile_result(make_invoices, make_momo, rows, [])
    db.save_run(conn, result, "biz")

    aging_df = db.aging_report(conn, "biz", as_of=as_of)
    summary = db.aging_summary_by_customer(aging_df)
    row = summary.iloc[0]
    assert row["customer_name"] == "ABC"
    assert row["total_owed"] == 150
    assert row["0-30"] == 100
    assert row["90+"] == 50
    assert row["31-60"] == 0  # empty bucket shows as 0, not omitted
    assert row["61-90"] == 0
