"""
Tests for reconciler/zra.py - the ZRA Smart Invoice (VSDC) transport
layer, Phase 11. No live ZRA sandbox exists (see the module's own
docstring for the sourcing), so ZRAClient tests inject a fake
requests.Session-like object rather than making a real HTTP call - same
reasoning as reconciler/whatsapp.py's LoggingWhatsAppClient letting the
whole order flow be tested with zero external credentials.
"""

import pytest

from reconciler.zra import (
    STANDARD_VAT_RATE_PERCENT,
    ZRAClient,
    ZRACredentials,
    ZRAConfigError,
    build_sales_payload,
)


def _credentials(**overrides):
    fields = {
        "server_url": "https://vsdc.example.com", "username": "biz1", "password": "secret",
        "tpin": "1000000000", "bhf_id": "000", "device_serial": "1000000000_VSDC",
    }
    fields.update(overrides)
    return ZRACredentials(**fields)


def _line(**overrides):
    line = {
        "product_id": "COKE-24", "name": "Coca-Cola 300ml (24-pack)",
        "quantity": 2, "unit_price": 120.0,
        "vat_category_code": "A", "item_class_code": "10101010",
    }
    line.update(overrides)
    return line


# --- build_sales_payload: never guess ---------------------------------------

def test_refuses_a_line_missing_vat_category_code():
    with pytest.raises(ZRAConfigError) as exc:
        build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None,
                             [_line(vat_category_code=None)])
    assert "COKE-24" in str(exc.value)


def test_refuses_a_line_missing_item_class_code():
    with pytest.raises(ZRAConfigError) as exc:
        build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None,
                             [_line(item_class_code=None)])
    assert "COKE-24" in str(exc.value)


def test_refuses_an_invoice_with_no_line_items():
    with pytest.raises(ZRAConfigError):
        build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None, [])


def test_refuses_an_unsupported_vat_category_rather_than_guessing_a_rate():
    """B, E, F, IPL1/2, TL, RVAT are real ZRA category codes this module
    has no verified rate for - must be refused, not defaulted to 0% or
    16%, since a wrong tax submission has real legal weight."""
    with pytest.raises(ZRAConfigError) as exc:
        build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None,
                             [_line(vat_category_code="B")])
    assert "B" in str(exc.value)


# --- build_sales_payload: correct math for the categories it does support ---

def test_standard_rate_category_computes_16_percent_vat():
    payload = build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None,
                                   [_line(quantity=2, unit_price=120.0, vat_category_code="A")])
    item = payload["itemList"][0]
    assert item["vatTaxblAmt"] == 240.0
    assert item["vatAmt"] == pytest.approx(240.0 * STANDARD_VAT_RATE_PERCENT / 100)
    assert item["totAmt"] == pytest.approx(240.0 + item["vatAmt"])


@pytest.mark.parametrize("category", ["C1", "C2", "C3", "D"])
def test_zero_rated_and_exempt_categories_compute_zero_vat(category):
    payload = build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None,
                                   [_line(quantity=1, unit_price=100.0, vat_category_code=category)])
    item = payload["itemList"][0]
    assert item["vatAmt"] == 0.0
    assert item["totAmt"] == 100.0


def test_totals_sum_across_multiple_lines():
    payload = build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None, [
        _line(product_id="COKE-24", quantity=2, unit_price=120.0, vat_category_code="A"),
        _line(product_id="MEALIE-25", quantity=1, unit_price=180.0, vat_category_code="D"),
    ])
    assert payload["totItemCnt"] == 2
    assert payload["totTaxblAmt"] == 240.0 + 180.0
    assert payload["totTaxAmt"] == pytest.approx(240.0 * STANDARD_VAT_RATE_PERCENT / 100)
    assert payload["totAmt"] == payload["totTaxblAmt"] + payload["totTaxAmt"]


def test_payload_carries_business_and_invoice_identity():
    creds = _credentials(tpin="1234567890", bhf_id="001")
    payload = build_sales_payload(creds, "ORD-abc123", "20260915", "Joseph Mwansa", "9999999999",
                                   [_line()])
    assert payload["tpin"] == "1234567890"
    assert payload["bhfId"] == "001"
    assert payload["cisInvcNo"] == "ORD-abc123"
    assert payload["salesDt"] == "20260915"
    assert payload["custNm"] == "Joseph Mwansa"
    assert payload["custTpin"] == "9999999999"


def test_customer_tpin_defaults_to_empty_string_not_none():
    """A retail customer plausibly has no TPIN at all - real and
    expected, not a config error to refuse over."""
    payload = build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None, [_line()])
    assert payload["custTpin"] == ""


def test_item_carries_its_own_classification_and_vat_category():
    payload = build_sales_payload(_credentials(), "INV-1", "20260915", "Grace Banda", None,
                                   [_line(item_class_code="20304050", vat_category_code="A")])
    item = payload["itemList"][0]
    assert item["itemClsCd"] == "20304050"
    assert item["vatCatCd"] == "A"
    assert item["itemCd"] == "COKE-24"
    assert item["itemSeq"] == 1


# --- ZRAClient: fake session, no real HTTP call ------------------------------

class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_authenticate_posts_credentials_and_extracts_token():
    session = _FakeSession([_FakeResponse({"Result": {"token": "abc123"}, "expires_in": 3600})])
    client = ZRAClient(_credentials(username="biz1", password="secret"), session=session)

    token = client.authenticate()

    assert token == "abc123"
    url, kwargs = session.calls[0]
    assert url == "https://vsdc.example.com/api/v1/Users/GetToken"
    assert kwargs["data"] == {"username": "biz1", "password": "secret"}


def test_authenticate_raises_when_no_token_in_response():
    session = _FakeSession([_FakeResponse({"Result": {}})])
    client = ZRAClient(_credentials(), session=session)
    with pytest.raises(ZRAConfigError):
        client.authenticate()


def test_initialize_device_posts_tpin_bhfid_and_device_serial():
    session = _FakeSession([
        _FakeResponse({"Result": {"token": "abc123"}}),  # authenticate() (auto-called)
        _FakeResponse({"Result": {"resultCd": "000"}}),  # initialize_device()
    ])
    client = ZRAClient(_credentials(tpin="1000000000", bhf_id="000", device_serial="SN1"), session=session)

    result = client.initialize_device()

    assert result["Result"]["resultCd"] == "000"
    url, kwargs = session.calls[1]
    assert url == "https://vsdc.example.com/api/v1/InitializationInfo/selectInitInfo"
    assert kwargs["json"] == {"tpin": "1000000000", "bhfId": "000", "dvcSrlNo": "SN1"}
    assert kwargs["headers"]["Authorization"] == "Bearer abc123"


def test_submit_sale_authenticates_first_if_no_token_yet():
    session = _FakeSession([
        _FakeResponse({"Result": {"token": "abc123"}}),
        _FakeResponse({"Result": {"resultCd": "000"}}),
    ])
    client = ZRAClient(_credentials(), session=session)

    result = client.submit_sale({"cisInvcNo": "INV-1"})

    assert result["Result"]["resultCd"] == "000"
    assert len(session.calls) == 2  # authenticate, then the actual submit
    assert session.calls[1][0] == "https://vsdc.example.com/api/v1/SalesInformation/saveSales"


def test_submit_sale_reuses_an_existing_token_without_reauthenticating():
    session = _FakeSession([
        _FakeResponse({"Result": {"token": "abc123"}}),
        _FakeResponse({"Result": {"resultCd": "000"}}),
    ])
    client = ZRAClient(_credentials(), session=session)
    client.authenticate()

    client.submit_sale({"cisInvcNo": "INV-1"})

    assert len(session.calls) == 2  # the explicit authenticate() + one submit, no second auth call
