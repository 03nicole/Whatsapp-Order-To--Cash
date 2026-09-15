from pathlib import Path

import pandas as pd

from reconciler.loaders import load_invoices, load_momo_statement
from reconciler.matcher import find_invoice_in_reference, reconcile

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"


def _rules(result):
    """Flattens matched/partial/needs_review into {transaction_id: match_rule}
    for easy assertions."""
    rules = {}
    for outcome in ("matched", "partial", "needs_review"):
        df = result[outcome]
        for r in df.to_dict(orient="records"):
            rules[r["transaction_id"]] = r["match_rule"]
    for r in result["unmatched"].to_dict(orient="records"):
        rules[r["transaction_id"]] = None
    return rules


# --- find_invoice_in_reference ------------------------------------------

def test_find_invoice_in_reference_ignores_punctuation_and_case():
    assert find_invoice_in_reference("payment for inv-1001 thanks", ["INV-1001"]) == "INV-1001"
    assert find_invoice_in_reference("INV1001 settlement", ["INV-1001"]) == "INV-1001"


def test_find_invoice_in_reference_no_match():
    assert find_invoice_in_reference("just a stock payment", ["INV-1001"]) is None


def test_find_invoice_in_reference_blank():
    assert find_invoice_in_reference("", ["INV-1001"]) is None


# --- Rule 1: invoice number in reference --------------------------------

def test_rule1_exact_amount_matches_and_zeroes_balance(make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    momo = make_momo([dict(transaction_id="T1", amount=1000, reference="INV-1 payment")])
    result = reconcile(invoices, momo)
    assert len(result["matched"]) == 1
    row = result["matched"].iloc[0]
    assert row["match_rule"] == "invoice_number_exact_amount"
    assert result["all_invoices"].loc[0, "balance"] == 0


def test_rule1_partial_amount(make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    momo = make_momo([dict(transaction_id="T1", amount=400, reference="INV-1 payment")])
    result = reconcile(invoices, momo)
    assert len(result["partial"]) == 1
    row = result["partial"].iloc[0]
    assert row["match_rule"] == "invoice_number_partial_amount"
    assert row["remaining_balance"] == 600
    assert result["all_invoices"].loc[0, "balance"] == 600


def test_rule1_overpay_flags_for_review_instead_of_auto_matching(make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    momo = make_momo([dict(transaction_id="T1", amount=1500, reference="INV-1 payment")])
    result = reconcile(invoices, momo)
    assert len(result["needs_review"]) == 1
    assert result["needs_review"].iloc[0]["match_rule"] == "invoice_number_but_amount_exceeds_balance"
    # balance must NOT be touched - a human resolves this, not the engine.
    assert result["all_invoices"].loc[0, "balance"] == 1000


# --- Rule 1b: split payment via a cited invoice --------------------------

def test_rule1_overpay_with_exact_remainder_suggests_split(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", amount=2750, date="2026-01-01"),
        dict(invoice_id="INV-2", customer_name="ABC", amount=1250, date="2026-01-02"),
    ])
    momo = make_momo([dict(transaction_id="T1", amount=4000, reference="INV-1 settlement",
                            date="2026-01-03")])
    result = reconcile(invoices, momo)
    row = result["needs_review"].iloc[0]
    assert row["match_rule"] == "invoice_number_split_payment_candidate"
    assert "INV-1" in row["candidate_invoices"]
    assert "INV-2" in row["candidate_invoices"]
    # nothing is auto-applied - both invoices' balances are untouched, same
    # as every other needs_review outcome, until a human confirms.
    assert set(result["all_invoices"]["balance"]) == {2750, 1250}


def test_rule1_overpay_with_partial_remainder_suggests_split(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", amount=2750, date="2026-01-01"),
        dict(invoice_id="INV-2", customer_name="ABC", amount=5000, date="2026-01-02"),
    ])
    momo = make_momo([dict(transaction_id="T1", amount=4000, reference="INV-1 settlement",
                            date="2026-01-03")])
    result = reconcile(invoices, momo)
    row = result["needs_review"].iloc[0]
    assert row["match_rule"] == "invoice_number_split_payment_candidate"
    assert "remainder 1250.00 of 5000.00 owed" in row["candidate_invoices"]


def test_rule1_overpay_falls_back_to_generic_flag_when_no_other_invoice(make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    momo = make_momo([dict(transaction_id="T1", amount=1500, reference="INV-1 payment")])
    result = reconcile(invoices, momo)
    assert result["needs_review"].iloc[0]["match_rule"] == "invoice_number_but_amount_exceeds_balance"


def test_rule1_overpay_falls_back_to_generic_flag_when_remainder_ambiguous(make_invoices, make_momo):
    """Two other open invoices for the same customer could each absorb the
    remainder - genuinely ambiguous, so no split is suggested."""
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", amount=1000, date="2026-01-01"),
        dict(invoice_id="INV-2", customer_name="ABC", amount=5000, date="2026-01-02"),
        dict(invoice_id="INV-3", customer_name="ABC", amount=5000, date="2026-01-02"),
    ])
    momo = make_momo([dict(transaction_id="T1", amount=1500, reference="INV-1 payment",
                            date="2026-01-03")])
    result = reconcile(invoices, momo)
    assert result["needs_review"].iloc[0]["match_rule"] == "invoice_number_but_amount_exceeds_balance"


def test_rule1_split_candidate_ignores_a_different_customers_invoice(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", amount=2750, date="2026-01-01"),
        dict(invoice_id="INV-2", customer_name="XYZ", amount=1250, date="2026-01-02"),
    ])
    momo = make_momo([dict(transaction_id="T1", amount=4000, reference="INV-1 settlement",
                            date="2026-01-03")])
    result = reconcile(invoices, momo)
    assert result["needs_review"].iloc[0]["match_rule"] == "invoice_number_but_amount_exceeds_balance"


# --- Rule 2: sender identified + exact/partial amount -------------------

def test_rule2_sender_identified_by_phone_exact_amount(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", customer_phone="0977111111", amount=1000),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=1000, sender_phone="260977111111", reference="stock payment"),
    ])
    result = reconcile(invoices, momo)
    assert len(result["matched"]) == 1
    assert result["matched"].iloc[0]["match_rule"] == "sender_identified_exact_amount"


def test_rule2_sender_identified_by_name_when_phone_absent(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC Traders", amount=1000),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=1000, sender_name="ABC Traders", reference="payment"),
    ])
    result = reconcile(invoices, momo)
    assert result["matched"].iloc[0]["match_rule"] == "sender_identified_exact_amount"


def test_rule2_sender_identified_partial_amount(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", customer_phone="0977111111", amount=1000),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=300, sender_phone="0977111111", reference="payment"),
    ])
    result = reconcile(invoices, momo)
    row = result["partial"].iloc[0]
    assert row["match_rule"] == "sender_identified_partial_amount"
    assert row["remaining_balance"] == 700


def test_rule2_invoice_dated_after_payment_is_not_a_candidate(make_invoices, make_momo):
    """An invoice issued after a payment happened can't be what it paid for."""
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", customer_phone="0977111111",
             amount=1000, date="2026-02-01"),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=1000, sender_phone="0977111111",
             date="2026-01-01", reference="payment"),
    ])
    result = reconcile(invoices, momo)
    assert len(result["matched"]) == 0
    assert len(result["unmatched"]) == 1


# --- Rule 3: sender identified, amount matches a combination ------------

def test_rule3_combo_match_flagged_for_review(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", customer_phone="0977111111", amount=600),
        dict(invoice_id="INV-2", customer_name="ABC", customer_phone="0977111111", amount=400),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=1000, sender_phone="0977111111", reference="combined payment"),
    ])
    result = reconcile(invoices, momo)
    assert len(result["needs_review"]) == 1
    row = result["needs_review"].iloc[0]
    assert row["match_rule"] == "sender_identified_multi_invoice_combo"
    assert set(row["candidate_invoices"].split(", ")) == {"INV-1", "INV-2"}
    # combo match never touches balances - a human confirms first.
    assert set(result["all_invoices"]["balance"]) == {600, 400}


def test_rule3b_split_pair_suggested_when_combo_does_not_match_exactly(make_invoices, make_momo):
    """Sender identified, amount fully covers one invoice and partially
    covers a second - combo_hit only catches exact sums, so this is the
    genuinely new case split-payment detection is for."""
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", customer_phone="0977111111",
             amount=600, date="2026-01-01"),
        dict(invoice_id="INV-2", customer_name="ABC", customer_phone="0977111111",
             amount=850, date="2026-01-02"),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=900, sender_phone="0977111111",
             reference="combined payment", date="2026-01-03"),
    ])
    result = reconcile(invoices, momo)
    row = result["needs_review"].iloc[0]
    assert row["match_rule"] == "sender_identified_split_payment_candidate"
    assert "INV-1" in row["candidate_invoices"]
    assert "INV-2" in row["candidate_invoices"]
    assert "remainder 300.00 of 850.00 owed" in row["candidate_invoices"]
    # never auto-applied
    assert set(result["all_invoices"]["balance"]) == {600, 850}


def test_rule3b_falls_back_when_remainder_also_overshoots_the_next_invoice(make_invoices, make_momo):
    """The remainder after fully covering the oldest invoice is bigger than
    even the next-oldest invoice's balance - that's 3+-invoice territory,
    not a clean one-full-plus-one-partial split, so no suggestion is made."""
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", customer_phone="0977111111",
             amount=600, date="2026-01-01"),
        dict(invoice_id="INV-2", customer_name="ABC", customer_phone="0977111111",
             amount=250, date="2026-01-02"),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=1000, sender_phone="0977111111",
             reference="combined payment", date="2026-01-03"),
    ])
    result = reconcile(invoices, momo)
    assert result["needs_review"].iloc[0]["match_rule"] == "sender_identified_amount_ambiguous"


def test_rule3_ambiguous_sender_amount_falls_to_review(make_invoices, make_momo):
    """Sender identified, but the amount doesn't cleanly match one invoice,
    a combo, or anything else -> human review, not a guess."""
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", customer_phone="0977111111", amount=1000),
        dict(invoice_id="INV-2", customer_name="ABC", customer_phone="0977111111", amount=2000),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=500, sender_phone="0977111111", reference="payment"),
    ])
    result = reconcile(invoices, momo)
    assert result["needs_review"].iloc[0]["match_rule"] == "sender_identified_amount_ambiguous"


# --- Rule 4: sender unknown, amount-only matching ------------------------

def test_rule4_unique_amount_unconfirmed_sender_is_never_auto_matched(make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=777)])
    momo = make_momo([dict(transaction_id="T1", amount=777, reference="momo payment")])
    result = reconcile(invoices, momo)
    assert len(result["matched"]) == 0
    row = result["needs_review"].iloc[0]
    assert row["match_rule"] == "amount_only_unique_match_unconfirmed_sender"
    assert row["matched_invoice"] == "INV-1"


def test_rule4_multiple_candidates_ambiguous(make_invoices, make_momo):
    """Two different customers owe the same amount - must be flagged, not
    silently dropped into unmatched."""
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", amount=500),
        dict(invoice_id="INV-2", customer_name="XYZ", amount=500),
    ])
    momo = make_momo([dict(transaction_id="T1", amount=500, reference="momo payment")])
    result = reconcile(invoices, momo)
    row = result["needs_review"].iloc[0]
    assert row["match_rule"] == "amount_only_multiple_candidates_ambiguous"
    assert set(row["candidate_invoices"].split(", ")) == {"INV-1", "INV-2"}


def test_rule4_respects_date_window(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="ABC", amount=500, date="2026-01-01"),
    ])
    momo = make_momo([
        dict(transaction_id="T1", amount=500, date="2026-06-01", reference="momo payment"),
    ])
    result = reconcile(invoices, momo, date_window_days=45)
    assert len(result["needs_review"]) == 0
    assert len(result["unmatched"]) == 1


# --- Rule 5: nothing matches ----------------------------------------------

def test_rule5_unmatched_when_nothing_fits(make_invoices, make_momo):
    invoices = make_invoices([dict(invoice_id="INV-1", customer_name="ABC", amount=1000)])
    momo = make_momo([dict(transaction_id="T1", amount=42, reference="wallet topup")])
    result = reconcile(invoices, momo)
    assert len(result["unmatched"]) == 1
    assert result["unmatched"].iloc[0]["matched_invoice"] is None


# --- Duplicate invoice_id resolution --------------------------------------

def test_duplicate_invoice_id_resolved_by_exact_balance(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="A", amount=1000, date="2026-01-01"),
        dict(invoice_id="INV-1", customer_name="B", amount=2200, date="2026-01-02"),
    ])
    momo = make_momo([dict(transaction_id="T1", amount=2200, reference="INV-1 payment")])
    result = reconcile(invoices, momo)
    balances = result["all_invoices"].sort_values("date")["balance"].tolist()
    assert balances == [1000, 0]  # only the exact-balance duplicate was paid off


def test_duplicate_invoice_id_falls_back_to_earliest_dated(make_invoices, make_momo):
    invoices = make_invoices([
        dict(invoice_id="INV-1", customer_name="A", amount=1000, date="2026-01-02"),
        dict(invoice_id="INV-1", customer_name="B", amount=2200, date="2026-01-01"),
    ])
    # Amount matches neither balance exactly -> falls back to the earliest-dated row.
    momo = make_momo([dict(transaction_id="T1", amount=500, reference="INV-1 payment")])
    result = reconcile(invoices, momo)
    all_inv = result["all_invoices"].sort_values("date")
    assert all_inv.iloc[0]["balance"] == 1700  # earliest-dated (2026-01-01, amount 2200) paid down


# --- Integration: full sample_data run pinned to known-good output -------

def test_sample_data_reconciles_to_expected_outcome_counts():
    """Regression test against the shipped sample data, which the README
    says is built to exercise every rule at once."""
    invoices = load_invoices(SAMPLE_DATA / "invoices.csv")
    momo = load_momo_statement(SAMPLE_DATA / "momo_statement.csv")
    result = reconcile(invoices, momo)

    assert len(result["matched"]) == 6
    assert len(result["partial"]) == 1
    assert len(result["needs_review"]) == 3
    assert len(result["unmatched"]) == 1
    assert len(result["open_invoices"]) == 8

    rules = _rules(result)
    assert rules["TXN00892"] == "invoice_number_exact_amount"
    assert rules["TXN00933"] == "sender_identified_exact_amount"
    assert rules["TXN00941"] == "sender_identified_exact_amount"
    assert rules["TXN00974"] == "invoice_number_exact_amount"
    assert rules["TXN00980"] == "sender_identified_exact_amount"
    assert rules["TXN00981"] == "invoice_number_exact_amount"
    assert rules["TXN00901"] == "sender_identified_partial_amount"
    assert rules["TXN00915"] == "invoice_number_split_payment_candidate"
    assert rules["TXN00920"] == "sender_identified_multi_invoice_combo"
    assert rules["TXN00958"] == "amount_only_multiple_candidates_ambiguous"
    assert rules["TXN00966"] is None  # unmatched
