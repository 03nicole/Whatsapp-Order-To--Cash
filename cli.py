#!/usr/bin/env python3
"""
Usage:
    python cli.py reconcile invoices.csv momo_statement.csv --business "ABC Distributors"
    python cli.py aging --business "ABC Distributors"

Accepts .csv or .xlsx for both inputs. See reconciler/loaders.py if a file's
columns aren't recognized — add the real header name to the alias list there.

Reconciliation results persist to a local SQLite file (--db, default
reconciliation.db) so invoice balances and processed transactions accumulate
across runs instead of resetting every time — that's what makes `aging`
possible without re-uploading anything.
"""

import argparse
import sys

from reconciler import load_invoices, load_momo_statement, reconcile
from reconciler import db
from reconciler.report import write_report, write_aging_report


def cmd_reconcile(args):
    print(f"Loading invoices from {args.invoices} ...")
    invoices = load_invoices(args.invoices)
    print(f"  {len(invoices)} invoice(s) loaded, total value "
          f"K{invoices['amount'].sum():,.2f}")

    print(f"Loading MoMo statement from {args.momo_statement} ...")
    momo = load_momo_statement(args.momo_statement)
    print(f"  {len(momo)} transaction(s) loaded, total value "
          f"K{momo['amount'].sum():,.2f}")

    conn = db.connect(args.db)
    invoices = db.combine_with_open_invoices(conn, args.business, invoices)
    momo, skipped = db.filter_new_transactions(momo, db.known_transaction_ids(conn, args.business))
    if skipped:
        print(f"  Skipped {skipped} transaction(s) already processed in a previous run.")

    print("Running matching engine ...")
    results = reconcile(invoices, momo, date_window_days=args.date_window)

    print()
    print("  Matched:      ", len(results["matched"]))
    print("  Partial:      ", len(results["partial"]))
    print("  Needs review: ", len(results["needs_review"]))
    print("  Unmatched:    ", len(results["unmatched"]))
    print("  Open invoices remaining:", len(results["open_invoices"]))

    write_report(results, args.output, business_name=args.business)
    db.save_run(conn, results, args.business)
    conn.close()

    print(f"\nReport written to {args.output}")
    print(f"Persisted to {args.db} (business={args.business!r}) - "
          f"run 'python cli.py aging --business \"{args.business}\" --db {args.db}' "
          f"for the current accounts-receivable view.")
    print("Run scripts/office/recalc.py (from the xlsx skill) on it if you need "
          "the formulas to show cached values before opening it outside Excel.")


def cmd_aging(args):
    conn = db.connect(args.db)
    aging_df = db.aging_report(conn, args.business)
    conn.close()

    if aging_df.empty:
        print(f"No open invoices on record for business={args.business!r} in {args.db}. "
              f"Run 'python cli.py reconcile ...' at least once first.")
        return

    summary_df = db.aging_summary_by_customer(aging_df)
    print(f"Accounts receivable - {args.business or '(no business name)'}")
    print(summary_df.to_string(index=False, float_format=lambda v: f"K{v:,.2f}"))
    print(f"\nTotal outstanding: K{aging_df['balance'].sum():,.2f} "
          f"across {len(aging_df)} open invoice(s)")

    write_aging_report(aging_df, summary_df, args.output, business_name=args.business)
    print(f"\nFull aging report written to {args.output}")


def main():
    parser = argparse.ArgumentParser(description="Reconcile MoMo payments against invoices.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("reconcile", help="Run the matching engine on an invoice + statement pair")
    p_rec.add_argument("invoices", help="Path to invoices file (.csv or .xlsx)")
    p_rec.add_argument("momo_statement", help="Path to MoMo statement file (.csv or .xlsx)")
    p_rec.add_argument("--output", default="reconciliation_report.xlsx",
                        help="Where to write the Excel report")
    p_rec.add_argument("--business", default="default", help="Business name - scopes persisted data")
    p_rec.add_argument("--date-window", type=int, default=45,
                        help="Days to look back for amount-only matches (default: 45)")
    p_rec.add_argument("--db", default="reconciliation.db", help="SQLite file to persist results to")
    p_rec.set_defaults(func=cmd_reconcile)

    p_age = sub.add_parser("aging", help="Print/export the current accounts-receivable aging view")
    p_age.add_argument("--business", default="default", help="Business name - scopes persisted data")
    p_age.add_argument("--db", default="reconciliation.db", help="SQLite file to read from")
    p_age.add_argument("--output", default="aging_report.xlsx", help="Where to write the Excel report")
    p_age.set_defaults(func=cmd_aging)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
