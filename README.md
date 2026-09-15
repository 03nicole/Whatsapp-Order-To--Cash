# Reconciliation Engine — V0/V1

Matches a business's MoMo statement against its invoices and produces an
Excel report showing what's matched, partially paid, needs a human look, or
unmatched. This is the V0 "manual service" tool from the plan: you run it on
a real distributor's data by hand, by email, or over a call — not a live
SaaS yet.

## Why it's built this way

The matching is a **deterministic rules waterfall**, not machine learning:

1. Invoice number found in the payment reference → highest confidence
2. Sender identified (name/phone) + exact amount match on one of their open invoices
3. Sender identified + amount matches a *combination* of their open invoices
   (they paid two invoices in one transfer)
4. Amount is unique across ALL open invoices, but sender is unknown →
   **flagged for review, never auto-confirmed** — two different customers
   can owe the same amount, and guessing wrong here is worse than not
   guessing at all
5. Multiple invoices share that amount and the sender is unknown → also
   flagged, explicitly, rather than silently dumped into "unmatched"
6. Nothing matches → unmatched

This mirrors the plan discussed: rules first, because you don't have enough
labelled data for ML yet, and because a wrong automatic match is much more
expensive to a business than a transaction sitting in "needs review" for a
day. Once you've run this against a few real distributors and have a stack
of human-confirmed matches, `matcher.py` has two clearly marked stub
functions (`customer_historical_pattern`, `fuzzy_match_customer`) that are
where that learned matching plugs in later — the waterfall doesn't need to
change shape to add them.

## Running it

```bash
pip install pandas openpyxl
python cli.py reconcile sample_data/invoices.csv sample_data/momo_statement.csv \
    --output report.xlsx --business "Sample Distributor Ltd"
```

Results also persist to a local SQLite file (`--db`, default
`reconciliation.db`), scoped by `--business`. That's what makes this work
correctly across repeated runs — the invoice balances and processed
transaction IDs accumulate there instead of resetting every time, so:

- Re-uploading the *same* invoice file later (a business will keep exporting
  its full invoice list, not just the new ones) doesn't undo a payment a
  previous run already applied — the persisted balance wins over the
  freshly-loaded one.
- Re-sending an overlapping statement export doesn't double-count a
  transaction already processed under the same `--business`.

Once at least one `reconcile` run has persisted data, check "who owes me
money right now" without re-uploading anything:

```bash
python cli.py aging --business "Sample Distributor Ltd"
```

This prints a per-customer summary (0-30/31-60/61-90/90+ day buckets) and
writes a two-sheet Excel report (`--output`, default `aging_report.xlsx`).

Both inputs accept `.csv` or `.xlsx`. Try it on the sample data first — it's
built to exercise every rule at once (an exact match, a two-part partial
payment, a combined payment across two invoices, an overpayment against a
cited invoice, a genuinely ambiguous case where two customers owe the same
amount, and two invoices that were accidentally issued under the same
invoice number — resolved correctly by balance rather than file order) so
you can see what each outcome looks like before you're staring at a real
customer's messy export.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

Covers the loader alias/parsing rules, every step of the matching waterfall
(including the duplicate-invoice-id and date-window edge cases), the SQLite
persistence guarantees described above (re-upload doesn't undo a partial
payment, an overlapping statement export isn't double-counted), and the
aging-bucket math. `test_matcher.py::test_sample_data_reconciles_to_expected_outcome_counts`
pins the full sample-data run to its known-good outcome per transaction, so
it doubles as a regression test for the waterfall as a whole.

## If a real file's columns aren't recognized

`reconciler/loaders.py` has an alias list per field (`INVOICE_ALIASES`,
`MOMO_ALIASES`). If you get a `Could not find a column for: [...]` error,
it's telling you exactly which field it couldn't find and what headers it
did see — add the real header text (lowercased) to the right list. This
will happen constantly in the field; every business's export looks
different. Don't take it as a bug, take it as the log of every format
you've now made the tool handle.

## Known limitations — read this before your first real pilot

- **No PDF statement support yet.** Some MoMo statements only come as PDFs.
  You'll need to convert to CSV/Excel by hand for now, or extend
  `loaders.py` with a PDF table extractor once you know how common this is.
- **No direct MoMo API integration.** By design, per the plan — statement
  upload first, API integration only once you know it's worth building.
- **Combination matching is capped at 4 invoices** for performance
  (`_find_combo_match(max_size=4)` in `matcher.py`). Fine for now; revisit
  if a real customer routinely pays 5+ invoices in one transfer.
- **A payment can only be matched, once, to one thing.** There's no support
  yet for a single transaction being split across matched + partial (e.g.
  paying one invoice in full and leaving a partial credit toward another).
  That will show up as a "needs review — amount exceeds balance" case
  instead. Track how often this actually happens before building for it.
- **This does not touch money.** It only reads a statement export and an
  invoice list — it never initiates, holds, or confirms a payment on its
  own. Keep it that way for as long as possible; it's a much easier thing
  for a business owner to trust.

## What the interviews should tell you before you build past this

Per the validation plan: don't extend this into order capture, inventory,
or delivery until you've watched a few real distributors reconcile their
own payments and confirmed (a) they have exportable invoices and statements
at all, (b) customers mostly pay into a small number of *business-owned*
MoMo lines rather than individual salespeople's personal numbers, and (c)
manual reconciliation is costing them real hours or real money, not just
mild annoyance. If (b) turns out false for a prospect, this tool's matching
logic can't fix that — it's an organizational problem, not a data problem.
