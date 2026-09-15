# Reconciliation Engine — V0/V1

[![Tests](https://github.com/03nicole/Whatsapp-Order-To--Cash/actions/workflows/tests.yml/badge.svg)](https://github.com/03nicole/Whatsapp-Order-To--Cash/actions/workflows/tests.yml)

Matches a business's MoMo statement against its invoices and produces an
Excel report showing what's matched, partially paid, needs a human look, or
unmatched. This is the V0 "manual service" tool from the plan: you run it on
a real distributor's data by hand, by email, or over a call — not a live
SaaS yet.

This is one component of a larger confirmed product direction — a
WhatsApp Order-to-Cash platform for FMCG wholesalers/distributors
(Zambia first). This repo now covers the **reconciliation stage**
(below) *and* Phase 5, **WhatsApp order capture → invoice generation**
(`reconciler/orders.py`, `reconciler/whatsapp.py` — a structured,
catalog-driven order-parsing waterfall with the same "never guess when
unsure" principle as the matcher, gated by a stock check, writing
straight into the same `invoices` table a manual upload would). Deeper
stock management, warehouse, and delivery are still scoped but not yet
built — see [`docs/ROADMAP.md`](docs/ROADMAP.md) for exactly where the
line is and why. Full system analysis:

- [Requirements](docs/REQUIREMENTS.md) — problem statement, ICP, actors, functional/non-functional requirements, scope
- [Architecture](docs/ARCHITECTURE.md) — component design, reference systems (Wasoko, Twiga Foods, Safaricom Daraja/M-Pesa, EU PEPPOL/EN16931), tech stack decisions
- [Data model](docs/DATA_MODEL.md) — entity-relationship diagram, how new entities extend this repo's existing schema
- [Roadmap](docs/ROADMAP.md) — phased plan with explicit validation gates per phase

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

There's also a **split-payment detector** layered onto rules 1 and 3: when a
transfer overpays one cited/identified invoice, it checks whether the
leftover cleanly covers (in full or in part) exactly one other open invoice
for the same customer, and — if so — names both invoices and the leftover
amount in the review sheet's Details column, instead of a bare "amount
exceeds balance" message. It's still `needs_review`, never auto-applied —
same as every other flagged case here — it just gives the human a concrete
starting point instead of nothing.

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
cited invoice that turns out to be a clean split-payment candidate against
that customer's other open invoice, a genuinely ambiguous case where two
customers owe the same amount, and two invoices that were accidentally
issued under the same invoice number — resolved correctly by balance rather
than file order) so you can see what each outcome looks like before you're
staring at a real customer's messy export.

## Web UI

A local browser front-end for the same two commands (`cli.py`'s `reconcile`
and `aging`) - upload the two files, see the outcome counts, download the
Excel report. No new matching logic lives here; it's a thin wrapper around
the same `reconciler` package the CLI uses, and shares `reconciliation.db`
with it.

```bash
pip install -r requirements-web.txt
python app.py
```

Then open http://127.0.0.1:5000. This is meant to be run locally by whoever
is doing the reconciliation - not deployed as a public-facing service. Set
`RECONCILIATION_DB` to point it at a different SQLite file (e.g. to keep a
pilot business's data separate) instead of the default `reconciliation.db`
next to `app.py`.

## Order capture (WhatsApp)

Phase 5 of the roadmap: a customer's WhatsApp order becomes an invoice in
the *same* `invoices` table the reconciliation engine already reads -
not a parallel system. See `docs/ARCHITECTURE.md` and `docs/ROADMAP.md`
for the full reasoning; this is the practical how-to.

**1. Import a product catalog** (from the web UI's homepage, or
`reconciler.load_catalog` directly) - a CSV/XLSX with a product code,
name, unit, price, and stock-on-hand. Re-importing later updates
price/stock for existing codes instead of duplicating them.

**2. Point a WhatsApp webhook at `/whatsapp/webhook?business=<name>`.**
One URL per business for now (see `app.py`'s `whatsapp_webhook_receive`
docstring - multi-number routing is a fast-follow, not needed for a
single pilot). Set `WHATSAPP_VERIFY_TOKEN` to whatever verify token you
register with Meta/your BSP for the GET handshake.

**3. Without real WhatsApp credentials, nothing here is blocked** - the
outbound side defaults to `reconciler.whatsapp.LoggingWhatsAppClient`,
which just records what would have been sent. Set
`WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` to switch to the
real Meta Cloud API client once you have them (see
`docs/ARCHITECTURE.md#7-external-api-strategy-in-detail` for the
direct-API-vs-BSP decision to make first).

An incoming order message like `10 COKE-24, 5 FANTA-24` is parsed
against the catalog by product code first, then by unique product name;
an ambiguous or unrecognized product, or a line that exceeds stock on
hand, flags the **whole** order for a human rather than guessing at part
of it - visible on the web UI's `/orders` page. A cleanly-resolved order
writes an invoice, decrements stock, and confirms back to the customer
over WhatsApp (or into the logging client's record, in dev).

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
it doubles as a regression test for the waterfall as a whole. Also covers
the order-capture waterfall (product resolution, stock gating, never
partially confirming an order), the WhatsApp webhook payload parsing, and
- in `test_orders_app.py::test_order_generated_invoice_reconciles_against_a_real_momo_payment`
- an end-to-end proof that an order-generated invoice reconciles through
the real `/reconcile` web route exactly like a manually-uploaded one.

## If a real file's columns aren't recognized

`reconciler/loaders.py` has an alias list per field (`INVOICE_ALIASES`,
`MOMO_ALIASES`, `CATALOG_ALIASES`). If you get a `Could not find a column for: [...]` error,
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
- **A payment can only be matched, once, to one thing.** A single
  transaction is never auto-applied across two invoices — split payments
  (paying one invoice in full and leaving a partial credit toward another)
  are *detected* and named in the review sheet when there's exactly one
  clean candidate (see "split-payment detector" above), but a human still
  has to confirm and apply it. Genuinely three-way splits, or cases where
  more than one invoice could plausibly absorb the leftover, still fall
  back to a generic review flag rather than a guess.
- **This does not touch money.** It only reads a statement export and an
  invoice list — it never initiates, holds, or confirms a payment on its
  own. Keep it that way for as long as possible; it's a much easier thing
  for a business owner to trust.
- **Order capture (`orders.py`) has never talked to a real WhatsApp
  account.** Everything is proven against simulated webhook payloads and
  a logging stub for outbound messages (`reconciler.whatsapp.LoggingWhatsAppClient`)
  - see `docs/ARCHITECTURE.md`'s direct-API-vs-BSP decision, still open,
  before pointing this at Meta for real.
- **No free-text order parsing.** Product references must match a
  catalog code or name closely enough to resolve uniquely; "10 boxes of
  the usual" won't. By design for now — see `docs/ROADMAP.md` Phase 5.
- **Stock can only be adjusted by re-importing the whole catalog file.**
  No UI yet for receiving new stock or correcting a miscount in place —
  see `docs/ROADMAP.md` Phase 6.

## What the interviews should tell you before you build past this

Per the original validation plan: don't extend this into inventory or
delivery until you've watched a few real distributors reconcile their own
payments and confirmed (a) they have exportable invoices and statements at
all, (b) customers mostly pay into a small number of *business-owned* MoMo
lines rather than individual salespeople's personal numbers, and (c)
manual reconciliation is costing them real hours or real money, not just
mild annoyance. If (b) turns out false for a prospect, this tool's matching
logic can't fix that — it's an organizational problem, not a data problem.

**Order capture was built ahead of this caution, deliberately** - see
`docs/ROADMAP.md`'s "Validation gates" section for why: it extends the
already-validated reconciliation engine (every invoice it produces still
lands in the same table, still goes through the same waterfall) rather
than committing to warehouse/delivery complexity, and a separate,
market-level validation (funded regional comps, a regulatory tailwind, no
entrenched local competitor) already justified the broader direction. The
distributor-interview caution above still fully applies to inventory,
warehouse, and delivery - those remain unbuilt until it's satisfied.
