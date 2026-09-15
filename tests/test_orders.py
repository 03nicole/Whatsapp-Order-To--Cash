from pathlib import Path

import pandas as pd
import pytest

from reconciler import load_catalog
from reconciler.orders import (
    build_invoice, check_stock, order_record, parse_order_message, resolve_native_order,
)

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture
def catalog():
    return pd.DataFrame([
        {"product_id": "COKE-24", "name": "Coca-Cola 300ml 24-pack", "unit": "box",
         "unit_price": 120.0, "quantity_on_hand": 50},
        {"product_id": "FANTA-24", "name": "Fanta Orange 300ml 24-pack", "unit": "box",
         "unit_price": 110.0, "quantity_on_hand": 30},
        {"product_id": "SPRITE-24", "name": "Sprite 300ml 24-pack", "unit": "box",
         "unit_price": 110.0, "quantity_on_hand": 5},
        {"product_id": "FANTA-CASE", "name": "Fanta Orange Case (small)", "unit": "case",
         "unit_price": 40.0, "quantity_on_hand": 20},
    ])


# --- parse_order_message: exact code / unique name -----------------------

def test_parses_exact_product_code(catalog):
    order = parse_order_message("10 COKE-24", catalog)
    assert order.status == "confirmed"
    assert len(order.lines) == 1
    line = order.lines[0]
    assert line.resolution == "exact_code"
    assert line.product_id == "COKE-24"
    assert line.quantity_requested == 10
    assert line.line_total == 1200.0
    assert order.amount == 1200.0


def test_parses_exact_product_code_case_insensitive_with_x_separator(catalog):
    order = parse_order_message("5x coke-24", catalog)
    assert order.status == "confirmed"
    assert order.lines[0].product_id == "COKE-24"


def test_parses_multiple_comma_separated_lines(catalog):
    order = parse_order_message("10 COKE-24, 5 FANTA-24, 3 SPRITE-24", catalog)
    assert order.status == "confirmed"
    assert [l.product_id for l in order.lines] == ["COKE-24", "FANTA-24", "SPRITE-24"]
    assert order.amount == 10 * 120.0 + 5 * 110.0 + 3 * 110.0


def test_parses_newline_separated_lines(catalog):
    order = parse_order_message("10 COKE-24\n5 FANTA-24", catalog)
    assert order.status == "confirmed"
    assert len(order.lines) == 2


def test_resolves_unique_exact_name_match(catalog):
    order = parse_order_message("2 Sprite 300ml 24-pack", catalog)
    assert order.status == "confirmed"
    assert order.lines[0].product_id == "SPRITE-24"
    assert order.lines[0].resolution == "unique_name"


def test_resolves_unique_substring_name_match(catalog):
    order = parse_order_message("2 Coca-Cola", catalog)
    assert order.status == "confirmed"
    assert order.lines[0].product_id == "COKE-24"
    assert order.lines[0].resolution == "unique_name"


# --- resolve_native_order (WhatsApp native Catalog/Cart checkout) --------

def _item(product_retailer_id, quantity, item_price=0.0, currency="ZMW"):
    from reconciler.whatsapp import OrderItem
    return OrderItem(product_retailer_id=product_retailer_id, quantity=quantity,
                      item_price=item_price, currency=currency)


def test_resolve_native_order_confirms_when_every_retailer_id_matches(catalog):
    order = resolve_native_order([_item("COKE-24", 10), _item("FANTA-24", 5)], catalog)
    assert order.status == "confirmed"
    assert order.amount == 10 * 120.0 + 5 * 110.0
    assert [l.resolution for l in order.lines] == ["exact_code", "exact_code"]


def test_resolve_native_order_is_case_insensitive_on_retailer_id(catalog):
    order = resolve_native_order([_item("coke-24", 1)], catalog)
    assert order.status == "confirmed"
    assert order.lines[0].product_id == "COKE-24"


def test_resolve_native_order_flags_the_whole_order_on_one_unknown_retailer_id(catalog):
    """Same invariant as parse_order_message(): never partially confirm.
    A mismatched Meta-catalog-to-our-catalog sync shouldn't silently
    ship half an order."""
    order = resolve_native_order([_item("COKE-24", 10), _item("NOT-IN-OUR-CATALOG", 2)], catalog)
    assert order.status == "flagged"
    assert "NOT-IN-OUR-CATALOG" in order.flag_reason
    assert order.lines[0].resolution == "exact_code"  # still parsed correctly
    assert order.lines[1].resolution == "unknown_product"


def test_resolve_native_order_never_falls_back_to_name_matching(catalog):
    """Unlike parse_order_message(), there's no code/name waterfall here
    - a native order's retailer_id must match a product_id exactly, or
    it's unresolved, even if it happens to look like a product name."""
    order = resolve_native_order([_item("Coca-Cola", 1)], catalog)
    assert order.status == "flagged"


def test_resolve_native_order_check_stock_integrates_normally(catalog):
    order = resolve_native_order([_item("SPRITE-24", 10)], catalog)  # only 5 on hand
    order = check_stock(order, catalog)
    assert order.status == "flagged"
    assert "insufficient stock" in order.flag_reason


# --- parse_order_message: never guess when unsure -------------------------

def test_ambiguous_name_match_flags_the_whole_order(catalog):
    """'Fanta' matches both FANTA-24 and FANTA-CASE - genuinely ambiguous,
    must never guess which one."""
    order = parse_order_message("10 Fanta", catalog)
    assert order.status == "flagged"
    assert "ambiguous" in order.flag_reason


def test_unknown_product_flags_the_whole_order(catalog):
    order = parse_order_message("10 Guinness", catalog)
    assert order.status == "flagged"
    assert "unknown_product" in order.flag_reason


def test_message_with_no_order_lines_flags(catalog):
    """No segment even starts with a quantity number, so none look like
    an order line attempt at all - flagged for having no order lines,
    not for a false per-segment "unknown_product" ambiguity."""
    order = parse_order_message("please send stock soon", catalog)
    assert order.status == "flagged"
    assert order.lines == []


def test_one_bad_line_flags_the_whole_order_not_just_that_line(catalog):
    """A half-right order is worse to auto-ship than one a human glances
    at - the same principle as the reconciliation engine's needs_review."""
    order = parse_order_message("10 COKE-24, 5 Guinness", catalog)
    assert order.status == "flagged"
    assert "Guinness" in order.flag_reason
    # COKE-24 line still parsed correctly, just not auto-confirmed
    coke_line = next(l for l in order.lines if l.raw_text.strip().startswith("10"))
    assert coke_line.resolution == "exact_code"


def test_empty_message_flags(catalog):
    order = parse_order_message("   ", catalog)
    assert order.status == "flagged"
    assert order.lines == []


def test_greeting_before_order_lines_does_not_flag_the_order(catalog):
    """Regression test: found live, sending a real webhook payload
    shaped like an actual customer message. A greeting segment has no
    leading quantity number, so it's not a genuine ambiguous product -
    it should be dropped, not treated as an unresolvable line that
    flags the whole order."""
    order = parse_order_message("Hi, please can I get:\n5x COKE-24\n3 FANTA-24", catalog)
    assert order.status == "confirmed"
    assert order.amount == 5 * 120.0 + 3 * 110.0
    assert [l.resolution for l in order.lines] == ["exact_code", "exact_code"]


# --- check_stock -----------------------------------------------------------

def test_check_stock_confirms_when_sufficient(catalog):
    order = parse_order_message("10 COKE-24", catalog)
    order = check_stock(order, catalog)
    assert order.status == "confirmed"


def test_check_stock_flags_when_insufficient(catalog):
    order = parse_order_message("10 SPRITE-24", catalog)  # only 5 on hand
    order = check_stock(order, catalog)
    assert order.status == "flagged"
    assert "insufficient stock" in order.flag_reason
    assert "SPRITE-24" in order.flag_reason


def test_check_stock_is_a_noop_on_an_already_flagged_order(catalog):
    order = parse_order_message("10 Guinness", catalog)
    result = check_stock(order, catalog)
    assert result.flag_reason == order.flag_reason  # unchanged, not overwritten


# --- build_invoice / order_record shape -----------------------------------

def test_build_invoice_shapes_a_row_for_the_existing_invoices_table(catalog):
    order = parse_order_message("10 COKE-24", catalog)
    invoice = build_invoice(order, order_id="abc123", customer_name="ABC Traders",
                             customer_phone="0977111111", placed_at="2026-01-01")
    assert invoice == {
        "invoice_id": "ORD-abc123",
        "customer_name": "ABC Traders",
        "customer_phone": "0977111111",
        "amount": 1200.0,
        "date": "2026-01-01",
    }


def test_build_invoice_strips_time_and_timezone_from_a_full_timestamp(catalog):
    """placed_at is a full ISO timestamp (used as-is for the order's own
    audit trail), but an invoice's date must be date-only, matching what
    a manually-uploaded invoice's date looks like - storing the full
    timestamp made a business's invoices column come back entirely
    timezone-aware whenever every invoice happened to be order-generated,
    which crashed aging_report()'s date arithmetic against a naive
    timestamp. Found live, not by inspection."""
    order = parse_order_message("10 COKE-24", catalog)
    invoice = build_invoice(order, order_id="abc123", customer_name="ABC Traders",
                             customer_phone="0977111111",
                             placed_at="2026-09-15T14:31:43+00:00")
    assert invoice["date"] == "2026-09-15"


def test_order_record_shapes_order_and_line_dicts(catalog):
    order = parse_order_message("10 COKE-24, 5 FANTA-24", catalog)
    order_dict, line_dicts = order_record(
        order, order_id="abc123", customer_name="ABC Traders", customer_phone="0977111111",
        placed_at="2026-01-01T10:00:00", raw_message="10 COKE-24, 5 FANTA-24",
        invoice_id="ORD-abc123",
    )
    assert order_dict["order_id"] == "abc123"
    assert order_dict["status"] == "confirmed"
    assert order_dict["invoice_id"] == "ORD-abc123"
    assert len(line_dicts) == 2
    assert line_dicts[0]["product_id"] == "COKE-24"
    assert line_dicts[0]["resolution"] == "exact_code"


# --- Regression test against the shipped sample_data/catalog.csv ---------

def test_sample_catalog_order_matches_the_readme_walkthrough():
    """Pins the exact order the README's 'Try the whole loop' demo uses
    (also exercised live against the running server, and end-to-end
    through reconciliation and warehouse fulfillment) to a known-good
    total - a regression anchor the same way
    test_matcher.py::test_sample_data_reconciles_to_expected_outcome_counts
    is for the reconciliation sample data."""
    catalog = load_catalog(SAMPLE_DATA / "catalog.csv")
    order = parse_order_message("10 COKE-24, 5 FANTA-24, 2 SUGAR-50", catalog)
    order = check_stock(order, catalog)
    assert order.status == "confirmed"
    assert order.amount == 10 * 120 + 5 * 110 + 2 * 650  # 3050.0
