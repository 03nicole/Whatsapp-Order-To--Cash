# Roadmap — WhatsApp Order-to-Cash for FMCG Distributors

See [REQUIREMENTS.md](REQUIREMENTS.md) and [ARCHITECTURE.md](ARCHITECTURE.md)
for the reasoning behind each phase. Phases 1–5 are **done and tested**;
everything from Phase 6 is scoped but not yet built. Each phase names its
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

### Phase 6 — Stock management beyond catalog re-import
**What's already done, as part of Phase 5:** the stock check itself
(`orders.check_stock`) — an order can't confirm without enough
`quantity_on_hand`, and re-importing the catalog CSV updates stock. No
batches, expiry, or multi-warehouse logic, matching the single-warehouse
ICP.
**What's still missing:** any way to adjust stock *without* re-uploading
the whole catalog — receiving new stock, correcting a miscount, voiding a
flagged order's would-be reservation. Right now the only lever is a full
catalog re-import.
**Exit criteria:** the pilot's actual catalog size/complexity and how
often stock actually changes are known, and this simple model either
holds up or names exactly what's missing.

### Phase 7 — Warehouse fulfillment tracking *(validation-gated)*
**Goal:** track pick/pack status for a confirmed order.
**Do not build before:** Phase 5–6 are live with a real pilot — this
phase has no software-design work worth doing yet, only operational
learning to gather first.

### Phase 8 — Delivery assignment/tracking *(validation-gated)*
Same caveat as Phase 7 — needs real order-volume and delivery-pattern
data to design against, not assumptions.

### Phase 9 — Owner analytics dashboard *(validation-gated)*
**Do not build before:** a few months of real reconciled data exist to
build against. An analytics dashboard against sample data would be
building for a hypothetical, not a customer.

### Phase 10 — Live MoMo webhook integration *(validation-gated)*
Replaces statement-upload reconciliation with the Daraja-style live
callback pattern described in
[ARCHITECTURE.md](ARCHITECTURE.md#safaricom-daraja-api-m-pesa--kenya-mobile-money-integration).
**Do not build before:** statement-upload reconciliation has demonstrably
earned its keep with at least one paying/committed pilot — this was
already the stated reasoning for deferring it before the order-to-cash
vision was confirmed, and nothing about that reasoning has changed.

### Phase 11 — ZRA Smart Invoice fiscalization *(deferred until needed)*
The data model already reserves the fields this would need
(see [DATA_MODEL.md](DATA_MODEL.md)); building the actual integration
waits until a pilot customer's compliance requirements make it
necessary, or ZRA enforcement makes it unavoidable.

## Validation gates

Two different kinds of validation are in play, and it's worth being
explicit about which one has actually happened for which decision:

1. **Market-level validation** (funded regional comps — Wasoko, Twiga,
   Chpter, Sukhiba — plus the Smart Invoice regulatory tailwind and the
   absence of an entrenched Zambian competitor). **This has been done.**
   It's what justified choosing this idea over Contractor Operations OS
   or Garage Management OS.
2. **Direct field validation** (watching real Lusaka distributors
   reconcile their own payments; confirming they have exportable
   invoices/statements, that customers pay into business-owned MoMo
   lines rather than individual salespeople's personal numbers, and that
   manual reconciliation is costing them real hours or money). **This has
   not been done yet** — it's the original README's caution, and it
   still gates Phases 7–11 specifically, even though the market-level
   case for the overall direction is strong. Phase 5 was built ahead of
   it deliberately, on the reasoning that it extends the already-
   validated reconciliation engine (every new invoice still lands in the
   same table, still goes through the same waterfall) rather than
   committing to warehouse/delivery complexity; Phase 6 is the same kind
   of low-risk extension. Phases 7 onward are not, and still wait.
