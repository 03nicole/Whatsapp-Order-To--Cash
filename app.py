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

import hmac
import os
import secrets
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from flask import Flask, Response, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from reconciler import load_catalog, load_invoices, load_momo_statement, reconcile
from reconciler import db, flutterwave, orders, whatsapp, zra
from reconciler.catalog_feed import build_meta_feed_csv
from reconciler.report import write_report, write_aging_report

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("RECONCILIATION_DB", APP_DIR / "reconciliation.db"))
UPLOAD_DIR = Path(tempfile.gettempdir()) / "reconciliation-engine-uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
# Used only outside the 24-hour customer service window (see
# reconciler/whatsapp.py's is_within_customer_service_window()) - this
# exact template must already exist, be approved in Meta Business
# Manager, and take exactly 2 body placeholders ({{1}} invoice id,
# {{2}} amount) in that order, or the real API will reject it. Nothing
# in this codebase can create or verify that template - same
# operational-setup-step category as Phase 5b's Commerce Catalog
# retailer_id assumption and Phase 11's ZRA device registration.
WHATSAPP_PAYMENT_TEMPLATE_NAME = os.environ.get("WHATSAPP_PAYMENT_TEMPLATE_NAME", "payment_received")
WHATSAPP_PAYMENT_TEMPLATE_LANG = os.environ.get("WHATSAPP_PAYMENT_TEMPLATE_LANG", "en_US")
FLUTTERWAVE_SECRET_HASH = os.environ.get("FLUTTERWAVE_SECRET_HASH", "")
# Signs the catalog feed URL below (see catalog_feed_token()). Unset in the
# common local/dev case - falls back to the process's own random
# app.secret_key, which is fine for local testing but means the URL
# changes on every restart; set this explicitly once the feed URL is
# actually registered with Meta Commerce Manager, so it stays stable.
CATALOG_FEED_SECRET = os.environ.get("CATALOG_FEED_SECRET", "")
# Public base URL this app is reachable at, if it's been deployed
# anywhere - unset in the local/dev default, in which case the feed's
# `link` column is left blank rather than pointing at a URL nobody but
# this machine can reach. See docs/ARCHITECTURE.md's deployment notes.
CATALOG_BASE_URL = os.environ.get("CATALOG_BASE_URL", "").rstrip("/")

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)


def catalog_feed_token(business: str) -> str:
    """HMAC of the business name, not a random per-request secret like
    /download's tokens - Meta polls this URL on a recurring schedule, so
    it has to keep working without this app remembering anything across
    restarts (this app doesn't persist a session across them; see
    CATALOG_FEED_SECRET above for why the URL is only stable long-term
    once that env var is set explicitly)."""
    secret = (CATALOG_FEED_SECRET or app.secret_key).encode()
    return hmac.new(secret, business.encode(), "sha256").hexdigest()[:24]

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
    feed_url = None
    if business:
        catalog_rows = db.get_catalog(conn, business).to_dict(orient="records")
        history_rows = db.stock_history(conn, business).head(20).to_dict(orient="records")
        feed_url = url_for("catalog_feed", business=business,
                            token=catalog_feed_token(business), _external=True)
    conn.close()
    return render_template(
        "catalog.html", businesses=businesses, business=business or None,
        products=catalog_rows, history=history_rows, feed_url=feed_url,
    )


@app.route("/catalog/feed.csv")
def catalog_feed():
    """Meta Commerce Catalog scheduled-feed endpoint - register the URL
    shown on /catalog in Commerce Manager under Catalog -> Data Sources ->
    Data Feed as a scheduled hosted-URL fetch (Meta supports polling as
    often as hourly). `id` is always this system's own product_id, which
    is what makes Phase 5b's retailer_id assumption hold automatically
    instead of depending on a distributor configuring it correctly by
    hand. Token-gated (see catalog_feed_token()) since, once registered,
    this URL is polled from the public internet on a fixed schedule -
    meaningless until this app is actually deployed somewhere reachable,
    the same pre-existing deferred assumption the WhatsApp/MoMo webhooks
    already have (see docs/ARCHITECTURE.md's deployment notes)."""
    business = request.args.get("business", "").strip()
    token = request.args.get("token", "")
    if not business or not hmac.compare_digest(token, catalog_feed_token(business)):
        return "Forbidden", 403

    conn = db.connect(DB_PATH)
    catalog_df = db.get_catalog(conn, business)
    conn.close()
    link_base = f"{CATALOG_BASE_URL}/catalog?business={business}" if CATALOG_BASE_URL else None
    csv_text = build_meta_feed_csv(catalog_df, product_link_base=link_base)
    return Response(csv_text, mimetype="text/csv")


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


@app.route("/invoice/<invoice_id>")
def view_invoice(invoice_id):
    """A real, renderable invoice document - the first one this system
    has ever had. Until now 'invoice' meant only a ledger row
    (amount/balance) used for reconciliation matching, never something a
    customer or distributor could actually look at. Printable via the
    browser's own print-to-PDF - no new dependency for that."""
    business = request.args.get("business", "").strip()
    if not business:
        flash("Business name is required to look up an invoice.", "error")
        return redirect(url_for("index"))

    conn = db.connect(DB_PATH)
    invoice = db.get_invoice_detail(conn, business, invoice_id)
    conn.close()
    if invoice is None:
        flash(f"No invoice '{invoice_id}' found for '{business}'.", "error")
        return redirect(url_for("index"))

    if invoice["balance"] <= 0:
        payment_status = "Paid in full"
    elif invoice["balance"] < invoice["amount"]:
        payment_status = "Partially paid"
    else:
        payment_status = "Awaiting payment"

    return render_template(
        "invoice.html", business=business, invoice=invoice, payment_status=payment_status,
    )


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


@app.route("/fiscalization")
def fiscalization():
    """Phase 11 follow-up: shows whether a business has ZRA credentials
    configured, and any confirmed order whose best-effort submission
    failed (_fiscalize_order() in this file) - the fiscalization
    equivalent of /orders' flagged-order review queue. Never blocks
    anything by itself; it's where a failure becomes visible instead of
    only a log line."""
    business = request.args.get("business", "").strip()
    conn = db.connect(DB_PATH)
    businesses = db.known_businesses(conn)
    settings, failed = None, []
    if business:
        settings = db.get_zra_settings(conn, business)
        failed = db.orders_needing_fiscalization_retry(conn, business).to_dict(orient="records")
    conn.close()
    return render_template(
        "fiscalization.html", businesses=businesses, business=business or None,
        settings=settings, failed=failed,
    )


@app.route("/fiscalization/settings", methods=["POST"])
def save_zra_settings():
    business = request.form.get("business", "").strip() or "default"
    fields = {
        key: request.form.get(key, "").strip()
        for key in ("server_url", "username", "password", "tpin", "bhf_id", "device_serial")
    }
    if not all(fields.values()):
        flash("All ZRA settings fields are required.", "error")
        return redirect(url_for("fiscalization", business=business))

    conn = db.connect(DB_PATH)
    db.save_zra_settings(conn, business, **fields)
    conn.close()
    flash(f"ZRA settings saved for '{business}'.", "success")
    return redirect(url_for("fiscalization", business=business))


@app.route("/fiscalization/retry", methods=["POST"])
def retry_fiscalization():
    """Re-attempts a previously-failed submission - e.g. after a product's
    vat_category_code/item_class_code has since been set via /catalog, or
    ZRA was simply unreachable the first time."""
    business = request.form.get("business", "").strip() or "default"
    order_id = request.form.get("order_id", "").strip()

    conn = db.connect(DB_PATH)
    row = conn.execute(
        "SELECT invoice_id, customer_name, placed_at FROM orders "
        "WHERE business = ? AND order_id = ? AND fiscalization_status = 'failed'",
        (business, order_id),
    ).fetchone()
    if row is None:
        conn.close()
        flash(f"Order '{order_id}' isn't awaiting a fiscalization retry.", "error")
        return redirect(url_for("fiscalization", business=business))

    invoice_id, customer_name, placed_at = row
    _fiscalize_order(conn, business, order_id, invoice_id, customer_name, placed_at)
    conn.close()
    flash(f"Retried fiscalization for {order_id} - check the status below.", "info")
    return redirect(url_for("fiscalization", business=business))


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

    if parsed.status == "confirmed":
        _fiscalize_order(conn, business, order_id, invoice_id, sender_name, placed_at)


def _fiscalize_order(conn: sqlite3.Connection, business: str, order_id: str, invoice_id: str,
                      customer_name: str | None, placed_at: str) -> None:
    """Best-effort ZRA Smart Invoice submission, fired right after order
    confirmation - see docs/ROADMAP.md's Phase 11 follow-up entry for why
    it's this moment (VAT invoicing is tied to the sale being invoiced,
    not to payment being received) and why a failure here must NEVER
    block the order: a government API being slow, down, or rejecting a
    submission (e.g. a product still missing vat_category_code) must not
    stop this business from taking orders. Anything short of a clean
    success is recorded via mark_order_fiscalization_failed() and
    surfaced on /fiscalization, not raised.

    Silently does nothing if this business hasn't configured ZRA
    credentials at all (db.get_zra_settings() returns None) - Phase 11 is
    opt-in per business, not a requirement every order now has to clear."""
    settings = db.get_zra_settings(conn, business)
    if settings is None:
        return

    try:
        credentials = zra.ZRACredentials.from_settings_row(settings)
        order_lines = db.order_lines_for(conn, business, order_id)
        catalog = db.get_catalog(conn, business).set_index("product_id")
        line_items = [
            {
                "product_id": line.product_id,
                "name": catalog.loc[line.product_id, "name"],
                "quantity": line.quantity_requested,
                "unit_price": line.unit_price,
                "vat_category_code": catalog.loc[line.product_id, "vat_category_code"],
                "item_class_code": catalog.loc[line.product_id, "item_class_code"],
            }
            for line in order_lines.itertuples()
        ]
        sales_dt = placed_at[:10].replace("-", "")
        payload = zra.build_sales_payload(
            credentials, invoice_id, sales_dt, customer_name or "", None, line_items,
        )
        response = zra.ZRAClient(credentials).submit_sale(payload)
        db.mark_order_fiscalized(conn, business, order_id, response)
    except Exception as exc:
        app.logger.warning("ZRA fiscalization failed for %s/%s: %s", business, order_id, exc)
        db.mark_order_fiscalization_failed(conn, business, order_id, str(exc))


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


def _notify_customers_of_payment(conn: sqlite3.Connection, business: str, results: dict,
                                  client: whatsapp.WhatsAppClient) -> None:
    """The other half of _finalize_order()'s "Pay via MoMo and we'll
    confirm once it's received" promise - Phase 10 matched a live
    payment to its invoice but never actually told the customer who was
    told to expect that confirmation. Only for a FULL match: a split
    payment (results["partial"]) still leaves a real balance open, so
    telling that customer "settled" would be wrong. Only for invoices
    with a phone on file - a manually-uploaded invoice from a bulk
    statement import may not have one, and that's fine, it just doesn't
    get a text.

    Chooses free text vs. a template message based on WhatsApp's real
    24-hour customer service window (see
    reconciler/whatsapp.py's is_within_customer_service_window()) - a
    payment can easily settle days after the customer last wrote in, and
    free text silently fails against the real API outside that window.
    The window is judged against the underlying order's placed_at (the
    only record this system has of "when did this customer last
    message in") - a manually-uploaded invoice has no order at all, so
    it fails closed to "outside the window" rather than guessing fresh."""
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
        amount_str = f"{row['amount']:,.2f}"
        detail = db.get_invoice_detail(conn, business, invoice_id)
        placed_at = detail["placed_at"] if detail else None
        if whatsapp.is_within_customer_service_window(placed_at):
            client.send_text(
                phone,
                f"Payment received - invoice {invoice_id} (K{amount_str}) is now settled. Thanks for your order!",
            )
        else:
            client.send_template(
                phone, WHATSAPP_PAYMENT_TEMPLATE_NAME, WHATSAPP_PAYMENT_TEMPLATE_LANG,
                [invoice_id, amount_str],
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
        _notify_customers_of_payment(conn, business, results, whatsapp.get_client())
    conn.close()

    return "", 200


if __name__ == "__main__":
    app.run(debug=True)
