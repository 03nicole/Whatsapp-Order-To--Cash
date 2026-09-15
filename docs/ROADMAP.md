# Roadmap — WhatsApp Order-to-Cash for FMCG Distributors

See [REQUIREMENTS.md](REQUIREMENTS.md) and [ARCHITECTURE.md](ARCHITECTURE.md)
for the reasoning behind each phase. Phases 1–7, 9, and 10 are **done
and tested**; Phase 8 and Phase 11 are scoped but not yet built. Each
phase names its
exit criteria — the point at which it's proven enough to justify starting
the next one — because building ahead of validation is exactly the
mistake this project has avoided so far (see the original README's own
"don't extend into order capture... until you've watched a few real
distributors" caution, and the split-payment/web-UI work that only
happened once the CLI's core was proven and tested).

## Done

| Phase | What | Proof it's done |
|---|---|---|
| 1 | Reconciliation engine — deterministic rules waterfall (`matcher.py`) | 48 passing tests, including a pinned regression test against `sample_data/` |
| 2 | Persistence & accounts-receivable/aging (`db.py`) | Re-upload doesn't undo a partial payment; overlapping statements don't double-count; aging buckets tested at exact day boundaries |
| 3 | Split-payment detection | One invoice fully paid + remainder credited to another, surfaced (never auto-applied) in the review sheet |
| 4 | Local web UI (`app.py`) | Upload → outcome counts → downloadable Excel report; aging view; 10 tests |
| 5 | WhatsApp order capture → invoice generation (`orders.py`, `whatsapp.py`) | Structured catalog-code/name parsing waterfall (never guesses on an ambiguous or unknown product, exactly like the reconciliation waterfall never guesses on amount alone); stock check gates confirmation; a confirmed order writes into the **same** `invoices` table a manual upload would. Proven with a real webhook payload driven against a live running server, not just the test client — see the "end-to-end" test below. `matcher.py` and `report.py` untouched; `db.py` gained new tables/helpers plus one fix (`combine_with_open_invoices` — see below) |
| 5b | Order capture via WhatsApp's native Catalog/Cart checkout (`orders.resolve_native_order`, `whatsapp.parse_order_messages`) | Handles Meta's `"order"`-type webhook message (a completed native-catalog checkout, already structured as exact `product_retailer_id`s + quantities) as a second, parallel entry point into the *same* `_finalize_order()` tail Phase 5's free-text path uses — invoice generation, stock check, and the "never partially confirm" invariant are all shared, not reimplemented. Depends on the distributor's Meta Commerce Catalog being set up with each product's `retailer_id` matching this system's own `product_id` — an operational setup step, not something the code can verify; a mismatch flags the whole order rather than guessing. Built and tested against Meta's documented payload shape (`catalog_id`, `product_items[].product_retailer_id/quantity/item_price`), no live Meta Commerce Catalog account exists. 12 new tests, including a mixed-batch test (one free-text message + one native-cart message in the same webhook payload, both resolve correctly). Verified live against the running server and a real open catalog. |
| 6 | Stock management beyond catalog re-import (`/catalog` page, `db.adjust_stock`/`stock_history`) | Receive new stock or correct a count for one product without re-uploading the whole file; every change — manual or order-driven — is logged to a `stock_adjustments` audit trail (same "every decision is traceable" principle as `match_rule`, applied to stock). 12 new tests, verified live against the running server |
| 7 | Warehouse fulfillment tracking (`/warehouse` picking list, `db.mark_order_fulfilled`) | One action — mark a confirmed order fulfilled, with an optional note — not a speculative multi-stage pick/pack workflow (see note below on why this was built ahead of its own gate). 17 new tests, including a migration test against a database that already had order rows from before this phase existed; verified live against the running server and its real pre-existing data |
| 9 | Owner analytics dashboard (`/analytics`) | Built entirely from numbers this tool already tracked and already treated as meaningful — the same outcome breakdown `report.py`'s Excel Summary sheet has shown since Phase 1, the same aging buckets the aging view already computes — not new invented metrics. Also built ahead of its own stated gate (see below). 16 new tests. Live verification against real accumulated demo data caught a genuine pre-existing bug in `_bucket()` (a negative days-outstanding fell through to the *worst* bucket instead of the best) — fixed, with a regression test |
| 10 | Live MoMo webhook integration (`reconciler/flutterwave.py`, `/momo/webhook`) | Resolved the direct-MTN/Airtel-vs-aggregator decision in favor of Flutterwave (see [ARCHITECTURE.md](ARCHITECTURE.md#mobile-money-zambia--phase-10-built-decision-resolved) for the sourced reasoning) — one webhook covering both telcos, built and tested against Flutterwave's real public documentation (no live account exists). Feeds a single incoming payment straight into the same `reconcile()` the CLI/web UI call on a batch. Also built ahead of its own stated gate. 19 new tests. Live verification against a real accumulated open invoice caught a second timezone bug — `reconcile()` itself now normalizes both DataFrames' dates, not just `db.open_invoices()` |

**Phases 7, 9, and 10 were built deliberately ahead of their own stated
gates.** Unlike Phases 5–6 (framed from the start as low-risk extensions
of the already-validated reconciliation engine), this roadmap's own
Phase 7 entry originally said "do not build before Phase 5–6 are live
with a real pilot... this phase has no software-design work worth doing
yet, only operational learning to gather first," and Phase 9's said
"do not build before a few months of real reconciled data exist... an
analytics dashboard against sample data would be building for a
hypothetical, not a customer." Neither gate was satisfied — no real
pilot, and no real reconciled data, exist yet. In both cases the user
was asked directly whether to hold off or proceed anyway, and chose to
proceed each time - asked separately, not assumed: the Phase 7 answer
was never treated as covering Phase 9's gate too. This is a real,
accepted risk in both cases: Phase 7's
single "mark fulfilled" action is a guess at the right granularity, and
Phase 9's metrics/thresholds are chosen from what the tool already
tracked rather than from what a real owner asked to see. Phase 8, 10,
and 11 inherit the same accepted-risk *option* but not the *decision* —
each still needs to be asked about on its own.

**One thing Phase 5 exposed and fixed in the existing persistence layer:**
`merge_persisted_balances()` only overrides the balance of an invoice
already present in a freshly-uploaded file — it had no way to surface an
invoice that exists *purely* in the database, which is exactly what an
order-generated invoice is (no accompanying upload ever created it). Fixed
via a new `db.combine_with_open_invoices()`, now used by both `cli.py` and
`app.py` in place of the old bare `merge_persisted_balances()` call — a
MoMo statement now reconciles against every currently-open invoice a
business has, not just whichever ones happen to be in this particular
upload. Covered by `test_order_generated_invoice_reconciles_against_a_real_momo_payment`
in `tests/test_orders_app.py`, which drives the real `/reconcile` web route
end to end, and by dedicated tests in `test_orders_db.py`.

## Next

### Phase 8 — Delivery assignment/tracking *(validation-gated — still unbuilt)*
Same caveat Phase 7 originally had and built past anyway — needs real order-volume and delivery-pattern
data to design against, not assumptions.

### Phase 11 — ZRA Smart Invoice fiscalization *(deferred until needed)*
The data model already reserves the fields this would need
(see [DATA_MODEL.md](DATA_MODEL.md)); building the actual integration
waits until a pilot customer's compliance requirements make it
necessary, or ZRA enforcement makes it unavoidable.

## Validation gates

Two different kinds of validation are in play, and it's worth being
explicit about which one has actually happened for which decision. See
[VALIDATION_INTERVIEW_GUIDE.md](VALIDATION_INTERVIEW_GUIDE.md) for how to
actually go run the direct field validation described below - not just
the criteria, but the interview structure, scoring, and what to do with
the result per prospect.

1. **Market-level validation** (funded regional comps — Wasoko, Twiga,
   Chpter, Sukhiba — plus the Smart Invoice regulatory tailwind, which
   desk research confirms is *already mandatory* for VAT-registered
   taxpayers since July 2024, not just an upcoming tailwind).
   **This has been done, with one correction as of 2026-09-15:** Wasoko
   itself is already operating in Zambia (Lusaka as its Southern Africa
   hub since May 2023) — not an entrenched competitor for *this
   product* specifically (it's a direct-to-retailer distributor, not
   software sold to distributors — see
   [ARCHITECTURE.md](ARCHITECTURE.md#wasoko-formerly-sokowatch--now-operating-in-zambia-itself-not-just-a-reference-market)),
   but its presence changes what "no entrenched Zambian competitor"
   actually means and is now a specific thing to ask prospects about.
   Sukhiba (the closer direct analog — B2B WhatsApp commerce sold *to*
   distributors) has no confirmed Zambia presence yet. This was still
   enough to justify choosing this idea over Contractor Operations OS
   or Garage Management OS, but the "blank slate" framing needs
   updating, not repeating uncritically.
2. **Direct field validation** (watching real Lusaka distributors
   reconcile their own payments; confirming they have exportable
   invoices/statements, that customers pay into business-owned MoMo
   lines rather than individual salespeople's personal numbers, and that
   manual reconciliation is costing them real hours or money). **This has
   not been done yet** — it's the original README's caution, and it
   still gates Phases 8 and 11. Phases 5 and 6 were built ahead of it
   deliberately, on the reasoning that both extend the already-validated
   reconciliation engine (every new invoice still lands in the same
   table, still goes through the same waterfall; every stock change is
   logged the same way a match rule is) rather than committing to
   warehouse/delivery complexity - a genuinely low-risk case. **Phases 7,
   9, and 10 are different:** none was that kind of low-risk extension -
   Phase 7 is exactly the "no software-design work worth doing yet, only
   operational learning to gather first" category this gate exists for;
   Phase 9 is exactly the "would be building for a hypothetical, not a
   customer" category; Phase 10 is exactly the "statement-upload hasn't
   earned its keep with a real pilot yet" category, and additionally
   required picking a vendor (Flutterwave over direct MTN/Airtel) with no
   real transaction-volume/fee data to weigh the choice against - all
   three got built anyway because the user was asked directly, separately,
   for each one, and chose to proceed every time. That's a legitimate
   call, but don't retroactively treat any of them as if they'd been
   low-risk all along, and don't assume Phase 8 or 11 get the same
   treatment without asking again - each "build anyway" was answered
   once, for one phase, not as a standing instruction to skip this gate
   going forward.
