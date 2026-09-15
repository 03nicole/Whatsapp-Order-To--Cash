#!/usr/bin/env python3
"""
Local web UI for the reconciliation engine - wraps cli.py's two commands
(reconcile, aging) in a browser form instead of a terminal. No new matching
logic lives here; this is purely upload -> reconciler.* -> render/download,
same as cli.py is argparse -> reconciler.* -> print/write.

Run with:
    pip install -r requirements-web.txt
    python app.py
then open http://127.0.0.1:5000

This is meant to be run locally by whoever's doing the reconciliation (you,
or on a pilot business's own machine) - not deployed as a public-facing
service. It shares reconciliation.db with the CLI (same default path), so
either interface can be used interchangeably against the same data.
"""

from __future__ import annotations

import os
import secrets
import tempfile
import uuid
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from reconciler import load_invoices, load_momo_statement, reconcile
from reconciler import db
from reconciler.report import write_report, write_aging_report

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RECONCILIATION_DB", APP_DIR / "reconciliation.db"))
UPLOAD_DIR = Path(tempfile.gettempdir()) / "reconciliation-engine-uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)

# token -> (path, friendly download name), for the download route. In-memory
# and per-process: fine for a tool one person runs locally, not meant to
# survive a restart.
_downloads: dict[str, tuple[Path, str]] = {}


def _save_upload(file_storage) -> Path:
    """Saves an uploaded file to a private temp path with a random name but
    the original extension (loaders.py picks its reader from the suffix).
    Raises ValueError if the extension isn't one we accept."""
    original_name = secure_filename(file_storage.filename or "")
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"'{file_storage.filename}' isn't a .csv or .xlsx file. "
            f"Export your data to one of those formats first."
        )
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    file_storage.save(dest)
    return dest


def _register_download(path: Path, friendly_name: str) -> str:
    token = uuid.uuid4().hex
    _downloads[token] = (path, friendly_name)
    return token


@app.route("/")
def index():
    conn = db.connect(DB_PATH)
    businesses = db.known_businesses(conn)
    conn.close()
    return render_template("index.html", businesses=businesses)


@app.route("/reconcile", methods=["POST"])
def do_reconcile():
    business = request.form.get("business", "").strip() or "default"
    date_window = int(request.form.get("date_window") or 45)

    invoices_file = request.files.get("invoices")
    momo_file = request.files.get("momo_statement")
    if not invoices_file or not invoices_file.filename:
        flash("Choose an invoices file.")
        return redirect(url_for("index"))
    if not momo_file or not momo_file.filename:
        flash("Choose a MoMo statement file.")
        return redirect(url_for("index"))

    try:
        invoices_path = _save_upload(invoices_file)
        momo_path = _save_upload(momo_file)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("index"))

    try:
        invoices = load_invoices(invoices_path)
        momo = load_momo_statement(momo_path)
    except ValueError as exc:
        flash(f"Couldn't read that file: {exc}")
        return redirect(url_for("index"))
    finally:
        invoices_path.unlink(missing_ok=True)
        momo_path.unlink(missing_ok=True)

    conn = db.connect(DB_PATH)
    invoices = db.merge_persisted_balances(invoices, db.known_balances(conn, business))
    momo, skipped = db.filter_new_transactions(momo, db.known_transaction_ids(conn, business))

    results = reconcile(invoices, momo, date_window_days=date_window)

    report_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.xlsx"
    write_report(results, report_path, business_name=business)
    db.save_run(conn, results, business)
    conn.close()

    token = _register_download(report_path, f"reconciliation_{business}.xlsx")
    summary = {
        "business": business,
        "matched": len(results["matched"]),
        "partial": len(results["partial"]),
        "needs_review": len(results["needs_review"]),
        "unmatched": len(results["unmatched"]),
        "open_invoices": len(results["open_invoices"]),
        "skipped": skipped,
    }
    return render_template("reconcile_result.html", summary=summary, token=token)


@app.route("/aging")
def aging():
    business = request.args.get("business", "").strip()
    conn = db.connect(DB_PATH)
    businesses = db.known_businesses(conn)

    if not business:
        conn.close()
        return render_template("aging.html", businesses=businesses, business=None)

    aging_df = db.aging_report(conn, business)
    if aging_df.empty:
        conn.close()
        flash(f"No open invoices on record for '{business}'. Run a reconcile first.")
        return redirect(url_for("index"))

    summary_df = db.aging_summary_by_customer(aging_df)
    report_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.xlsx"
    write_aging_report(aging_df, summary_df, report_path, business_name=business)
    conn.close()

    token = _register_download(report_path, f"aging_{business}.xlsx")
    bucket_cols = [c for c in summary_df.columns if c not in ("customer_name", "total_owed")]
    return render_template(
        "aging.html", businesses=businesses, business=business,
        rows=summary_df.to_dict(orient="records"), bucket_cols=bucket_cols,
        total_outstanding=aging_df["balance"].sum(), open_count=len(aging_df),
        token=token,
    )


@app.route("/download/<token>")
def download(token):
    entry = _downloads.get(token)
    if entry is None or not entry[0].exists():
        flash("That report has expired - run the reconciliation again.")
        return redirect(url_for("index"))
    path, friendly_name = entry
    return send_file(path, as_attachment=True, download_name=friendly_name)


if __name__ == "__main__":
    app.run(debug=True)
