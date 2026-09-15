# Requirements — WhatsApp Order-to-Cash for FMCG Distributors

## 1. Problem statement

A Lusaka FMCG wholesaler (drinks, groceries, hardware, cosmetics,
electrical, spares, agri-inputs) runs order-to-cash as twelve disconnected,
manual steps: read WhatsApp, check stock, price, reply, hand-write an
invoice, send payment details, receive a MoMo screenshot, manually verify
it, update a notebook/Excel, brief the warehouse, brief the delivery
person, and reconcile sales later. Every handoff between those steps is a
place money or stock can go missing without anyone noticing until the
books don't balance.

## 2. Product statement

> Turn WhatsApp orders into invoices and automatically reconcile the
> payments.

Deliberately not "AI-powered WhatsApp ordering" — the pitch is the boring,
trustworthy truth: five of the twelve steps collapse into one system, and
the payment-matching part (the highest-trust, highest-cost-of-error part)
never guesses when it isn't sure. AI/NLP is an implementation detail that
can sit underneath later, not the headline.

## 3. Initial customer profile (ICP)

**Lusaka-based FMCG wholesalers/distributors, 3–20 sales staff, 1–3
warehouses, whose customers already order repeatedly over WhatsApp.**
Explicitly narrower than "wholesalers" — a beverage distributor, a food
wholesaler, a cosmetics wholesaler, a hardware distributor. Same
underlying software; the first sale is what proves it, so the segment has
to be one where the WhatsApp-ordering behavior already exists (per
[the go/no-go validation criteria](ROADMAP.md#validation-gates) — this
isn't assumed, it's a thing to confirm per prospect).

## 4. Actors

| Actor | Role in the flow |
|---|---|
| Customer | Retailer/shop owner placing repeat orders over WhatsApp |
| Sales agent | Owns the WhatsApp relationship; confirms/adjusts orders a bot gets unsure about |
| Warehouse staff | Picks/packs the order (Phase 7, deferred) |
| Delivery rider/driver | Last-mile delivery (Phase 8, deferred) |
| Business owner / finance admin | Reads the AR/aging view, resolves `needs_review` transactions, watches analytics (Phase 9, deferred) |
| WhatsApp Business Platform | External system — message transport |
| Mobile money provider (MTN MoMo, Airtel Money) | External system — payment rail |
| ZRA Smart Invoice (future) | External system — fiscal compliance (Phase 11, deferred) |

## 5. Scope

### In scope for the current build phase (Phase 5 onward — see [ROADMAP.md](ROADMAP.md))
- Structured WhatsApp order capture (menu/catalog-driven first; free-text NLP is an optimization, not a prerequisite)
- Basic stock-on-hand check against a single warehouse's ledger
- Invoice generation from a confirmed order, feeding the **existing**
  invoice schema the reconciliation engine already consumes
- Payment via MoMo, reconciled by the engine that already exists (`reconciler/matcher.py`, `db.py`, `report.py` — unchanged)

### Explicitly deferred (with why)
| Deferred | Why |
|---|---|
| Warehouse pick/pack optimization | Needs real fulfillment-time data first; premature to model routing before one warehouse's flow is proven |
| Delivery assignment/tracking | Same — depends on order volume patterns not yet observed |
| Multi-warehouse stock allocation | Only matters once a pilot actually runs >1 warehouse |
| Owner-facing analytics dashboards | Needs a few months of real reconciled data to be worth building against |
| Live MoMo webhook integration (replacing statement upload) | Statement-upload reconciliation has to first prove it's valuable enough to justify live API access & webhook infra (see [ARCHITECTURE.md](ARCHITECTURE.md#reference-safaricom-daraja-api-m-pesa)) |
| ZRA Smart Invoice fiscalization | Not yet a blocker for any pilot customer; data model is kept compatible with it (see [DATA_MODEL.md](DATA_MODEL.md)) so it's additive later, not a migration |
| PDF statement ingestion | Still no real sample file to build the parser against responsibly |

## 6. Functional requirements

**Ordering**
- FR-1: A customer can place an order by sending a WhatsApp message.
- FR-2: The system presents/confirms a catalog-backed order (product, quantity) rather than free-text guessing at first; free-text parsing is additive once structured ordering is proven.
- FR-3: An ambiguous or unparseable order is routed to the sales agent, never guessed at — same trust principle as the existing reconciliation engine's `needs_review` bucket.

**Stock**
- FR-4: The system checks requested quantities against a stock-on-hand figure per product before quoting.
- FR-5: An order (or line) that exceeds available stock is flagged for the sales agent, not silently reduced or rejected without explanation.

**Invoicing**
- FR-6: A confirmed order generates exactly one invoice, in the schema the existing `reconciler.loaders.load_invoices` / `db.py` already expect (see [DATA_MODEL.md](DATA_MODEL.md)).
- FR-7: The customer receives the invoice total and payment instructions (MoMo number/details) over WhatsApp.

**Payment & reconciliation (already built — carried forward, not rebuilt)**
- FR-8: Payments are matched to invoices via the existing deterministic waterfall in `matcher.py`; this requirement is already satisfied and must not regress (guarded by the existing 58-test suite, including the pinned `test_sample_data_reconciles_to_expected_outcome_counts` regression test).
- FR-9: Accounts-receivable/aging (`db.py`, `cli.py aging`, `app.py`'s aging view) continues to reflect balances accumulated from this new order flow exactly as it does from manually-uploaded invoice files today — the order-capture flow is a new *producer* of invoices, not a parallel reconciliation path.

**Notifications**
- FR-10: The customer is notified of order confirmation, invoice, and (once reconciled) payment confirmation over WhatsApp.

## 7. Non-functional requirements

- **NFR-1 (Trust):** No automated step may commit money-affecting state (mark an invoice paid, confirm an order against insufficient stock) without a named, deterministic rule behind it — the same invariant `matcher.py` already enforces for payment matching extends to order/invoice generation.
- **NFR-2 (Auditability):** Every automated decision records which rule produced it (mirrors `match_rule` in the existing schema) so a human can always see *why* the system did what it did.
- **NFR-3 (Connectivity tolerance):** Sales staff and customers may have patchy connectivity; WhatsApp message handling must be asynchronous/queue-tolerant, not require a live round-trip to complete.
- **NFR-4 (Localization):** Zambian Kwacha formatting, MTN/Airtel phone-number normalization (already implemented in `matcher._normalize_phone`) extended consistently to the ordering flow.
- **NFR-5 (Regulatory readiness):** The invoice data model carries the fields a fiscal/e-invoicing integration (ZRA Smart Invoice, or the PEPPOL/EN16931-style discipline it resembles) would need, even before that integration is built — see [ARCHITECTURE.md](ARCHITECTURE.md#reference-peppol--en16931-europe).

## 8. Core use cases

- **UC-1** Customer places an order via WhatsApp.
- **UC-2** Sales agent confirms/adjusts an order the bot flagged (ambiguous product, insufficient stock).
- **UC-3** System generates an invoice and sends payment instructions.
- **UC-4** Customer pays via MoMo.
- **UC-5** System reconciles the payment against the invoice — **existing engine, unchanged**.
- **UC-6** Business owner reviews the AR/aging view and resolves anything in `needs_review` — **existing UI, unchanged**.
- **UC-7** *(deferred)* Warehouse fulfillment and delivery assignment.
