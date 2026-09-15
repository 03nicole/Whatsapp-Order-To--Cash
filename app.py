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
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from reconciler import load_catalog, load_invoices, load_momo_statement, reconcile
from reconciler import db, flutterwave, orders, whatsapp
from reconciler.report import write_report, write_aging_report

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RECONCILIATION_DB", APP_DIR / "reconciliation.db"))
UPLOAD_DIR = Path(tempfile.gettempdir()) / "reconciliation-engine-uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
FLUTTERWAVE_SECRET_HASH = os.environ.get("FLUTTERWAVE_SECRET_HASH", "")

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
        flash("Choose an invoices file.", "error")
        return redirect(url_for("index"))
    if not momo_file or not momo_file.filename:
        flash("Choose a MoMo statement file.", "error")
        return redirect(url_for("index"))

    try:
        invoices_path = _save_upload(invoices_file)
        momo_path = _save_upload(momo_file)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

    try:
        invoices = load_invoices(invoices_path)
        momo = load_momo_statement(momo_path)
    except ValueError as exc:
        flash(f"Couldn't read that file: {exc}", "error")
        return redirect(url_for("index"))
    finally:
        invoices_path.unlink(missing_ok=True)
        momo_path.unlink(missing_ok=True)

    conn = db.connect(DB_PATH)
    invoices = db.combine_with_open_invoices(conn, business, invoices)
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
        flash(f"No open invoices on record for '{business}'. Run a reconcile first.", "info")
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
        flash("That report has expired - run the reconciliation again.", "error")
        return redirect(url_for("index"))
    path, friendly_name = entry
    return send_file(path, as_attachment=True, download_name=friendly_name)


@app.route("/catalog/import", methods=["POST"])
def import_catalog():
    business = request.form.get("business", "").strip() or "default"
    catalog_file = request.files.get("catalog")
    if not catalog_file or not catalog_file.filename:
        flash("Choose a catalog file.", "error")
        return redirect(url_for("index"))

    try:
        catalog_path = _save_upload(catalog_file)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

    try:
        catalog_df = load_catalog(catalog_path)
    except ValueError as exc:
        flash(f"Couldn't read that file: {exc}", "error")
        return redirect(url_for("index"))
    finally:
        catalog_path.unlink(missing_ok=True)

    conn = db.connect(DB_PATH)
    db.save_catalog(conn, catalog_df, business)
    conn.close()

    flash(f"Imported {len(catalog_df)} product(s) into the catalog for '{business}'.", "success")
    return redirect(url_for("catalog_manage", business=business))


@app.route("/catalog")
def catalog_manage():
    business = request.args.get("business", "").strip()
    conn = db.connect(DB_PATH)
    businesses = db.known_businesses(conn)
    catalog_rows, history_rows = [], []
    if business:
        catalog_rows = db.get_catalog(conn, business).to_dict(orient="records")
        history_rows = db.stock_history(conn, business).head(20).to_dict(orient="records")
    conn.close()
    return render_template(
        "catalog.html", businesses=businesses, business=business or None,
        products=catalog_rows, history=history_rows,
    )


@app.route("/catalog/adjust", methods=["POST"])
def adjust_stock():
    business = request.form.get("business", "").strip() or "default"
    product_id = request.form.get("product_id", "").strip()
    reason = request.form.get("reason", "").strip() or None
    try:
        delta = int(request.form.get("delta", ""))
    except ValueError:
        flash("Stock change must be a whole number (e.g. 20 or -5).", "error")
        return redirect(url_for("catalog_manage", business=business))

    conn = db.connect(DB_PATH)
    applied = db.adjust_stock(conn, business, product_id, delta, reason=reason)
    conn.close()
    if not applied:
        flash(f"No product '{product_id}' in the catalog for '{business}'.", "error")
        return redirect(url_for("catalog_manage", business=business))

    flash(f"Adjusted {product_id} by {delta:+d}" + (f" ({reason})" if reason else "") + ".", "success")
    return redirect(url_for("catalog_manage", business=business))


@app.route("/catalog/edit", methods=["POST"])
def edit_product_details():
    """Sets a product's description/image - not something the original
    catalog CSV shape ever carried, needed once WhatsApp's native Catalog/
    Cart checkout made a real product photo worth having. Same "don't
    require a full re-import for one field" shape as /catalog/adjust."""
    business = request.form.get("business", "").strip() or "default"
    product_id = request.form.get("product_id", "").strip()
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()

    conn = db.connect(DB_PATH)
    applied = db.update_product_details(conn, business, product_id, description, image_url)
    conn.close()
    if not applied:
        flash(f"No product '{product_id}' in the catalog for '{business}'.", "error")
        return redirect(url_for("catalog_manage", business=business))

    flash(f"Updated details for {product_id}.", "success")
    return redirect(url_for("catalog_manage", business=business))


@app.route("/orders")
def orders_review():
    business = request.args.get("business", "").strip()
    conn = db.connect(DB_PATH)
    businesses = db.known_businesses(conn)
    flagged = db.flagged_orders(conn, business) if business else pd.DataFrame()
    conn.close()
    return render_template(
        "orders.html", businesses=businesses, business=business or None,
        orders=flagged.to_dict(orient="records"),
    )


@app.route("/warehouse")
def warehouse():
    """Phase 7's picking list: every confirmed order not yet marked
    fulfilled, oldest first, with the product/quantity lines a warehouse
    picker actually needs. One action - mark fulfilled - not a
    multi-stage pick/pack workflow; see db.mark_order_fulfilled's
    docstring for why."""
    business = request.args.get("business", "").strip()
    conn = db.connect(DB_PATH)
    businesses = db.known_businesses(conn)
    pending = []
    if business:
        for order in db.fulfillable_orders(conn, business).to_dict(orient="records"):
            order["lines"] = db.order_lines_for(conn, business, order["order_id"]).to_dict(orient="records")
            pending.append(order)
    conn.close()
    return render_template(
        "warehouse.html", businesses=businesses, business=business or None, orders=pending,
    )


@app.route("/analytics")
def analytics():
    """Phase 9's dashboard - built entirely from numbers this tool has
    always tracked and already treats as meaningful (the same outcome
    breakdown the Excel Summary sheet has shown since Phase 1, the same
    aging buckets the aging view already computes), not new invented
    metrics. Built ahead of its own stated validation gate - see
    docs/ROADMAP.md's Phase 9 entry - against whatever demo/sample data
    exists today, not months of real reconciled data."""
    business = request.args.get("business", "").strip()
    conn = db.connect(DB_PATH)
    businesses = db.known_businesses(conn)
    data = None
    if business:
        aging_df = db.aging_report(conn, business)
        data = {
            "reconciliation": db.reconciliation_summary(conn, business),
            "total_outstanding": float(aging_df["balance"].sum()) if not aging_df.empty else 0.0,
            "open_invoice_count": len(aging_df),
            "bucket_totals": db.aging_bucket_totals(aging_df),
            "orders": db.order_summary(conn, business),
            "low_stock": db.lowest_stock(conn, business, limit=5).to_dict(orient="records"),
            "backlog": len(db.fulfillable_orders(conn, business)),
        }
    conn.close()
    return render_template(
        "analytics.html", businesses=businesses, business=business or None, data=data,
    )


@app.route("/warehouse/fulfill", methods=["POST"])
def fulfill_order():
    business = request.form.get("business", "").strip() or "default"
    order_id = request.form.get("order_id", "").strip()
    note = request.form.get("note", "").strip() or None

    conn = db.connect(DB_PATH)
    applied = db.mark_order_fulfilled(conn, business, order_id, note=note)
    conn.close()
    if not applied:
        flash(f"Order '{order_id}' isn't awaiting fulfillment for '{business}' (already done, or doesn't exist).", "error")
        return redirect(url_for("warehouse", business=business))

    flash(f"Order {order_id} marked fulfilled" + (f" ({note})" if note else "") + ".", "success")
    return redirect(url_for("warehouse", business=business))


def _finalize_order(conn: sqlite3.Connection, business: str, order_id: str,
                     sender_name: str | None, sender_phone: str, raw_message: str,
                     parsed: "orders.ParsedOrder", client: whatsapp.WhatsAppClient) -> None:
    """Shared tail for every order-capture path (free-text WhatsApp
    messages, and - since Phase 5b - WhatsApp's native Catalog/Cart
    checkout): once a ParsedOrder exists and its stock check has run,
    everything after that is identical regardless of how the order was
    resolved - an invoice row in the EXISTING invoices table (never a
    parallel schema, per docs/DATA_MODEL.md), or a flagged order a sales
    agent resolves from the /orders page. Never partially confirms."""
    placed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    invoice_id = None
    if parsed.status == "confirmed":
        invoice = orders.build_invoice(parsed, order_id, sender_name, sender_phone, placed_at)
        invoice_id = invoice["invoice_id"]
        db.record_order_invoice(conn, business, invoice)
        for line in parsed.lines:
            db.adjust_stock(conn, business, line.product_id, -line.quantity_requested,
                             reason=f"order:{order_id}", adjusted_at=placed_at)
        client.send_text(
            sender_phone,
            f"Order confirmed - invoice {invoice_id} for K{parsed.amount:,.2f}. "
            f"Pay via MoMo and we'll confirm once it's received.",
        )
    else:
        client.send_text(
            sender_phone,
            "Thanks - your order needs a quick check from our team before we confirm it. "
            "We'll get back to you shortly.",
        )

    order_dict, line_dicts = orders.order_record(
        parsed, order_id, sender_name, sender_phone, placed_at, raw_message, invoice_id,
    )
    db.save_order(conn, business, order_dict, line_dicts)


def _handle_incoming_order(conn: sqlite3.Connection, business: str,
                            message: whatsapp.IncomingMessage, client: whatsapp.WhatsAppClient) -> None:
    """A free-text WhatsApp message -> parsed against the catalog by
    code/name -> _finalize_order()."""
    catalog_df = db.get_catalog(conn, business)
    order_id = message.message_id or uuid.uuid4().hex
    parsed = orders.parse_order_message(message.text, catalog_df)
    parsed = orders.check_stock(parsed, catalog_df)
    _finalize_order(conn, business, order_id, message.sender_name, message.sender_phone,
                     message.text, parsed, client)


def _handle_incoming_native_order(conn: sqlite3.Connection, business: str,
                                   native_order: whatsapp.IncomingOrder,
                                   client: whatsapp.WhatsAppClient) -> None:
    """A completed WhatsApp native Catalog/Cart checkout -> resolved by
    exact product_retailer_id, no code/name waterfall needed ->
    _finalize_order(). See reconciler.orders.resolve_native_order's
    docstring for the retailer_id/product_id matching assumption this
    depends on."""
    catalog_df = db.get_catalog(conn, business)
    order_id = native_order.message_id or uuid.uuid4().hex
    parsed = orders.resolve_native_order(native_order.items, catalog_df)
    parsed = orders.check_stock(parsed, catalog_df)
    raw_message = "[WhatsApp catalog order] " + ", ".join(
        f"{item.quantity}x {item.product_retailer_id}" for item in native_order.items
    )
    if native_order.note:
        raw_message += f" (note: {native_order.note})"
    _finalize_order(conn, business, order_id, native_order.sender_name, native_order.sender_phone,
                     raw_message, parsed, client)


@app.route("/whatsapp/webhook", methods=["GET"])
def whatsapp_webhook_verify():
    challenge = whatsapp.verify_webhook_subscription(request.args, WHATSAPP_VERIFY_TOKEN)
    if challenge is None:
        return "Verification failed", 403
    return challenge, 200


@app.route("/whatsapp/webhook", methods=["POST"])
def whatsapp_webhook_receive():
    """One webhook URL per business for now - configure it as
    `/whatsapp/webhook?business=<name>` with the provider. Multi-number
    routing (mapping a WhatsApp Business phone number ID to a business
    automatically) is a Phase 5 follow-up, not needed for a single pilot.

    Handles both order-capture paths: free-text messages (parsed against
    the catalog by code/name) and, since Phase 5b, completed WhatsApp
    native Catalog/Cart checkouts ("order"-type messages, already
    structured - see reconciler/whatsapp.py's IncomingOrder)."""
    business = request.args.get("business", "").strip() or "default"
    payload = request.get_json(silent=True) or {}
    messages = whatsapp.parse_webhook_payload(payload)
    native_orders = whatsapp.parse_order_messages(payload)

    conn = db.connect(DB_PATH)
    client = whatsapp.get_client()
    for message in messages:
        _handle_incoming_order(conn, business, message, client)
    for native_order in native_orders:
        _handle_incoming_native_order(conn, business, native_order, client)
    conn.close()

    return "", 200


def _notify_customers_of_payment(results: dict, client: whatsapp.WhatsAppClient) -> None:
    """The other half of _finalize_order()'s "Pay via MoMo and we'll
    confirm once it's received" promise - Phase 10 matched a live
    payment to its invoice but never actually told the customer who was
    told to expect that confirmation. Only for a FULL match: a split
    payment (results["partial"]) still leaves a real balance open, so
    telling that customer "settled" would be wrong. Only for invoices
    with a phone on file - a manually-uploaded invoice from a bulk
    statement import may not have one, and that's fine, it just doesn't
    get a text."""
    if results["matched"].empty:
        return
    all_invoices = results["all_invoices"].set_index("invoice_id")
    for row in results["matched"].to_dict(orient="records"):
        invoice_id = row.get("matched_invoice")
        if invoice_id not in all_invoices.index:
            continue
        phone = all_invoices.loc[invoice_id, "customer_phone"]
        if not phone or pd.isna(phone):
            continue
        client.send_text(
            phone,
            f"Payment received - invoice {invoice_id} (K{row['amount']:,.2f}) is now settled. Thanks for your order!",
        )


@app.route("/momo/webhook", methods=["POST"])
def momo_webhook_receive():
    """Phase 10's live-reconciliation path, built against Flutterwave -
    see reconciler/flutterwave.py's docstring for why that provider over
    a direct MTN/Airtel integration. One webhook URL per business for
    now (`/momo/webhook?business=<name>`), same convention as the
    WhatsApp webhook. Never trusts a request without a valid verif-hash
    - see docs/ARCHITECTURE.md's mobile money callback authentication
    requirement."""
    if not flutterwave.verify_signature(request.headers, FLUTTERWAVE_SECRET_HASH):
        return "Signature verification failed", 401

    business = request.args.get("business", "").strip() or "default"
    payload = request.get_json(silent=True) or {}
    payment = flutterwave.parse_webhook_payload(payload)
    if payment is None:
        # Not a completed Zambia-mobile-money charge (a card payment on
        # the same account, a pending/failed charge, a different event
        # type entirely) - not this project's concern, not an error.
        return "", 200

    conn = db.connect(DB_PATH)
    invoices = db.open_invoices(conn, business)
    momo_df = flutterwave.to_momo_dataframe(payment)
    momo_df, skipped = db.filter_new_transactions(momo_df, db.known_transaction_ids(conn, business))

    if not momo_df.empty:
        results = reconcile(invoices, momo_df)
        db.save_run(conn, results, business)
        _notify_customers_of_payment(results, whatsapp.get_client())
    conn.close()

    return "", 200


if __name__ == "__main__":
    app.run(debug=True)
