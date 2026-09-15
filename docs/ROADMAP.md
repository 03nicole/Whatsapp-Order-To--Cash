# Roadmap — WhatsApp Order-to-Cash for FMCG Distributors

See [REQUIREMENTS.md](REQUIREMENTS.md) and [ARCHITECTURE.md](ARCHITECTURE.md)
for the reasoning behind each phase. Phases 1–4 are **done and tested**;
everything from Phase 5 is scoped but not yet built. Each phase names its
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

## Next

### Phase 5 — WhatsApp order capture → invoice generation
**Goal:** a customer's WhatsApp order becomes an invoice in the existing
schema, without touching `matcher.py`/`db.py`/`report.py`.
**Build:** structured (catalog/menu-driven) ordering first — free-text
NLP is an optimization, not a prerequisite, per
[ARCHITECTURE.md](ARCHITECTURE.md#1-architectural-principles) principle 3.
**Exit criteria:** one real pilot customer's customers place real orders
this way, and the resulting invoices flow into the reconciliation engine
with no schema hacks.

### Phase 6 — Basic stock-on-hand check
**Goal:** an order can be checked against a single warehouse's quantity-
on-hand before it's confirmed.
**Build:** `STOCK_ITEM` per [DATA_MODEL.md](DATA_MODEL.md) — no batches,
expiry, or multi-warehouse logic yet.
**Exit criteria:** the pilot's actual catalog size/complexity is known,
and this simple model either holds up or names exactly what's missing.

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
[ARCHITECTURE.md](ARCHITECTURE.md#reference-safaricom-daraja-api-m-pesa).
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
   case for the overall direction is strong. Phase 5–6 are low-risk
   enough to build ahead of it (they extend the already-validated
   reconciliation engine rather than committing to warehouse/delivery
   complexity); Phases 7 onward are not.
