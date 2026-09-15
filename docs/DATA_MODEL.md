# Data Model — WhatsApp Order-to-Cash for FMCG Distributors

See [ARCHITECTURE.md](ARCHITECTURE.md) for the component picture this
data feeds. **`Invoice` and `Transaction` already exist** as the
`invoices` and `transactions` tables in `reconciler/db.py` — everything
else here is new, and is designed to produce rows in those two tables
rather than duplicate or bypass them.

## Entity-relationship diagram

```mermaid
erDiagram
    BUSINESS ||--o{ CUSTOMER : has
    BUSINESS ||--o{ PRODUCT : sells
    BUSINESS ||--o{ SALES_AGENT : employs
    CUSTOMER ||--o{ ORDER : places
    SALES_AGENT ||--o{ ORDER : confirms
    ORDER ||--|{ ORDER_LINE : contains
    ORDER_LINE }o--|| PRODUCT : references
    PRODUCT ||--|| STOCK_ITEM : "tracked by (Phase 6)"
    ORDER ||--o| INVOICE : generates
    INVOICE ||--|{ INVOICE_LINE : contains
    INVOICE ||--o{ TRANSACTION : "matched by (existing engine)"
    ORDER ||--o| DELIVERY : "fulfilled by (Phase 8, deferred)"

    BUSINESS {
        string business_name PK "matches existing 'business' scoping column in db.py"
    }
    CUSTOMER {
        string customer_id PK
        string name
        string phone "normalized via matcher._normalize_phone"
        string whatsapp_id
    }
    SALES_AGENT {
        string agent_id PK
        string name
        string whatsapp_number
    }
    PRODUCT {
        string product_id PK
        string name
        string unit "e.g. box, crate, each"
        decimal unit_price
    }
    STOCK_ITEM {
        string product_id FK
        int quantity_on_hand
        string warehouse_id "single warehouse for MVP"
    }
    ORDER {
        string order_id PK
        string customer_id FK
        string agent_id FK
        datetime placed_at
        string status "pending | confirmed | flagged | fulfilled"
        string raw_message "original WhatsApp text, for audit"
    }
    ORDER_LINE {
        string order_id FK
        string product_id FK
        int quantity_requested
    }
    INVOICE {
        string invoice_id PK "== existing invoice_id column"
        string customer_name "== existing column"
        string customer_phone "== existing column"
        decimal amount "== existing column"
        date date "== existing column"
        decimal balance "== existing column, matcher-managed"
        string order_id FK "NEW - links back to the order that generated it"
        string tax_id "NEW, unused today - seller/buyer tax identifiers for future fiscalization (PEPPOL/EN16931-style, ZRA Smart Invoice)"
        decimal tax_amount "NEW, unused today - line-level tax breakdown for the same reason"
    }
    INVOICE_LINE {
        string invoice_id FK
        string product_id FK
        int quantity
        decimal line_total
    }
    TRANSACTION {
        string transaction_id PK "== existing column, unchanged"
        string invoice_id FK "== existing matched_invoice column, unchanged"
        string outcome "== existing column: matched|partial|needs_review|unmatched"
        string match_rule "== existing column, unchanged"
    }
    DELIVERY {
        string delivery_id PK
        string order_id FK
        string rider_id
        string status
    }
```

## How this extends, rather than replaces, the existing schema

`reconciler/db.py`'s `SCHEMA` currently defines two tables:

- **`invoices`** (`business, invoice_id, customer_name, customer_phone, amount, date, balance`)
- **`transactions`** (`business, transaction_id, date, amount, sender_name, sender_phone, reference, matched_invoice, customer, outcome, match_rule, details, processed_at`)

Both stay exactly as they are — the matching waterfall, persistence
guarantees (re-upload doesn't undo a partial payment, overlapping
statements don't double-count), and aging logic are all unaffected. What
changes is **where invoices come from**: today a human uploads a CSV/XLSX
built outside this system; from Phase 5 onward, the Invoice Service can
also *write* a row into `invoices` directly, generated from a confirmed
`ORDER`. `load_invoices()` in `loaders.py` stays the path for a business
that still exports invoices from elsewhere (some pilots will, for a
while) — the two invoice sources coexist rather than one replacing the
other.

Two additive columns on `invoices` are anticipated but **not being added
yet**: `order_id` (links an invoice back to the order that generated it —
only meaningful once Phase 5 exists) and the tax/fiscalization fields
described in [ARCHITECTURE.md](ARCHITECTURE.md#peppol--en16931--eu-e-invoicing-standard).
Called out here so the eventual migration is a known, planned one-liner
(`ALTER TABLE invoices ADD COLUMN ...`) rather than a surprise.

## What's deliberately not modeled yet

- **Multi-warehouse stock allocation** — `STOCK_ITEM` above assumes one
  warehouse per business, matching the ICP (1–3 warehouses, and the first
  pilot need only prove the single-warehouse case).
- **Product variants/batches/expiry** — out of scope until a pilot's
  actual catalog complexity demands it.
- **Delivery routing details** (stops, sequencing, vehicle assignment) —
  `DELIVERY` is a placeholder entity for Phase 8, not a designed subsystem.
