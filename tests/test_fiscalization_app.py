"""
Web-app-level tests for Phase 11's follow-up: wiring
reconciler/zra.py's transport layer into the order-confirmation flow
(_fiscalize_order() in app.py) and the /fiscalization settings/review
page. No live ZRA sandbox exists, so these monkeypatch
zra.ZRAClient.submit_sale directly - the same "fake the external client,
test the wiring" pattern test_orders_app.py's RecordingClient already
uses for WhatsApp.
"""

import io
import json

import app as app_module
from conftest import webhook_payload

CATALOG_CSV = (
    "SKU,Product Name,UOM,Price,Stock\n"
    "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
)


def _import_catalog(client, business="WABiz"):
    return client.post(
        "/catalog/import",
        data={"business": business, "catalog": (io.BytesIO(CATALOG_CSV.encode()), "catalog.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )


def _set_tax_fields(client, business="WABiz", product_id="COKE-24"):
    client.post("/catalog/edit", data={
        "business": business, "product_id": product_id,
        "description": "", "image_url": "",
    })
    conn = app_module.db.connect(app_module.DB_PATH)
    app_module.db.update_product_tax_fields(conn, business, product_id, "A", "10101010")
    conn.close()


def _configure_zra(client, business="WABiz"):
    return client.post("/fiscalization/settings", data={
        "business": business, "server_url": "https://vsdc.example.com",
        "username": "u", "password": "p", "tpin": "1000000000",
        "bhf_id": "000", "device_serial": "SN1",
    }, follow_redirects=True)


def _fake_zra_success(monkeypatch):
    monkeypatch.setattr(app_module.zra.ZRAClient, "authenticate", lambda self: "fake-token")
    monkeypatch.setattr(app_module.zra.ZRAClient, "submit_sale",
                         lambda self, payload: {"Result": {"resultCd": "000"}})


def _fake_zra_failure(monkeypatch, message="ZRA unreachable"):
    def _raise(self, payload):
        raise Exception(message)
    monkeypatch.setattr(app_module.zra.ZRAClient, "authenticate", lambda self: "fake-token")
    monkeypatch.setattr(app_module.zra.ZRAClient, "submit_sale", _raise)


def _place_order(client, text="10 COKE-24"):
    return client.post("/whatsapp/webhook", query_string={"business": "WABiz"},
                        data=json.dumps(webhook_payload(text)), content_type="application/json")


def _order_row(business="WABiz"):
    conn = app_module.db.connect(app_module.DB_PATH)
    row = conn.execute(
        "SELECT status, fiscalization_status, fiscalization_error FROM orders "
        "WHERE business = ? ORDER BY rowid DESC LIMIT 1", (business,),
    ).fetchone()
    conn.close()
    return row


# --- order confirmation is never blocked by fiscalization -------------------

def test_order_confirms_normally_with_no_zra_settings_configured(client):
    _import_catalog(client)
    r = _place_order(client)
    assert r.status_code == 200

    status, fiscalization_status, _ = _order_row()
    assert status == "confirmed"  # the order itself is unaffected
    assert fiscalization_status is None  # never even attempted - opt-in per business


def test_order_confirms_normally_when_a_product_is_missing_tax_fields(client, monkeypatch):
    _fake_zra_success(monkeypatch)
    _import_catalog(client)
    _configure_zra(client)
    # deliberately not calling _set_tax_fields - COKE-24 has no
    # vat_category_code/item_class_code yet

    r = _place_order(client)
    assert r.status_code == 200

    status, fiscalization_status, error = _order_row()
    assert status == "confirmed"  # still not blocked
    assert fiscalization_status == "failed"
    assert "COKE-24" in error


def test_order_confirms_normally_when_zra_submission_raises(client, monkeypatch):
    _fake_zra_failure(monkeypatch, message="Connection timed out")
    _import_catalog(client)
    _configure_zra(client)
    _set_tax_fields(client)

    r = _place_order(client)
    assert r.status_code == 200

    status, fiscalization_status, error = _order_row()
    assert status == "confirmed"
    assert fiscalization_status == "failed"
    assert "Connection timed out" in error


def test_order_is_marked_fiscalized_on_a_clean_success(client, monkeypatch):
    _fake_zra_success(monkeypatch)
    _import_catalog(client)
    _configure_zra(client)
    _set_tax_fields(client)

    r = _place_order(client)
    assert r.status_code == 200

    status, fiscalization_status, error = _order_row()
    assert status == "confirmed"
    assert fiscalization_status == "fiscalized"
    assert error is None


def test_flagged_order_is_never_fiscalized(client, monkeypatch):
    """A flagged order never became an invoice - nothing to submit."""
    _fake_zra_success(monkeypatch)
    _import_catalog(client)
    _configure_zra(client)

    r = _place_order(client, text="10 NOT-A-REAL-PRODUCT")
    assert r.status_code == 200

    status, fiscalization_status, _ = _order_row()
    assert status == "flagged"
    assert fiscalization_status is None


# --- /fiscalization settings page --------------------------------------------

def test_fiscalization_page_shows_not_configured_by_default(client):
    r = client.get("/fiscalization", query_string={"business": "WABiz"})
    assert "Not configured yet" in r.get_data(as_text=True)


def test_save_zra_settings_persists_and_shows_as_configured(client):
    _configure_zra(client)
    r = client.get("/fiscalization", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "Configured" in body
    assert "1000000000" in body  # tpin


def test_save_zra_settings_rejects_an_incomplete_form(client):
    r = client.post("/fiscalization/settings", data={
        "business": "WABiz", "server_url": "https://vsdc.example.com",
        "username": "u", "password": "p", "tpin": "", "bhf_id": "000", "device_serial": "SN1",
    }, follow_redirects=True)
    assert "required" in r.get_data(as_text=True).lower()

    conn = app_module.db.connect(app_module.DB_PATH)
    assert app_module.db.get_zra_settings(conn, "WABiz") is None
    conn.close()


# --- /fiscalization retry -----------------------------------------------------

def test_fiscalization_page_lists_an_order_needing_retry(client, monkeypatch):
    _fake_zra_failure(monkeypatch)
    _import_catalog(client)
    _configure_zra(client)
    _set_tax_fields(client)
    _place_order(client)

    r = client.get("/fiscalization", query_string={"business": "WABiz"})
    body = r.get_data(as_text=True)
    assert "ZRA unreachable" in body


def test_retry_fiscalization_succeeds_once_the_underlying_problem_is_fixed(client, monkeypatch):
    _fake_zra_failure(monkeypatch, message="ZRA unreachable")
    _import_catalog(client)
    _configure_zra(client)
    _set_tax_fields(client)
    _place_order(client)
    status, fiscalization_status, _ = _order_row()
    assert fiscalization_status == "failed"

    _fake_zra_success(monkeypatch)  # "fixed" - ZRA is reachable now
    conn = app_module.db.connect(app_module.DB_PATH)
    order_id = conn.execute("SELECT order_id FROM orders WHERE business='WABiz'").fetchone()[0]
    conn.close()

    r = client.post("/fiscalization/retry", data={"business": "WABiz", "order_id": order_id},
                     follow_redirects=True)
    assert r.status_code == 200

    status, fiscalization_status, error = _order_row()
    assert fiscalization_status == "fiscalized"
    assert error is None


def test_retry_fiscalization_rejects_an_order_not_awaiting_retry(client):
    r = client.post("/fiscalization/retry", data={"business": "WABiz", "order_id": "not-a-real-order"},
                     follow_redirects=True)
    assert "awaiting a fiscalization retry" in r.get_data(as_text=True)
