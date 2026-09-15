"""
The matching engine.

Waterfall, applied per MoMo transaction in date order (so an invoice's
balance is consumed in the same sequence the payments actually happened):

  1. Invoice number found in the payment reference        -> high confidence
  2. Sender identified (phone/name) + exactly one of their
     open invoices matches the amount                     -> high confidence
  3. Sender identified + amount matches a *combination* of
     their open invoices (multi-invoice payment)           -> needs review
  3b. One invoice cited/identified is fully covered and the leftover is a
      full/partial credit toward exactly one other open invoice of the
      same customer (a "split payment")                    -> needs review
  4. Amount is unique across ALL open invoices, but sender
     could not be identified                                -> needs review
     (never auto-confirmed on amount alone — two customers can owe the
     same amount, so this always goes to a human)
  5. Nothing matches                                         -> unmatched

Split payments (3b) are always flagged, never auto-applied — even the
"obviously covered" invoice's balance is left untouched, same as every other
needs_review outcome, since a human might reject the whole suggestion. When
more than one (primary, secondary) invoice pairing could explain the amount,
it's left ambiguous rather than guessed at, exactly like the amount-only
rules below.

Two extension points are deliberately left as stubs, not built yet:
  - `customer_historical_pattern()` — once you have months of confirmed
    matches, a customer's typical payment amount/timing becomes a signal.
  - `fuzzy_match_customer()` — handles misspelled names ("Kunda Traders"
    vs "Kunda Trading") once rule-based matching stops being enough.
  Both are called from `reconcile()` but currently return None immediately,
  so wiring them up later doesn't require touching the waterfall logic.
"""

from __future__ import annotations

import re
from itertools import combinations

import pandas as pd


def _normalize_ref(text: str) -> str:
    """Strip everything but letters/digits and uppercase, for loose matching."""
    return re.sub(r"[^A-Za-z0-9]", "", str(text)).upper()


def _normalize_phone(phone: str) -> str:
    """Compare phone numbers by their last 9 digits (drops +260/0 prefixes)."""
    digits = re.sub(r"\D", "", str(phone))
    return digits[-9:] if len(digits) >= 9 else digits


def _naive_datetime(series: pd.Series) -> pd.Series:
    """Coerces a datetime column to timezone-naive, converting via UTC
    first if it's tz-aware. reconcile()'s date comparisons assume both
    DataFrames' `date` columns are directly comparable - true for two
    CSV-derived columns, not guaranteed once a live webhook's tz-aware
    ISO timestamp (reconciler/flutterwave.py) or a database round-trip
    (db.open_invoices()) is one of the two sides."""
    if getattr(series.dt, "tz", None) is not None:
        return series.dt.tz_convert("UTC").dt.tz_localize(None)
    return series


def find_invoice_in_reference(reference: str, open_invoice_ids: list[str]) -> str | None:
    """Return the invoice_id if it appears (in any punctuation form) in the reference text."""
    norm_ref = _normalize_ref(reference)
    if not norm_ref:
        return None
    for inv_id in open_invoice_ids:
        norm_inv = _normalize_ref(inv_id)
        if norm_inv and norm_inv in norm_ref:
            return inv_id
    return None


def customer_historical_pattern(transaction: pd.Series, candidate_invoices: pd.DataFrame):
    """Stub. Future: use a customer's confirmed match history to break ties
    (e.g. 'this customer always pays within 3 days of the invoice date')."""
    return None


def fuzzy_match_customer(name: str, known_names: list[str]):
    """Stub. Future: rapidfuzz/difflib matching for misspelled customer names."""
    return None


def reconcile(invoices_df: pd.DataFrame, momo_df: pd.DataFrame,
              date_window_days: int = 45) -> dict[str, pd.DataFrame]:
    """
    Runs the full waterfall over every transaction and returns:
        {
          "matched":      DataFrame,
          "partial":      DataFrame,
          "needs_review": DataFrame,
          "unmatched":    DataFrame,
          "open_invoices": DataFrame,   # remaining balances after all matching
        }
    `invoices_df` / `momo_df` are usually the outputs of
    loaders.load_invoices / loaders.load_momo_statement, but not always
    any more - db.open_invoices() and reconciler/flutterwave.py's
    to_momo_dataframe() feed this function too, and don't all agree on
    whether their `date` column carries a timezone (a live webhook's
    ISO timestamp typically does; a CSV-uploaded date never does).
    Comparing a tz-aware Timestamp against a naive one raises, so both
    date columns are normalized to naive here - the one place every
    producer's output actually converges - rather than trusting each
    producer to remember. This function does not mutate its inputs.
    """
    invoices = invoices_df.copy()
    invoices["invoice_id"] = invoices["invoice_id"].astype(str)
    if "date" in invoices.columns:
        invoices["date"] = _naive_datetime(invoices["date"])
    momo_df = momo_df.copy()
    if "date" in momo_df.columns:
        momo_df["date"] = _naive_datetime(momo_df["date"])

    # phone/name -> list of invoice row indices, for identifying the sender
    phone_index: dict[str, list[int]] = {}
    name_index: dict[str, list[int]] = {}
    for idx, row in invoices.iterrows():
        p = _normalize_phone(row.get("customer_phone", ""))
        n = str(row.get("customer_name", "")).strip().lower()
        if p:
            phone_index.setdefault(p, []).append(idx)
        if n:
            name_index.setdefault(n, []).append(idx)

    matched_rows, partial_rows, review_rows, unmatched_rows = [], [], [], []

    for _, txn in momo_df.iterrows():
        open_ids = invoices.loc[invoices["balance"] > 0, "invoice_id"].tolist()

        # --- Rule 1: invoice number in the reference text ---------------
        inv_id = find_invoice_in_reference(txn["reference"], open_ids)
        if inv_id is not None:
            amount = txn["amount"]
            inv_idx = _resolve_duplicate_invoice_id(invoices, inv_id, amount)
            balance = invoices.at[inv_idx, "balance"]

            if abs(amount - balance) < 0.01:
                invoices.at[inv_idx, "balance"] = 0
                matched_rows.append(_row(txn, inv_id, invoices.at[inv_idx, "customer_name"],
                                          "invoice_number_exact_amount"))
                continue
            if amount < balance:
                invoices.at[inv_idx, "balance"] = round(balance - amount, 2)
                partial_rows.append(_row(txn, inv_id, invoices.at[inv_idx, "customer_name"],
                                          "invoice_number_partial_amount",
                                          extra={"invoice_total": invoices.at[inv_idx, "amount"],
                                                 "remaining_balance": invoices.at[inv_idx, "balance"]}))
                continue
            # amount > balance: invoice number matched but overpaid. Might be a
            # split payment - this transfer fully covers `inv_id` and the
            # leftover is a credit toward another of the same customer's open
            # invoices. Never auto-applied (see _find_split_payment_candidate)
            # - always flagged for a human, just with a concrete suggestion
            # instead of a bare "overpaid" message when one clean candidate
            # exists.
            remainder = round(amount - balance, 2)
            customer_name = invoices.at[inv_idx, "customer_name"]
            split = _find_split_payment_candidate(invoices, customer_name, inv_idx,
                                                    remainder, txn["date"])
            if split is not None:
                other_idx, other_balance = split
                other_id = invoices.at[other_idx, "invoice_id"]
                review_rows.append(_row(
                    txn, inv_id, customer_name, "invoice_number_split_payment_candidate",
                    extra={"invoice_balance": balance,
                           "candidate_invoices": f"{inv_id} (full, {balance:.2f}) + "
                                                  f"{other_id} (remainder {remainder:.2f} of "
                                                  f"{other_balance:.2f} owed)"}))
                continue

            review_rows.append(_row(txn, inv_id, invoices.at[inv_idx, "customer_name"],
                                     "invoice_number_but_amount_exceeds_balance",
                                     extra={"invoice_balance": balance}))
            continue

        # --- Rule 2/3: identify the sender, then match on amount --------
        sender_phone = _normalize_phone(txn.get("sender_phone", ""))
        sender_name = str(txn.get("sender_name", "")).strip().lower()
        candidate_idx = phone_index.get(sender_phone, []) or name_index.get(sender_name, [])
        # An invoice issued after this payment happened can't be what it paid for.
        candidate_idx = [i for i in candidate_idx
                          if invoices.at[i, "balance"] > 0 and invoices.at[i, "date"] <= txn["date"]]

        if candidate_idx:
            candidates = invoices.loc[candidate_idx]
            exact = candidates[(candidates["balance"] - txn["amount"]).abs() < 0.01]
            if len(exact) == 1:
                inv_idx = exact.index[0]
                invoices.at[inv_idx, "balance"] = 0
                matched_rows.append(_row(txn, invoices.at[inv_idx, "invoice_id"],
                                          invoices.at[inv_idx, "customer_name"],
                                          "sender_identified_exact_amount"))
                continue

            smaller = candidates[candidates["balance"] > txn["amount"]]
            if len(smaller) == 1 and len(exact) == 0:
                inv_idx = smaller.index[0]
                balance = invoices.at[inv_idx, "balance"]
                invoices.at[inv_idx, "balance"] = round(balance - txn["amount"], 2)
                partial_rows.append(_row(txn, invoices.at[inv_idx, "invoice_id"],
                                          invoices.at[inv_idx, "customer_name"],
                                          "sender_identified_partial_amount",
                                          extra={"invoice_total": invoices.at[inv_idx, "amount"],
                                                 "remaining_balance": invoices.at[inv_idx, "balance"]}))
                continue

            # Rule 3: does the amount match a *combination* of this
            # customer's open invoices? (capped at 4 invoices to keep this fast)
            combo_hit = _find_combo_match(candidates, txn["amount"], max_size=4)
            if combo_hit is not None:
                review_rows.append(_row(
                    txn, "+".join(combo_hit), candidates.at[candidates.index[0], "customer_name"],
                    "sender_identified_multi_invoice_combo",
                    extra={"candidate_invoices": ", ".join(combo_hit)}))
                continue

            # Rule 3b: split payment - fully covers one of this customer's
            # open invoices, and the remainder is a full/partial credit
            # toward exactly one other. (combo_hit above already catches the
            # case where both invoices are paid exactly in full - this only
            # fires when combo_hit found nothing, i.e. the remainder is a
            # genuine partial.) Still needs_review - never auto-applied.
            split_pair = _find_split_pair(candidates, txn["amount"])
            if split_pair is not None:
                primary_idx, secondary_idx, remainder = split_pair
                primary_id = invoices.at[primary_idx, "invoice_id"]
                secondary_id = invoices.at[secondary_idx, "invoice_id"]
                review_rows.append(_row(
                    txn, primary_id, candidates.at[candidates.index[0], "customer_name"],
                    "sender_identified_split_payment_candidate",
                    extra={"candidate_invoices":
                           f"{primary_id} (full, {invoices.at[primary_idx, 'balance']:.2f}) + "
                           f"{secondary_id} (remainder {remainder:.2f} of "
                           f"{invoices.at[secondary_idx, 'balance']:.2f} owed)"}))
                continue

            # Sender known, but amount doesn't cleanly resolve -> human review
            review_rows.append(_row(
                txn, None, str(txn.get("sender_name", "")), "sender_identified_amount_ambiguous",
                extra={"candidate_invoices": ", ".join(candidates["invoice_id"].tolist())}))
            continue

        # --- Rule 4: sender unknown, but amount is unique across ALL open
        #             invoices within the date window -> needs review,
        #             never auto-confirmed (two customers can owe the same
        #             amount, so a human must confirm identity here). ------
        window_start = txn["date"] - pd.Timedelta(days=date_window_days)
        in_window = invoices[(invoices["balance"] > 0) &
                              (invoices["date"] >= window_start) &
                              (invoices["date"] <= txn["date"])]
        amount_hits = in_window[(in_window["balance"] - txn["amount"]).abs() < 0.01]
        if len(amount_hits) == 1:
            inv_idx = amount_hits.index[0]
            review_rows.append(_row(
                txn, invoices.at[inv_idx, "invoice_id"], invoices.at[inv_idx, "customer_name"],
                "amount_only_unique_match_unconfirmed_sender"))
            continue

        if len(amount_hits) > 1:
            # This is exactly the "two customers owe the same amount" trap —
            # surface it explicitly rather than let it disappear into
            # "unmatched", where nobody would know to go looking for it.
            review_rows.append(_row(
                txn, None, None, "amount_only_multiple_candidates_ambiguous",
                extra={"candidate_invoices": ", ".join(amount_hits["invoice_id"].tolist())}))
            continue

        # --- Rule 5: nothing matched -------------------------------------
        unmatched_rows.append(_row(txn, None, None, None))

    result = {
        "matched": pd.DataFrame(matched_rows),
        "partial": pd.DataFrame(partial_rows),
        "needs_review": pd.DataFrame(review_rows),
        "unmatched": pd.DataFrame(unmatched_rows),
        "open_invoices": invoices[invoices["balance"] > 0].copy(),
        # Every invoice, including ones this run paid off to a balance of 0 —
        # "open_invoices" excludes those, but persistence (db.py) needs the
        # full picture to record that a balance actually reached zero.
        "all_invoices": invoices,
    }
    return result


def _resolve_duplicate_invoice_id(invoices: pd.DataFrame, inv_id: str, amount: float) -> int:
    """`inv_id` came from find_invoice_in_reference, so at least one open row
    with this id exists. If invoice_id isn't actually unique in the file,
    picking invoices["invoice_id"] == inv_id).index[0] regardless of balance
    would grab an already-closed duplicate instead of the one this payment is
    for. Prefer an exact-balance match among the still-open duplicates, then
    fall back to the earliest-dated one."""
    open_matches = invoices.index[(invoices["invoice_id"] == inv_id) & (invoices["balance"] > 0)]
    if len(open_matches) == 1:
        return open_matches[0]
    exact = [i for i in open_matches if abs(invoices.at[i, "balance"] - amount) < 0.01]
    if exact:
        return exact[0]
    return min(open_matches, key=lambda i: invoices.at[i, "date"])


def _find_split_payment_candidate(invoices: pd.DataFrame, customer_name: str, exclude_idx: int,
                                   remainder: float, as_of_date) -> tuple[int, float] | None:
    """After a reference-cited invoice is fully covered, does the leftover
    `remainder` fully or partially cover exactly one of the SAME customer's
    other open invoices? Returns (invoice_index, that invoice's balance) only
    when there is exactly one such candidate - multiple candidates are just
    as ambiguous as the amount-only rules elsewhere, so they're left for a
    human rather than guessed at. Never mutates `invoices`."""
    if remainder < 0.01:
        return None
    same_customer = invoices[
        (invoices.index != exclude_idx) &
        (invoices["balance"] > 0) &
        (invoices["customer_name"].str.strip().str.lower() == str(customer_name).strip().lower()) &
        (invoices["date"] <= as_of_date)
    ]
    covers_remainder = same_customer[same_customer["balance"] >= remainder - 0.01]
    if len(covers_remainder) == 1:
        idx = covers_remainder.index[0]
        return idx, invoices.at[idx, "balance"]
    return None


def _find_split_pair(candidates: pd.DataFrame, amount: float) -> tuple[int, int, float] | None:
    """Applies `amount` to one identified customer's open invoices oldest
    first (the standard AR convention - also how _resolve_duplicate_invoice_id
    breaks ties elsewhere in this module), and checks whether that lands in
    exactly the shape of a split payment: the oldest invoice fully covered,
    with a full/partial credit left for the next-oldest. Note that "does the
    amount happen to cover invoice A fully with leftover for invoice B" is
    symmetric in A and B (A+B >= amount either way round) - picking oldest
    as primary rather than searching all orderings avoids reporting two
    equally 'valid' but contradictory suggestions. Returns
    (primary_idx, secondary_idx, remainder), or None if the amount doesn't
    land in exactly that two-invoice shape (covers three+ invoices, doesn't
    fully cover even the oldest, or overshoots the second invoice too -
    that's combo/overpay territory, not this rule)."""
    if len(candidates) < 2:
        return None
    ordered = candidates.sort_values("date")
    primary_idx, primary_balance = ordered.index[0], ordered["balance"].iloc[0]
    secondary_idx, secondary_balance = ordered.index[1], ordered["balance"].iloc[1]
    if primary_balance >= amount - 0.01:
        return None  # doesn't even fully cover the oldest invoice
    remainder = round(amount - primary_balance, 2)
    if remainder >= secondary_balance - 0.01:
        return None  # remainder covers the next invoice too - not a partial credit
    return primary_idx, secondary_idx, remainder


def _find_combo_match(candidates: pd.DataFrame, amount: float, max_size: int = 4) -> list[str] | None:
    balances = list(zip(candidates["invoice_id"], candidates["balance"]))
    for size in range(2, min(max_size, len(balances)) + 1):
        for combo in combinations(balances, size):
            if abs(sum(b for _, b in combo) - amount) < 0.01:
                return [inv_id for inv_id, _ in combo]
    return None


def _row(txn: pd.Series, invoice_id, customer_name, rule, extra: dict | None = None) -> dict:
    base = {
        "transaction_id": txn["transaction_id"],
        "date": txn["date"],
        "amount": txn["amount"],
        "sender_name": txn.get("sender_name", ""),
        "sender_phone": txn.get("sender_phone", ""),
        "reference": txn.get("reference", ""),
        "matched_invoice": invoice_id,
        "customer": customer_name,
        "match_rule": rule,
    }
    if extra:
        base.update(extra)
    return base
