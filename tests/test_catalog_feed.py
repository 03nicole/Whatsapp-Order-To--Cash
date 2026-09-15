"""
Tests for reconciler/catalog_feed.py - the Meta Commerce Catalog feed
export. Built against Meta's real documented feed field spec (see the
module's own docstring for the source), not a live account - same "prove
it against the real shape, no live credentials" pattern as
reconciler/flutterwave.py.
"""

import csv
import io

import pandas as pd

from reconciler.catalog_feed import build_meta_feed_csv


def _catalog(**overrides):
    row = {
        "product_id": "COKE-24", "name": "Coca-Cola 300ml (24-pack)", "unit": "box",
        "unit_price": 120.0, "quantity_on_hand": 40,
        "description": None, "image_url": None,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def _rows(csv_text):
    return list(csv.DictReader(io.StringIO(csv_text)))


def test_feed_has_meta_required_columns():
    rows = _rows(build_meta_feed_csv(_catalog()))
    assert set(rows[0].keys()) == {
        "id", "title", "description", "availability", "condition",
        "price", "link", "image_link", "brand",
    }


def test_id_is_always_the_product_id():
    """The whole point: id must equal product_id so a WhatsApp native-cart
    order's retailer_id always resolves, without a distributor having to
    configure the match by hand (see resolve_native_order())."""
    rows = _rows(build_meta_feed_csv(_catalog(product_id="COKE-24")))
    assert rows[0]["id"] == "COKE-24"


def test_price_includes_iso_currency_code():
    rows = _rows(build_meta_feed_csv(_catalog(unit_price=120.0), currency="ZMW"))
    assert rows[0]["price"] == "120.00 ZMW"


def test_availability_reflects_stock():
    in_stock = _rows(build_meta_feed_csv(_catalog(quantity_on_hand=5)))
    out_of_stock = _rows(build_meta_feed_csv(_catalog(quantity_on_hand=0)))
    assert in_stock[0]["availability"] == "in stock"
    assert out_of_stock[0]["availability"] == "out of stock"


def test_description_falls_back_to_name_when_not_set():
    rows = _rows(build_meta_feed_csv(_catalog(name="Coca-Cola 300ml (24-pack)", description=None)))
    assert rows[0]["description"] == "Coca-Cola 300ml (24-pack)"


def test_description_uses_the_real_value_when_set():
    rows = _rows(build_meta_feed_csv(_catalog(description="Refreshing cola, 300ml x 24")))
    assert rows[0]["description"] == "Refreshing cola, 300ml x 24"


def test_image_link_is_blank_not_fabricated_when_no_photo_on_file():
    """Never invents a placeholder image URL - a row missing a real photo
    should read as genuinely incomplete, not silently 'pass'."""
    rows = _rows(build_meta_feed_csv(_catalog(image_url=None)))
    assert rows[0]["image_link"] == ""


def test_image_link_uses_the_real_url_when_set():
    rows = _rows(build_meta_feed_csv(_catalog(image_url="https://example.com/coke.jpg")))
    assert rows[0]["image_link"] == "https://example.com/coke.jpg"


def test_link_is_blank_without_a_product_link_base():
    rows = _rows(build_meta_feed_csv(_catalog(), product_link_base=None))
    assert rows[0]["link"] == ""


def test_link_uses_product_link_base_when_given():
    rows = _rows(build_meta_feed_csv(
        _catalog(product_id="COKE-24"),
        product_link_base="https://example.com/catalog?business=Sample",
    ))
    assert rows[0]["link"] == "https://example.com/catalog?business=Sample&product=COKE-24"


def test_handles_a_catalog_df_without_description_or_image_url_columns():
    """The same shape a pre-image-support catalog_df (or a hand-built
    test fixture) has - build_meta_feed_csv must not blow up on a
    missing attribute."""
    bare = pd.DataFrame([{
        "product_id": "COKE-24", "name": "Coca-Cola", "unit": "box",
        "unit_price": 120.0, "quantity_on_hand": 40,
    }])
    rows = _rows(build_meta_feed_csv(bare))
    assert rows[0]["description"] == "Coca-Cola"
    assert rows[0]["image_link"] == ""


def test_one_row_per_product():
    catalog = pd.concat([_catalog(product_id="COKE-24"), _catalog(product_id="FANTA-24")],
                         ignore_index=True)
    rows = _rows(build_meta_feed_csv(catalog))
    assert [r["id"] for r in rows] == ["COKE-24", "FANTA-24"]
