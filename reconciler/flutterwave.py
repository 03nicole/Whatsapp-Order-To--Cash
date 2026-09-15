"""
Flutterwave transport — Phase 10 ("live MoMo webhook integration").

Replaces statement-upload reconciliation with the Daraja-style live
callback pattern described in docs/ARCHITECTURE.md §2 (Safaricom Daraja
reference) — the provider pushes a payment confirmation the instant it
happens, instead of a human uploading a statement export afterward.

Built against Flutterwave specifically, not a direct MTN/Airtel
integration — a decision made when Phase 10 actually started (see
docs/ARCHITECTURE.md §7 and docs/ROADMAP.md's Phase 10 entry for the
full reasoning): Flutterwave has one well-documented, Zambia-confirmed
webhook payload shape covering both MTN and Airtel mobile money
collections, versus building and maintaining two separate telco
integrations with materially less-complete public documentation for
either one. This is a real, sourced decision, not a coin flip — and
it's still worth re-examining once real transaction volume/fees data
exists to compare against direct integration.

Same "prove it without the live integration first" pattern the project
already used for MoMo statement uploads (before this module existed)
and for WhatsApp order capture: everything here is built and tested
against Flutterwave's real documented payload shape
(https://developer.flutterwave.com/docs/webhooks), not a live account —
there are no Flutterwave credentials configured anywhere in this
project, by design, until a real pilot needs this turned on.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

import pandas as pd

# The only payment_type this project reconciles through this webhook -
# a Flutterwave merchant account can carry other payment types (cards,
# other countries' mobile money) through the same URL; those are
# silently ignored, not errors, the same way a non-text WhatsApp
# message is ignored rather than rejected.
ZAMBIA_MOBILE_MONEY_PAYMENT_TYPE = "mobilemoneyzm"

# Only a genuinely completed payment should ever be reconciled - never
# a pending/failed one, and never speculatively "probably fine".
SUCCESSFUL_STATUS = "successful"


@dataclass
class IncomingPayment:
    transaction_id: str
    date: str
    amount: float
    sender_name: str
    sender_phone: str
    reference: str


def verify_signature(headers: dict, secret_hash: str) -> bool:
    """Flutterwave signs every webhook request with a `verif-hash` header
    set to whatever secret hash you configured in your dashboard - not
    an HMAC of the body, just a shared-secret string compare. Constant-
    time comparison (hmac.compare_digest) so this check itself doesn't
    leak the secret through response-timing. Never trust a webhook
    request that fails this - see docs/ARCHITECTURE.md's security
    section on mobile money callback authentication."""
    if not secret_hash:
        return False
    received = headers.get("verif-hash", "")
    return hmac.compare_digest(received, secret_hash)


def parse_webhook_payload(payload: dict) -> IncomingPayment | None:
    """Extracts a single completed Zambia-mobile-money payment from a
    Flutterwave `charge.completed` webhook body, or None if this
    payload isn't one (wrong event, wrong payment_type, not yet
    successful) - the caller should silently skip those, not error.

    `data.id` (Flutterwave's own numeric charge ID) is used as the
    transaction_id rather than `tx_ref` (merchant-supplied, not
    guaranteed unique across every possible integration mistake) or
    `flw_ref` (a display reference, not documented as stable) - `id` is
    the one field Flutterwave's own docs treat as the charge's
    identity."""
    if payload.get("event") != "charge.completed":
        return None

    data = payload.get("data", {})
    if data.get("payment_type") != ZAMBIA_MOBILE_MONEY_PAYMENT_TYPE:
        return None
    if data.get("status") != SUCCESSFUL_STATUS:
        return None

    charge_id = data.get("id")
    amount = data.get("amount")
    if charge_id is None or amount is None:
        return None

    customer = data.get("customer") or {}
    return IncomingPayment(
        transaction_id=str(charge_id),
        date=data.get("created_at", ""),
        amount=float(amount),
        sender_name=customer.get("name") or "",
        sender_phone=customer.get("phone_number") or "",
        # Flutterwave's redirect-based mobile money flow doesn't reliably
        # give the customer a free-text reference field the way a P2P
        # MoMo transfer's narration does - reference is often blank here,
        # which is fine: the waterfall's sender-identification rules
        # (matcher.py rules 2/3) don't depend on it, only rule 1 does.
        reference=data.get("narration") or "",
    )


def to_momo_dataframe(payment: IncomingPayment) -> pd.DataFrame:
    """Shapes one IncomingPayment into the exact single-row DataFrame
    reconciler.matcher.reconcile() already expects as its momo_df
    argument - the same shape loaders.load_momo_statement() produces
    from an uploaded file. This is the whole point of Phase 10: a live
    webhook feeds the SAME reconcile() function a statement upload
    already does, not a parallel matching path."""
    return pd.DataFrame([{
        "transaction_id": payment.transaction_id,
        "date": pd.to_datetime(payment.date, errors="coerce"),
        "amount": payment.amount,
        "sender_name": payment.sender_name,
        "sender_phone": payment.sender_phone,
        "reference": payment.reference,
    }])
