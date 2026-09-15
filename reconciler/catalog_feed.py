"""
Meta Commerce Catalog feed export.

The other half of Phase 5b's retailer_id/product_id assumption
(reconciler/orders.py's resolve_native_order): rather than trusting a
distributor to hand-configure their Meta Commerce Catalog so its
retailer_id matches this system's product_id exactly - an operational
step this code has never been able to verify - this feed makes that sync
automatic by construction, since `id` here always IS product_id.

Meta supports pulling a catalog from a scheduled fetch against a hosted
URL (as often as hourly) instead of a manual re-upload - see
docs/ARCHITECTURE.md's catalog-sync research notes for the sourced
field spec this was built against (id, title, description, availability,
condition, price, link, image_link, brand - required fields per Meta's
product feed documentation, checked 2026-09-15, not guessed).

Deliberately does NOT fabricate a value for a field this system has no
real data for: link and image_link come through blank when there's
nothing real to put there, rather than a placeholder URL. A row that's
missing a field Meta calls required shows up as incomplete in Meta's own
catalog diagnostics - visible and actionable for the distributor to fix
by adding a real photo - rather than silently "passing" on fake data.
Same "never guess" principle the reconciliation waterfall has followed
since Phase 1.
"""

from __future__ import annotations

import csv
import io

import pandas as pd

FEED_COLUMNS = ["id", "title", "description", "availability", "condition",
                "price", "link", "image_link", "brand"]


def _or_fallback(value, fallback):
    return fallback if pd.isna(value) or value == "" else value


def build_meta_feed_csv(catalog_df: pd.DataFrame, currency: str = "ZMW",
                         product_link_base: str | None = None) -> str:
    """Renders `catalog_df` (db.get_catalog()'s shape) as a Meta-compliant
    product feed CSV.

    A product with no description on file falls back to its name - a
    real, honest value, not an invented one (the same way a plain
    product title is a legitimate minimal description in most real
    catalogs). `product_link_base`, if given, becomes
    "<product_link_base>&product=<product_id>" for the `link` column;
    None leaves it blank rather than fabricating a URL that doesn't lead
    anywhere real - there's no per-product page in this system, and
    `link` only becomes meaningful once this app is deployed somewhere
    publicly reachable in the first place."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FEED_COLUMNS)
    writer.writeheader()
    for row in catalog_df.itertuples():
        description = _or_fallback(getattr(row, "description", None), row.name)
        image_link = _or_fallback(getattr(row, "image_url", None), "")
        link = f"{product_link_base}&product={row.product_id}" if product_link_base else ""
        writer.writerow({
            "id": row.product_id,
            "title": row.name,
            "description": description,
            "availability": "in stock" if row.quantity_on_hand > 0 else "out of stock",
            "condition": "new",
            "price": f"{row.unit_price:.2f} {currency}",
            "link": link,
            "image_link": image_link,
            "brand": "",
        })
    return buf.getvalue()
