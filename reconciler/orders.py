"""
Order parsing — Phase 5 ("order capture").

A WhatsApp order is free text, but this project's governing principle
(see matcher.py) is deterministic-first: never guess when unsure. So
order parsing is its own small waterfall, structured/catalog-driven
rather than NLP:

  1. Each comma/newline-separated segment of the message is expected to
     look like "<quantity> <product reference>" (e.g. "10 COKE-24" or
     "10x Coke 300ml").
  2. The product reference resolves against the business's catalog by:
       a. exact product_id match (case-insensitive)         -> confident
       b. exactly one product whose name matches (exact or   -> confident
          substring, case-insensitive)
       c. more than one name match                           -> ambiguous
       d. no match at all, or the segment doesn't even parse  -> unknown
          as "<qty> <reference>"
  3. If every line resolves confidently AND there's enough stock for
     each one, the order confirms automatically. If ANY line is
     ambiguous/unknown, or ANY line exceeds available stock, the WHOLE
     order is flagged for a human — never partially auto-confirmed. A
     half-right order shipped on the system's own judgment is worse than
     one a sales agent glances at for ten seconds — the same trade this
     project already made for payment matching.

Free-text NLP ("10 boxes of Coke" without a code, typo correction,
other languages) is a later optimization layered on top of this
waterfall, not a replacement for it — the same relationship matcher.py
has with its own two stub extension points.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

_LINE_RE = re.compile(r"^\s*(\d+)\s*[xX]?\s*(.+?)\s*$")


@dataclass
class ParsedLine:
    raw_text: str
    quantity_requested: int | None = None
    product_id: str | None = None
    product_name: str | None = None
    unit_price: float | None = None
    line_total: float | None = None
    resolution: str = "unknown_product"  # exact_code | unique_name | ambiguous | unknown_product


@dataclass
class ParsedOrder:
    lines: list[ParsedLine] = field(default_factory=list)
    status: str = "flagged"  # confirmed | flagged
    flag_reason: str | None = None
    amount: float = 0.0


def _split_segments(message: str) -> list[str]:
    """Splits on commas/newlines into candidate order lines, then drops
    any segment that doesn't even start with a quantity number. A real
    customer's message usually isn't just order lines - "Hi, please can
    I get: 5x COKE-24" - and a segment with no leading quantity was never
    an attempt at "<qty> <product>" under this waterfall's own grammar,
    so treating it as an unresolvable product (and flagging the whole
    order for it) was flagging on conversational filler, not on genuine
    order ambiguity. Found live: a real free-text order with a greeting
    got flagged for exactly this reason. A segment that DOES start with
    a quantity but still fails to resolve to a product is untouched by
    this - that's still genuine ambiguity the waterfall must flag."""
    parts = re.split(r"[,\n]", message)
    return [p.strip() for p in parts if p.strip() and _LINE_RE.match(p.strip())]


def _resolve_product(reference: str, catalog: pd.DataFrame):
    """Returns (product_id, product_name, unit_price, resolution)."""
    ref_norm = reference.strip().lower()

    exact_code = catalog[catalog["product_id"].str.lower() == ref_norm]
    if len(exact_code) == 1:
        row = exact_code.iloc[0]
        return row["product_id"], row["name"], float(row["unit_price"]), "exact_code"

    name_matches = catalog[catalog["name"].str.lower() == ref_norm]
    if len(name_matches) == 0:
        name_matches = catalog[catalog["name"].str.lower().str.contains(re.escape(ref_norm), na=False)]
    if len(name_matches) == 1:
        row = name_matches.iloc[0]
        return row["product_id"], row["name"], float(row["unit_price"]), "unique_name"
    if len(name_matches) > 1:
        return None, None, None, "ambiguous"

    return None, None, None, "unknown_product"


def parse_order_message(message: str, catalog: pd.DataFrame) -> ParsedOrder:
    """Parses a raw WhatsApp order message against a business's product
    catalog (db.get_catalog / loaders.load_catalog's shape). Message text
    and catalog in, a structured result out — doesn't touch stock or the
    database itself, exactly like matcher.reconcile() takes DataFrames in
    and returns a result dict rather than writing to a DB itself."""
    segments = _split_segments(message)
    if not segments:
        return ParsedOrder(lines=[], status="flagged", flag_reason="no order lines found in message")

    lines: list[ParsedLine] = []
    for segment in segments:
        qty_text, reference = _LINE_RE.match(segment).groups()
        product_id, product_name, unit_price, resolution = _resolve_product(reference, catalog)
        quantity = int(qty_text)
        line_total = round(quantity * unit_price, 2) if unit_price is not None else None
        lines.append(ParsedLine(
            raw_text=segment, quantity_requested=quantity, product_id=product_id,
            product_name=product_name, unit_price=unit_price, line_total=line_total,
            resolution=resolution,
        ))

    unresolved = [l for l in lines if l.resolution not in ("exact_code", "unique_name")]
    if unresolved:
        reason = "; ".join(f"'{l.raw_text}' -> {l.resolution}" for l in unresolved)
        return ParsedOrder(lines=lines, status="flagged", flag_reason=f"could not resolve: {reason}")

    amount = round(sum(l.line_total for l in lines), 2)
    return ParsedOrder(lines=lines, status="confirmed", amount=amount)


def resolve_native_order(items: list, catalog: pd.DataFrame) -> ParsedOrder:
    """Builds a ParsedOrder from a WhatsApp native Catalog/Cart checkout
    (reconciler.whatsapp.IncomingOrder.items - typed loosely here as
    `list` rather than importing that dataclass, so this module stays
    transport-agnostic the way parse_order_message() already is; each
    item just needs .product_retailer_id, .quantity, .item_price
    attributes).

    Unlike parse_order_message(), there's no code/name-matching
    waterfall to run - a native checkout already carries an exact
    product identifier per line, on the assumption that the
    distributor's Meta Commerce Catalog was set up with each product's
    retailer_id matching this system's own product_id (a setup step,
    not something this function can verify). Still never partially
    confirms: if any item's retailer_id isn't found in the catalog, the
    WHOLE order is flagged - the same invariant parse_order_message()
    enforces, so a business can't end up with an order half-resolved
    against a stale or mismatched catalog sync."""
    lines: list[ParsedLine] = []
    for item in items:
        ref_norm = item.product_retailer_id.strip().lower()
        match = catalog[catalog["product_id"].str.lower() == ref_norm]
        if len(match) == 1:
            row = match.iloc[0]
            unit_price = float(row["unit_price"])
            line_total = round(item.quantity * unit_price, 2)
            lines.append(ParsedLine(
                raw_text=item.product_retailer_id, quantity_requested=item.quantity,
                product_id=row["product_id"], product_name=row["name"],
                unit_price=unit_price, line_total=line_total, resolution="exact_code",
            ))
        else:
            lines.append(ParsedLine(
                raw_text=item.product_retailer_id, quantity_requested=item.quantity,
                resolution="unknown_product",
            ))

    unresolved = [l for l in lines if l.resolution != "exact_code"]
    if unresolved:
        reason = "; ".join(f"'{l.raw_text}' -> {l.resolution}" for l in unresolved)
        return ParsedOrder(lines=lines, status="flagged",
                            flag_reason=f"native catalog order: could not resolve: {reason}")

    amount = round(sum(l.line_total for l in lines), 2)
    return ParsedOrder(lines=lines, status="confirmed", amount=amount)


def check_stock(order: ParsedOrder, catalog: pd.DataFrame) -> ParsedOrder:
    """Only meaningful once order.status == 'confirmed' (every line
    resolved) — checks each line's requested quantity against
    quantity_on_hand and flips the order to 'flagged' if any line asks
    for more than is on hand. Never partially fulfills an order; mutates
    and returns the same ParsedOrder for convenience."""
    if order.status != "confirmed":
        return order

    stock_by_id = dict(zip(catalog["product_id"], catalog["quantity_on_hand"]))
    shortfalls = []
    for line in order.lines:
        on_hand = stock_by_id.get(line.product_id, 0)
        if line.quantity_requested > on_hand:
            shortfalls.append(f"{line.product_id} ({line.product_name}): "
                               f"requested {line.quantity_requested}, {on_hand} on hand")

    if shortfalls:
        order.status = "flagged"
        order.flag_reason = "insufficient stock: " + "; ".join(shortfalls)
    return order


def build_invoice(order: ParsedOrder, order_id: str, customer_name: str | None,
                   customer_phone: str | None, placed_at: str) -> dict:
    """Only call once order.status == 'confirmed'. Shapes a plain dict
    matching the columns db.record_order_invoice writes into the
    EXISTING `invoices` table — Phase 5's entire point is producing a row
    there, not a parallel schema.

    `placed_at` is a full timestamp (used as-is for the order's own
    audit trail), but an invoice's `date` is a date, matching what
    load_invoices()/save_run() store for a manually-uploaded invoice
    (date-only, no time-of-day or timezone) — storing the full
    timestamp here made this invoice's date column timezone-aware while
    every other invoice's is naive, which crashes aging_report()'s
    pd.Timestamp(as_of) - df["date"] the moment the two get mixed."""
    return {
        "invoice_id": f"ORD-{order_id}",
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "amount": order.amount,
        "date": placed_at.split("T")[0],
    }


def order_record(order: ParsedOrder, order_id: str, customer_name: str | None,
                  customer_phone: str | None, placed_at: str, raw_message: str,
                  invoice_id: str | None) -> tuple[dict, list[dict]]:
    """Shapes (order_dict, line_dicts) for db.save_order."""
    order_dict = {
        "order_id": order_id,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "placed_at": placed_at,
        "status": order.status,
        "raw_message": raw_message,
        "invoice_id": invoice_id,
        "flag_reason": order.flag_reason,
    }
    line_dicts = [
        {
            "product_id": l.product_id,
            "raw_text": l.raw_text,
            "quantity_requested": l.quantity_requested,
            "unit_price": l.unit_price,
            "line_total": l.line_total,
            "resolution": l.resolution,
        }
        for l in order.lines
    ]
    return order_dict, line_dicts
