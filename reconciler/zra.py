"""
ZRA Smart Invoice (VSDC) transport layer - Phase 11, deliberately scoped.

Zambia's e-invoicing system (Smart Invoice, mandatory for VAT-registered
taxpayers since July 2024) works through a Virtual Sales Data Controller
(VSDC): a REST API, JSON over HTTPS, that a taxpayer's own invoicing
software talks to. ZRA's own spec PDF
(https://www.zra.org.zm/wp-content/uploads/2024/08/VSDC-API-Specification-Document-v1.0.7-1.pdf)
was not fetchable directly (TLS certificate error on zra.org.zm) - the
endpoint paths, payload field names, and auth flow below are instead
sourced from a real, actively-maintained open-source VSDC integration
(github.com/CrystalisedApps/ca-erpnext-zra, an ERPNext plugin built
against that same spec) and cross-checked against independent search
results confirming the same architecture (REST/JSON, a device-
initialization step, JWT auth). No live ZRA account or sandbox exists to
test this against - same "prove it against a real documented shape, no
live credentials" position this project already took with Flutterwave.

Deliberately scoped to the transport layer + payload shape only. What is
NOT attempted here, and why:

  - VAT-rate computation for most category codes. ZRA's VAT category
    codes (A, B, C1-C3, D, E, F, IPL1-2, TL, RVAT) are real and appear
    throughout the reference implementation, but their exact meanings
    and rates are set by tax legislation this module has no verified
    source for beyond the two best-established facts: Zambia's standard
    VAT rate is 16% (category "A"), and a zero-rated/exempt supply is by
    definition taxed at 0% regardless of which zero/exempt subcode
    applies. Every other category is refused outright (see
    UNSUPPORTED_VAT_CATEGORIES) rather than guessed - a wrong VAT
    computation submitted to a real tax authority is a materially worse
    failure mode than this project's usual "flag for a human," so it
    isn't treated as an equivalent risk.
  - Item classification codes (itemClsCd) and per-product VAT category
    are required, real inputs (reconciler/db.py's
    update_product_tax_fields()) - never defaulted. build_sales_payload()
    refuses to build a payload for any invoice containing a product
    missing either one.
  - No live submission route in app.py yet, and no UI to trigger one -
    there is nothing to point it at. Wiring it into the order-
    confirmation flow is a follow-up decision, not assumed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import requests

# Sourced from Zambia's standard VAT rate (widely and consistently cited,
# e.g. PwC's "VAT in Africa" country guide) - the one rate this module is
# confident enough in to compute rather than require as an input.
STANDARD_VAT_RATE_PERCENT = 16.0

# Zero-rated and exempt supplies are both taxed at 0% by definition,
# regardless of which ZRA subcode applies - safe to compute even without
# knowing the exact legal distinction between e.g. C1/C2/C3.
ZERO_RATED_VAT_CATEGORIES = {"C1", "C2", "C3", "D"}
STANDARD_VAT_CATEGORY = "A"

# Categories referenced in the real VSDC payload shape (see
# reference implementation) whose exact rate/rules this module has no
# verified source for - refused rather than guessed. B is genuinely
# ambiguous even about which "rated" bucket it is; E/F/IPL1/IPL2/TL/RVAT
# are excise/tourism/insurance/turnover levy categories governed by
# separate legislation this module has not researched.
UNSUPPORTED_VAT_CATEGORIES_MESSAGE = (
    "vat_category_code '{code}' is not one of the categories this system "
    "has a verified rate for ({supported}). Confirm the real rate with "
    "ZRA/an accountant and extend zra.py's VAT_RATE_BY_CATEGORY rather "
    "than guessing - never submit a fiscal document with an assumed rate."
)


class ZRAConfigError(Exception):
    """A business or product is missing something ZRA fiscalization
    genuinely requires - a TPIN, a device serial, a product's VAT
    category. Raised instead of silently defaulting, the same invariant
    every other waterfall in this project already follows (matcher.py's
    never-guess-the-amount, orders.py's never-partially-confirm)."""


@dataclass
class ZRACredentials:
    """One business's VSDC connection details - see db.save_zra_settings().
    server_url is never a fixed constant: ZRA's own model has a taxpayer
    either run a local VSDC (a WAR/JAR file from the Smart Invoice portal)
    or use a certified third party's hosted one, so the host is
    inherently per-deployment, not something this module can default."""
    server_url: str
    username: str
    password: str
    tpin: str
    bhf_id: str
    device_serial: str

    @classmethod
    def from_settings_row(cls, row: dict) -> "ZRACredentials":
        return cls(
            server_url=row["server_url"].rstrip("/"), username=row["username"],
            password=row["password"], tpin=row["tpin"], bhf_id=row["bhf_id"],
            device_serial=row["device_serial"],
        )


class ZRAClient:
    """Thin REST client over the VSDC API. `session` is injectable so
    tests never make a real HTTP call - there is no live sandbox to call
    anyway."""

    def __init__(self, credentials: ZRACredentials, session: requests.Session | None = None):
        self.credentials = credentials
        self.session = session or requests.Session()
        self._token: str | None = None

    def authenticate(self) -> str:
        """POST {server_url}/api/v1/Users/GetToken, form-encoded
        {username, password}. Response shape: {"Result": {"token": ...},
        "expires_in": ...} - sourced from the reference implementation's
        ZRAAuthService, not guessed."""
        url = f"{self.credentials.server_url}/api/v1/Users/GetToken"
        response = self.session.post(
            url,
            data={"username": self.credentials.username, "password": self.credentials.password},
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        response.raise_for_status()
        token = (response.json().get("Result") or {}).get("token")
        if not token:
            raise ZRAConfigError("ZRA authentication succeeded but no token was returned in Result.token")
        self._token = token
        return token

    def _headers(self) -> dict:
        if not self._token:
            self.authenticate()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def initialize_device(self) -> dict:
        """POST /api/v1/InitializationInfo/selectInitInfo - a one-time
        step per device. Response's Result.resultCd: "000" = success,
        "902" = already initialized (both fine to proceed from); anything
        else is a real failure the caller should surface, not swallow."""
        url = f"{self.credentials.server_url}/api/v1/InitializationInfo/selectInitInfo"
        payload = {
            "tpin": self.credentials.tpin,
            "bhfId": self.credentials.bhf_id,
            "dvcSrlNo": self.credentials.device_serial,
        }
        response = self.session.post(url, json=payload, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def submit_sale(self, payload: dict) -> dict:
        """POST /api/v1/SalesInformation/saveSales. `payload` must already
        be a complete, correctly-shaped ZRA sales payload - see
        build_sales_payload(), which is the only supported way to build
        one in this codebase."""
        url = f"{self.credentials.server_url}/api/v1/SalesInformation/saveSales"
        response = self.session.post(url, json=payload, headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()


def _vat_rate_for_category(code: str) -> float:
    if code == STANDARD_VAT_CATEGORY:
        return STANDARD_VAT_RATE_PERCENT
    if code in ZERO_RATED_VAT_CATEGORIES:
        return 0.0
    supported = ", ".join(sorted({STANDARD_VAT_CATEGORY, *ZERO_RATED_VAT_CATEGORIES}))
    raise ZRAConfigError(UNSUPPORTED_VAT_CATEGORIES_MESSAGE.format(code=code, supported=supported))


def build_sales_payload(credentials: ZRACredentials, invoice_id: str, invoice_date: str,
                         customer_name: str, customer_tpin: str | None,
                         line_items: list[dict], currency: str = "ZMW") -> dict:
    """Builds a real-shaped ZRA `saveSales` payload from this system's own
    invoice + catalog data. `line_items` is a list of dicts, one per
    product on the invoice:
        {"product_id", "name", "quantity", "unit_price",
         "vat_category_code", "item_class_code"}
    (db.get_catalog()'s row shape covers all but quantity, which comes
    from the order/invoice line itself).

    Refuses (ZRAConfigError) rather than guesses when any line is missing
    vat_category_code/item_class_code, or carries a vat_category_code
    this module has no verified rate for - see the module docstring for
    why that's treated as a harder failure than this project's usual
    "flag for a human" pattern.

    Field names (tpin, bhfId, cisInvcNo, salesDt, custTpin, custNm,
    currencyTyCd, totItemCnt, totAmt, totTaxAmt, totTaxblAmt, itemList[]
    with itemSeq/itemCd/itemNm/itemClsCd/qty/qtyUnitCd/prc/splyAmt/
    vatAmt/totAmt/vatTaxblAmt/vatCatCd) are sourced from the reference
    implementation's build_invoice_payload(), simplified to the fields
    that don't depend on ERPNext-specific concepts (packaging codes,
    discounts, MTV/RRP retail-price rules) this system has no equivalent
    of and isn't guessing values for."""
    if not line_items:
        raise ZRAConfigError(f"Invoice {invoice_id} has no line items - nothing to fiscalize.")

    missing = [
        li["product_id"] for li in line_items
        if not li.get("vat_category_code") or not li.get("item_class_code")
    ]
    if missing:
        raise ZRAConfigError(
            f"Invoice {invoice_id} cannot be fiscalized - these products are missing "
            f"vat_category_code and/or item_class_code: {missing}. "
            f"Set them via db.update_product_tax_fields() before fiscalizing."
        )

    items = []
    tot_amt = tot_tax_amt = tot_taxbl_amt = 0.0
    for idx, li in enumerate(line_items, start=1):
        qty = float(li["quantity"])
        unit_price = float(li["unit_price"])
        vat_category_code = li["vat_category_code"]
        rate_percent = _vat_rate_for_category(vat_category_code)

        taxable_amt = round(qty * unit_price, 2)
        vat_amt = round(taxable_amt * rate_percent / 100, 2)
        line_total = round(taxable_amt + vat_amt, 2)

        items.append({
            "itemSeq": idx,
            "itemCd": li["product_id"],
            "itemNm": li["name"],
            "itemClsCd": li["item_class_code"],
            "qty": qty,
            "qtyUnitCd": "EA",  # unit-of-quantity codes come from ZRA's own
                                # reference table (CodeData/selectCodes) -
                                # this system has no cached copy of it, so
                                # every line uses the generic "each" code
                                # rather than guessing a more specific one.
            "prc": unit_price,
            "splyAmt": taxable_amt,
            "vatAmt": vat_amt,
            "totAmt": line_total,
            "vatTaxblAmt": taxable_amt,
            "vatCatCd": vat_category_code,
        })
        tot_amt += line_total
        tot_tax_amt += vat_amt
        tot_taxbl_amt += taxable_amt

    return {
        "tpin": credentials.tpin,
        "bhfId": credentials.bhf_id,
        "cisInvcNo": invoice_id,
        "salesDt": invoice_date,
        "custTpin": customer_tpin or "",
        "custNm": customer_name,
        "currencyTyCd": currency,
        "totItemCnt": len(items),
        "totAmt": round(tot_amt, 2),
        "totTaxAmt": round(tot_tax_amt, 2),
        "totTaxblAmt": round(tot_taxbl_amt, 2),
        "cfmDt": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "itemList": items,
    }
