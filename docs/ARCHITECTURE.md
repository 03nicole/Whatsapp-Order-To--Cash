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

### Wasoko (formerly Sokowatch) — now operating in Zambia itself, not just a reference market
**Correction (2026-09-15, via web research — see sources at the end of
this section):** Wasoko expanded into Zambia in May 2023, using **Lusaka
as its Southern Africa hub**, $1m+ committed in year one, hub-and-spoke
logistics. This is not a comparable-market reference anymore — it is a
live presence in the exact target city.
**Pattern borrowed:** order aggregation from many small, repeat B2B
customers into one system, a warehouse-fulfillment-then-delivery pipeline,
and (later, once order history accumulates) embedded credit scored off
that history. This directly informs the shape of Phases 7–9 in the
roadmap.
**Where the pattern *doesn't* transfer:** Wasoko operates its own
warehouses and fleet and sells directly to retailers — it *is* the
distributor, disintermediating the ones already there. This project
builds software *for* a distributor who already owns the warehouse and
the customer relationship, so Wasoko isn't a head-to-head competitor for
*this product* - but it is direct competition for whichever independent
Lusaka wholesaler becomes a pilot customer, and that cuts two ways worth
asking about directly (see
[VALIDATION_INTERVIEW_GUIDE.md](VALIDATION_INTERVIEW_GUIDE.md)): a
wholesaler who feels threatened by Wasoko may have a sharper reason to
modernize, or may already be shrinking and a poor pilot candidate. Either
way, Wasoko's presence is itself evidence that app/chat-based ordering
behavior already works in this exact market - the core behavioral
assumption the whole plan depends on.
Sources: [TechCabal](https://techcabal.com/2023/05/12/wasoko-expands-operations-to-southern-africa-with-launch-in-zambia/),
[African Business](https://african.business/2023/05/quick-reads/retail-startup-wasoko-to-invest-over-1m-in-zambia-expansion)

### Sukhiba — pan-African/India, the closest direct product analog
**Pattern borrowed:** unlike Wasoko, Sukhiba doesn't own inventory or
warehouses - it's B2B software *sold to* manufacturers/distributors,
letting them take orders, run a product catalog, and collect payment
through WhatsApp for their own MSME retail customers. This is
architecturally the closest existing match to this project's Phase 5.
Sukhiba reports enabling WhatsApp commerce for 30+ companies serving
~15,000 MSMEs across eight markets in Africa and India.
**No confirmed Zambia presence found** as of this research (2026-09-15) -
worth re-checking periodically rather than treating as settled either way.
Source: [Accion](https://www.accion.org/article/sukhiba-aims-to-redefine-b2b-commerce-in-africa-using-whatsapp/)

### ChatCash — Zimbabwe, the nearest regional player to watch
Harare-based WhatsApp/Messenger commerce + payments platform, 10,000 SMEs
onboarded as of 2026, raising a Series A to scale toward 30,000 clients,
with stated expansion plans to South Africa, Nigeria, and Rwanda - not
Zambia yet, but geographically and economically closer to Zambia than any
Kenya-based comp. Notably localizes to Shona/Ndebele NLP, underscoring
that local-language handling is a real differentiator in this category,
not a nice-to-have.
Source: [TechCabal](https://techcabal.com/2025/09/05/chatcash-turns-chats-into-commerce/)

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
| Mobile money integration (Phase 10) | Flutterwave, resolved over direct MTN/Airtel APIs | See [§7](#7-external-api-strategy-in-detail) — decided when Phase 10 actually started, not a default; `reconciler/flutterwave.py` |

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

**The 24-hour customer service window, built for, 2026-09-16.** Meta
only allows free-form text replies within 24 hours of the customer's
last inbound message; anything sent after that must be a pre-approved
**message template**, submitted to Meta for review in advance, or the
real API rejects it outright. This is easy to miss building against a
test client, since nothing in this codebase's own tests exercises real
wall-clock delay — it was found by researching actual deployment/cost
questions, not by a test failing. `reconciler/whatsapp.py`'s
`is_within_customer_service_window()` judges this against the
underlying order's `placed_at`, the only record this system keeps of
"when did this customer last write in"; `_notify_customers_of_payment()`
in `app.py` is the one place today where enough wall-clock time can
plausibly pass to matter (a MoMo payment settling days after the order).
The template itself (`WHATSAPP_PAYMENT_TEMPLATE_NAME`/`_LANG` env vars)
must be created and approved in Meta Business Manager before this
actually works for real — an operational step this code documents but
can't perform itself, the same category as the Commerce Catalog
`retailer_id` assumption above and Phase 11's ZRA device registration.

### Catalog sync — Phase 5b follow-up, built

Phase 5b's native Catalog/Cart checkout (`reconciler/orders.py`'s
`resolve_native_order()`) depends on an assumption it can't itself
verify: that the distributor's Meta Commerce Catalog has each product's
`retailer_id` set to match this system's own `product_id` exactly. Left
as a manual setup step, that's a real, silent failure mode — a typo or a
stale sync flags every affected order rather than shipping it wrong, but
still means orders silently start failing for no reason visible from
inside this app.

**Researched 2026-09-15, against Meta's real product feed documentation**
(not guessed): Commerce Manager supports populating a catalog by manual
CSV/TSV/XLSX upload, a **scheduled fetch from a hosted URL** (as often as
hourly), the Catalog Batch API (a live push, needs a Meta access token +
catalog ID), or a partner-platform integration (Shopify, WooCommerce —
not applicable here). The scheduled-feed-URL path was chosen for the same
reason Flutterwave and the WhatsApp webhook itself were buildable without
live credentials: this app just serves a URL Meta polls, no Meta API
token or catalog ID needed to build or test it.

Required feed fields, sourced from Meta's product feed spec: `id, title,
description, availability, condition, price` (numeric + ISO 4217
currency code, e.g. `"150.00 ZMW"`), `link, image_link, brand`. Mapping
that against the `products` table exposed two fields this system's data
model never had at all — `description` and a product image — closed as
their own small data-model change (`products.description`,
`products.image_url`, both nullable) rather than faking placeholder
values in the feed itself.

Built as `reconciler/catalog_feed.py` (`build_meta_feed_csv()`) +
`GET /catalog/feed.csv?business=<name>&token=<token>` in `app.py`. `id`
is always this system's own `product_id` — the actual fix for the
retailer_id assumption above, since a distributor registering this feed
never has to hand-type a matching retailer_id at all. A product with no
description on file falls back to its name (a real value); a product
with no photo yet leaves `image_link` blank rather than a placeholder
image, so it shows up as genuinely incomplete in Meta's own catalog
diagnostics — the same "never guess, never fake it" principle the
reconciliation waterfall has followed since Phase 1.

Token-gated with an HMAC of the business name (`CATALOG_FEED_SECRET` env
var; falls back to the process's own random secret key when unset, fine
for local testing, not stable across restarts — set it explicitly once
this is registered with Meta for real) rather than left open, since once
registered this URL is polled from the public internet on a fixed
schedule. **Like the WhatsApp/MoMo webhooks before it, this route is
inert until the app is actually deployed somewhere Meta's fetcher can
reach** — see §6's hosting notes; nothing about building this ahead of
that decision is new to this feature specifically.

### Mobile money (Zambia) — Phase 10, built, decision resolved

**Resolved 2026-09-15 in favor of Flutterwave (the aggregator path)**,
when Phase 10 actually started — this was genuinely open until then,
not a default. Two things tipped it, checked directly against each
provider's public docs rather than assumed:

- **MTN Mobile Money Open API (Zambia)** and **Airtel Money Open API
  (Zambia)** each expose a Collections API following the same
  webhook-confirmation shape as Safaricom Daraja (see
  [§2](#safaricom-daraja-api-m-pesa--kenya-mobile-money-integration)) -
  but the public documentation found for MTN's webhook callback payload
  was incomplete (general request-flow patterns, no full schema), and
  direct integration means building and maintaining two separate
  callback handlers and auth schemes, one per telco.
- **Flutterwave** has an explicit, dedicated ["Zambia Mobile
  Money"](https://developer.flutterwave.com/v3.0/docs/zambia-mobile-money)
  page and a fully documented, stable webhook payload
  (`event: "charge.completed"`, Zambia mobile money charges carry
  `payment_type: "mobilemoneyzm"`) covering both MTN and Airtel
  collections through one integration - at the cost of a per-transaction
  aggregator fee and another vendor in the trust chain, which is worth
  re-examining once real transaction-volume/fee data from an actual
  pilot exists to compare against direct integration properly.

Built in `reconciler/flutterwave.py` - webhook payload parsing
(filtering to completed, Zambia-mobile-money charges only; other
payment types/events on the same merchant account are ignored, not
errored) and `verif-hash` signature verification (Flutterwave's stated
authentication mechanism - a shared-secret header compare, not HMAC).
`/momo/webhook?business=<name>` in `app.py` feeds a single incoming
payment straight into the same `reconciler.matcher.reconcile()`
function the CLI and web UI already call on a batch - exactly the
architecture this section originally called for, not a parallel
matching path. No live Flutterwave account exists - built and tested
against Flutterwave's real public documentation, the same "prove it
without live credentials first" pattern already used for WhatsApp.

### ZRA Smart Invoice fiscalization — Phase 11, built, deliberately scoped

Zambia's e-invoicing system, **Smart Invoice**, has been mandatory for
VAT-registered taxpayers since July 2024 (launched March 2024,
Continuous Transaction Controls architecture, replacing the earlier
Electronic Fiscal Device regime). Taxpayers connect their own invoicing
software to it through a **Virtual Sales Data Controller (VSDC)** - a
REST/JSON API, either run locally (ZRA distributes it as a downloadable
WAR/JAR file via the Smart Invoice Taxpayer Portal) or provided by a
certified third-party vendor's hosted instance. Either way, the exact
host is taxpayer/vendor-specific - there is no single fixed ZRA cloud
endpoint this system could hardcode.

**Sourcing note:** ZRA's own VSDC API Specification PDF (v1.0.7,
`zra.org.zm/wp-content/uploads/2024/08/VSDC-API-Specification-Document-v1.0.7-1.pdf`)
was not fetchable directly (TLS certificate error on zra.org.zm at
research time). The endpoint paths, payload field names, and auth flow
`reconciler/zra.py` is built against instead come from a real,
actively-maintained open-source VSDC integration
([github.com/CrystalisedApps/ca-erpnext-zra](https://github.com/CrystalisedApps/ca-erpnext-zra),
an ERPNext plugin built against that same ZRA spec) - cloned and read
directly (not just its README) to confirm the real route table
(`fixtures/crystal_smart_invoice_routes.json`) and payload construction
(`utils/payload_utils.py`'s `build_invoice_payload()`), cross-checked
against independent search results confirming the same architecture. No
live ZRA account or sandbox exists to test against - the same "prove it
against a real documented shape, no live credentials" position already
taken with Flutterwave and the WhatsApp webhook.

**Confirmed real, from that source:**
- Auth: `POST {server_url}/api/v1/Users/GetToken`, form-encoded
  `{username, password}` → `{"Result": {"token": "..."}, "expires_in": ...}`,
  used as a Bearer token on every subsequent request.
- Device init (one-time per device): `POST /api/v1/InitializationInfo/selectInitInfo`
  with `{tpin, bhfId, dvcSrlNo}`. Result code `"000"` = success, `"902"` =
  already initialized (both fine); anything else is a real failure.
- Sale submission: `POST /api/v1/SalesInformation/saveSales` with a
  payload keyed by `tpin`, `bhfId`, `cisInvcNo` (invoice number),
  `salesDt`, `custTpin`/`custNm`, `totAmt`/`totTaxAmt`/`totTaxblAmt`, and
  an `itemList[]` of `{itemSeq, itemCd, itemNm, itemClsCd, qty,
  qtyUnitCd, prc, splyAmt, vatAmt, totAmt, vatTaxblAmt, vatCatCd}`.
- Related endpoints the same route table confirms exist but this phase
  doesn't call yet: `ItemsClassInformation/selectItemsClass` and
  `CodeData/selectCodes` (ZRA's own reference tables for item
  classification codes and standard codes like units-of-measure -
  fetching and caching these properly is real future work, not
  something to hand-populate from a guess).

**Why this was scoped to a transport layer only, not a full
integration**, decided via a mid-build check-in once the real shape of
the work became clear (see [ROADMAP.md](ROADMAP.md)'s Phase 11 entry):
this system's data model had zero VAT/tax modeling of any kind before
this phase - no per-product tax category, no ZRA item classification
code, no TPIN storage anywhere, and critically, **no verified source for
what most of ZRA's own VAT category codes (A, B, C1-C3, D, E, F, IPL1-2,
TL, RVAT) actually mean or what rate each one carries** beyond the two
best-established facts: Zambia's standard VAT rate is 16% (category
`"A"`, consistently corroborated across independent sources including
PwC's VAT-in-Africa country guide), and a zero-rated/exempt supply is by
definition taxed at 0% regardless of the exact subcode. Guessing at the
rest - inventing a plausible-looking `itemClsCd` or assuming an
unfamiliar VAT category's rate - would be fabricating data with real
legal weight if it were ever pointed at a live ZRA sandbox, a
categorically worse failure mode than this project's usual "flag it for
a human" response to ambiguity. So `reconciler/zra.py`'s
`build_sales_payload()` refuses outright (`ZRAConfigError`) rather than
guesses:
- any invoice containing a product missing `vat_category_code` or
  `item_class_code` (two new nullable `products` columns, `db.py`'s
  `update_product_tax_fields()` - deliberately kept out of the bulk
  catalog-CSV-import path, so a routine re-import can never silently
  wipe a value someone set deliberately);
- any `vat_category_code` outside `{"A", "C1", "C2", "C3", "D"}` - the
  only ones this module computes a rate for.

**What's genuinely still missing, not attempted here:** no live
submission route or UI exists in `app.py` yet - there's nothing to point
it at. Wiring fiscalization into the order-confirmation or
reconciliation flow means deciding *when* it should fire (at order time?
at payment time?) and what happens on a ZRA rejection, which needs a
real pilot customer's actual compliance workflow to design against, not
assumptions - see [ROADMAP.md](ROADMAP.md)'s Phase 11 follow-up entry.

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
