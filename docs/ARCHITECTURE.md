# Architecture — WhatsApp Order-to-Cash for FMCG Distributors

See [REQUIREMENTS.md](REQUIREMENTS.md) for scope and [DATA_MODEL.md](DATA_MODEL.md)
for entities. This document covers component architecture, the external
systems we're drawing patterns from, and the tech-stack decisions.

## 1. Architectural principles

1. **Deterministic first, human-in-the-loop for anything ambiguous
   involving money or stock.** Already the governing rule in
   `reconciler/matcher.py` (never auto-confirm a match on amount alone when
   two customers could owe the same amount). Every new module — order
   parsing, stock checks — extends the same rule: uncertain → flagged for
   a human, never guessed.
2. **Every automated decision is traceable to a named rule.** Mirrors
   `match_rule` in the existing schema. An order that got auto-confirmed,
   or one that got flagged, should always be answerable with "because of
   rule X," never "the model felt like it."
3. **Prove the manual/upload version before building the live-integration
   version.** This is exactly the pattern that already happened once in
   this codebase: CLI (manual, upload-based) shipped and got a test suite
   *before* the web UI wrapped it, and the web UI still shares the same
   `reconciler.*` core rather than reimplementing anything. The same
   discipline applies going forward: structured WhatsApp ordering before
   free-text NLP, statement-upload reconciliation before live MoMo
   webhooks, single-warehouse stock before multi-warehouse allocation.
4. **Model data to be standards-aware before compliance is mandatory.**
   Cheaper to shape the `Invoice` entity correctly once than to migrate it
   under regulatory pressure later (see the PEPPOL/EN16931 reference
   below).

## 2. Reference systems

These are patterns to borrow, not products to clone — named specifically
so decisions can be checked against a real precedent instead of
first-principles guessing.

### Wasoko (formerly Sokowatch) — Kenya/Tanzania/Uganda/Rwanda, B2B FMCG distribution to informal retailers
**Pattern borrowed:** order aggregation from many small, repeat B2B
customers into one system, a warehouse-fulfillment-then-delivery pipeline,
and (later, once order history accumulates) embedded credit scored off
that history. This directly informs the shape of Phases 7–9 in the
roadmap.
**Where the pattern *doesn't* transfer:** Wasoko operates its own
warehouses and fleet — it *is* the distributor. We're building software
*for* a distributor who already owns the warehouse and the customer
relationship. Our "warehouse" module is fulfillment tracking for someone
else's warehouse, not logistics operations of our own. Worth restating
whenever a Wasoko-inspired feature is being scoped, so we don't
accidentally design for the wrong side of that relationship.

### Twiga Foods — Kenya, B2B fresh-produce supply chain
**Pattern borrowed:** the core validated behavior — small businesses will
place *repeat* B2B orders through a digital channel once it's reliably
faster than the manual alternative, without needing the underlying
relationship (supplier trust, credit terms) to change. Reinforces the
positioning choice in [REQUIREMENTS.md](REQUIREMENTS.md#2-product-statement):
the sales agent stays in the loop, the system removes the eleven
disconnected steps around them, it doesn't replace the relationship.

### Safaricom Daraja API (M-Pesa) — Kenya, mobile money integration
**Pattern borrowed:** Daraja draws a clean line between two integration
shapes:
- **Batch/statement reconciliation** — pull a statement after the fact,
  match it against records. This is what `reconciler/matcher.py` does
  today, and it's deliberately where this project started (per the
  existing README's own "statement upload first, API integration only
  once you know it's worth building").
- **Live callback reconciliation** — the provider pushes a payment
  confirmation to a registered webhook the instant it happens (Daraja's
  STK Push + C2B confirmation callback; MTN/Airtel MoMo's equivalents in
  Zambia follow the same shape).

The concrete implication: when live MoMo integration is eventually built
(Phase 10, deferred — see [ROADMAP.md](ROADMAP.md)), it should be a new
*transport* that calls the same `reconcile()` function on a single
incoming transaction that the CLI/web UI already call on a batch — **not**
a parallel reconciliation implementation. That's why `matcher.reconcile()`
takes plain DataFrames rather than anything upload-shaped: a webhook
handler can construct a one-row DataFrame from a callback payload and
call the exact same function.

### PEPPOL / EN16931 — EU e-invoicing standard
**Pattern borrowed:** not the PEPPOL network itself (that's EU-specific
infrastructure) but its underlying discipline — an invoice is a
structured document with a fixed minimum field set (unique sequential
number, issue date, seller/buyer tax identifiers, line-level tax
breakdown, currency) designed to be machine-verifiable by a third party,
not just human-readable. Zambia's own ZRA Smart Invoice program, and
Kenya's KRA eTIMS, are functionally the same idea. **Practical
implication:** [DATA_MODEL.md](DATA_MODEL.md)'s `Invoice` entity carries
those fields now, unused, so a future fiscalization integration
(Phase 11) is additive, not a schema migration.

## 3. Component architecture

```mermaid
flowchart LR
    subgraph External
        WA[WhatsApp Business Platform]
        MM[Mobile Money Provider<br/>MTN MoMo / Airtel Money]
        ZRA[ZRA Smart Invoice<br/>-- deferred, Phase 11 --]
    end

    WA <--> ORD[Order Service<br/>-- Phase 5, next --]
    ORD --> STK[Stock Service<br/>-- Phase 6 --]
    ORD --> INV[Invoice Service<br/>-- Phase 5 --]
    INV -.->|future| ZRA
    INV --> NOTIFY[Notification Service]
    NOTIFY <--> WA

    MM --> PAYIN[Payment Ingestion]
    PAYIN -->|"today: file upload"| RECON
    PAYIN -.->|"Phase 10, deferred: webhook"| RECON

    subgraph "Existing — unchanged"
        RECON[Reconciliation Engine<br/>reconciler/matcher.py]
        DB[(SQLite<br/>reconciler/db.py)]
        REPORT[Excel Report<br/>reconciler/report.py]
        CLI[cli.py]
        WEBUI[app.py — Flask Web UI]
    end

    INV --> RECON
    RECON --> DB
    RECON --> REPORT
    CLI --> RECON
    WEBUI --> RECON
    DB --> WEBUI

    WH[Warehouse Fulfillment<br/>-- Phase 7, deferred --]
    DEL[Delivery Tracking<br/>-- Phase 8, deferred --]
    ANALYTICS[Owner Analytics<br/>-- Phase 9, deferred --]
    ORD -.-> WH
    WH -.-> DEL
    DB -.-> ANALYTICS
```

The box labeled "Existing — unchanged" is the whole point of this
diagram: everything built so far (the matching waterfall, persistence,
Excel reporting, CLI, web UI, and their 58-test suite) is the
**reconciliation stage** of this pipeline, and nothing above changes it —
new modules produce invoices and consume the reconciliation engine's
output, they don't reimplement matching logic.

## 4. Core flow — sequence diagram

```mermaid
sequenceDiagram
    participant C as Customer
    participant WA as WhatsApp
    participant ORD as Order Service
    participant STK as Stock Service
    participant INV as Invoice Service
    participant MM as Mobile Money
    participant PAY as Payment Ingestion
    participant REC as Reconciliation Engine
    participant OWN as Business Owner (Web UI)

    C->>WA: "Boss, give me 10 boxes of Coke, 5 Fanta, 3 Sprite"
    WA->>ORD: incoming order message
    ORD->>STK: check stock for requested lines
    alt stock sufficient, order unambiguous
        STK-->>ORD: OK
        ORD->>INV: create invoice from confirmed order
        INV->>WA: send invoice total + MoMo payment instructions
        WA->>C: payment instructions
        C->>MM: pays via MoMo
        MM->>PAY: payment recorded (statement upload today, webhook later)
        PAY->>REC: reconcile(invoices, new transaction)
        REC->>REC: apply rules waterfall (matcher.py, unchanged)
        REC-->>OWN: matched / partial / needs_review / unmatched
        REC->>WA: (optional) payment-confirmed notification
        WA->>C: payment confirmed
    else stock insufficient or order ambiguous
        STK-->>ORD: flagged
        ORD->>OWN: needs_review (sales agent resolves manually)
    end
```

## 5. Tech stack decisions

| Decision | Choice | Rationale |
|---|---|---|
| Core language/framework | Python, Flask (unchanged) | Already built, tested, working — no rewrite to justify |
| Database | SQLite (unchanged) for now, Postgres later | `db.py`'s own docstring already states the reasoning: "one business's (or a few pilot businesses') data on a laptop/VPS... no reason to run a database server yet." See [§6](#6-database-strategy-in-detail) for the specific migration triggers and where to host it when that day comes |
| WhatsApp integration | Meta Cloud API direct, or a BSP (Twilio/360dialog/etc.) | Both are viable; see [§7](#7-external-api-strategy-in-detail) for the trade-off and why a short spike is worth doing before committing |
| Async processing | Simple in-process queue initially; Celery/RQ only once webhook volume needs it | Matches principle 3 — don't build infrastructure ahead of the load that justifies it |
| Order/invoice storage | Extends the existing SQLite schema in `db.py` (see [DATA_MODEL.md](DATA_MODEL.md)) | The reconciliation engine already reads/writes this database; new entities join it rather than living in a separate store |
| Mobile money integration (Phase 10, deferred) | Direct MTN/Airtel Zambia APIs, or one aggregator (e.g. Flutterwave) | See [§7](#7-external-api-strategy-in-detail) — genuinely undecided until Phase 10 starts; named now so the shape (webhook-based, Daraja-style) is known ahead of time |

## 6. Database strategy, in detail

**Now (Phases 1–6): SQLite, unchanged.** The `business` column already in
`db.py`'s schema is de facto multi-tenancy — every query is scoped by it.
That's sufficient for a handful of pilot businesses running on one
VPS/laptop, which is the actual load for this whole phase range.

**Trigger to migrate to Postgres — any one of these, not a fixed date:**
1. Concurrent write load from WhatsApp/MoMo webhooks arriving for
   multiple businesses at once — SQLite's single-writer lock becomes a
   real bottleneck, not a theoretical one.
2. Need for managed backups/point-in-time-recovery/HA, once real
   customer money-adjacent data is at stake for paying customers, not
   just pilots.
3. More concurrent pilot businesses than one file comfortably serves
   (roughly 5–10, as a planning number, not a hard limit).

**Where, when that day comes:** the target market is Zambia/Southern
Africa, so region latency matters less for the database than it sounds —
the customer-facing latency is dominated by the WhatsApp<->Meta hop, not
backend<->DB. Two reasonable paths, in order of how little new
operational surface they add:
- **Managed Postgres** (Supabase, Neon, Railway, or a cloud provider's
  RDS-equivalent) — least ops work, but check each provider's nearest
  region to Southern Africa before picking one; most of these are
  US/EU/AP-first, so this is a "good enough, not latency-optimal" choice
  and that's fine at this stage.
- **Self-hosted Postgres in a Southern-Africa-adjacent region** — AWS
  `af-south-1` (Cape Town) or Azure South Africa North (Johannesburg) are
  the closest major regions to Lusaka. More ops work, better latency and
  data-residency story if a customer or regulator ever asks where the
  data lives.

**Migration mechanics:** because the schema is already clean and
business-scoped, this is a lift-and-shift (`pgloader` handles SQLite→
Postgres directly), not a redesign — another reason not to add
complexity to the schema speculatively before it's needed.

## 7. External API strategy, in detail

*(Caveat: exact program names, free-tier limits, and pricing for all of
the below change over time and should be re-verified against each
provider's current developer docs at build time — treat the choices
below as directional, not a locked-in contract.)*

### WhatsApp

Two viable integration paths:

1. **Meta's official WhatsApp Business Cloud API directly.** Self-serve,
   no intermediary. Best long-term cost and control; onboarding
   (Business verification, phone number registration, message template
   approval) has historically been the slow part.
2. **A Business Solution Provider (BSP)** — e.g. Twilio, 360dialog,
   Gupshup, Infobip. Trades a per-message/monthly fee and an extra vendor
   dependency for materially faster onboarding and, with some BSPs,
   pre-built catalog/commerce tooling. Given the founder's own stated
   priority — a real MVP in front of a real customer in weeks, not
   months — a short BSP-vs-direct spike before Phase 5 starts is worth
   the half-day it costs, rather than assuming direct integration is
   automatically the "proper" choice.

**Worth investigating specifically before building a custom order flow:**
WhatsApp's native **Catalog/Cart** feature lets a customer browse a
synced product catalog and build a cart inside WhatsApp itself, handing
the Order Service a structured cart rather than free text. If it fits
this catalog's shape (FMCG SKUs, fixed pricing), it could cut a real
chunk of Phase 5's custom "parse what the customer wants" scope — check
this before writing a bespoke conversation-state machine.

### Mobile money (Zambia) — for Phase 10, still deferred, named now so the shape is known ahead of time

- **MTN Mobile Money Open API (Zambia)** and **Airtel Money Open API
  (Zambia)** — each expose a Collections API following the same
  webhook-confirmation shape as Safaricom Daraja (see
  [§2](#safaricom-daraja-api-m-pesa--kenya-mobile-money-integration)). Direct integration means
  building and maintaining two callback handlers, two auth/signing
  schemes.
- **Alternative: a payment aggregator** that unifies multiple mobile
  money rails behind one API (e.g. Flutterwave, which has supported
  Zambian MTN/Airtel mobile money collections) — one integration instead
  of two, at the cost of a per-transaction aggregator fee and another
  vendor in the trust chain. Worth a genuine cost/time comparison against
  direct telco integration when Phase 10 actually starts, not a default
  either way.

## 8. Security & trust considerations

- **WhatsApp webhook signature verification.** Meta signs webhook payloads
  (`X-Hub-Signature-256`); the order service must verify this before
  trusting any inbound message, not just check the sender number.
- **Mobile money callback authentication.** Daraja-style and MTN/Airtel
  MoMo callbacks are signed or IP-allowlisted by the provider — the
  eventual Phase 10 webhook handler must validate this before writing
  anything to the database, exactly as the current file-upload path
  validates column structure before trusting file content
  (`loaders.py`'s alias-matching + clear-error-on-mismatch behavior is the
  existing analog).
- **Same reconciliation trust invariant, extended upstream.** Nothing
  about order or invoice generation may auto-commit state that a human
  hasn't effectively confirmed (stock check failure blocks, it doesn't
  silently under-fulfill; an ambiguous product mention routes to the
  sales agent, it doesn't guess).
