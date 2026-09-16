"""
WhatsApp transport — Phase 5 ("order capture").

Both directions are isolated behind small interfaces so the real Meta
Cloud API integration (see docs/ARCHITECTURE.md §7 — direct API vs. a
BSP is still an open decision) is a swap later, not a rewrite now. This
mirrors how the project already built MoMo reconciliation against an
uploaded statement well before any live payment API existed.

Inbound: parse_webhook_payload() pulls the handful of fields this
project needs (sender phone, sender name, message text) out of Meta's
webhook JSON shape, tolerant of missing optional fields — a real
payload has plenty this project doesn't use yet (media, statuses,
reactions), and none of that should break parsing.

Outbound: WhatsAppClient is the interface app.py calls to send a reply.
LoggingWhatsAppClient (the default) just records what would have been
sent, so the entire order flow — parse, stock-check, invoice, "send
confirmation" — is testable with zero external credentials.
MetaCloudAPIClient is the real implementation, used automatically once
WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID are configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Meta's real, documented rule (sourced 2026-09-16, not guessed): a
# business can send free-form text only within 24 hours of the
# customer's last inbound message ("the customer service window").
# Anything sent after that MUST be a pre-approved message template, or
# the real Cloud API rejects it outright - this project's original
# free-text-only send_text() would silently fail for a message like the
# payment-received confirmation, which can easily fire days after the
# customer last wrote in.
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)


@dataclass
class IncomingMessage:
    sender_phone: str
    sender_name: str | None
    text: str
    message_id: str | None = None


@dataclass
class OrderItem:
    product_retailer_id: str
    quantity: int
    item_price: float
    currency: str


@dataclass
class IncomingOrder:
    """A completed WhatsApp native Catalog/Cart checkout - arrives
    already structured (exact product identifiers + quantities), unlike
    IncomingMessage's free text. Assumes the distributor's Meta Commerce
    Catalog is set up with each product's retailer_id matching this
    system's own product_id - an operational setup step, not something
    this code can verify; see reconciler/orders.py's
    resolve_native_order() for what happens when that assumption is
    wrong for a given item."""
    sender_phone: str
    sender_name: str | None
    catalog_id: str
    items: list[OrderItem]
    note: str
    message_id: str | None = None


def parse_webhook_payload(payload: dict) -> list[IncomingMessage]:
    """Extracts every free-text message in one Meta webhook POST body.
    Ignores non-text messages - images, stickers, status callbacks, and
    (since Phase 5b) native Catalog/Cart "order" messages, which
    parse_order_messages() below handles separately, since they arrive
    already structured rather than as free text to run through the
    parsing waterfall."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {c.get("wa_id"): c.get("profile", {}).get("name")
                        for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue
                sender_phone = msg.get("from", "")
                text = msg.get("text", {}).get("body", "")
                messages.append(IncomingMessage(
                    sender_phone=sender_phone,
                    sender_name=contacts.get(sender_phone),
                    text=text,
                    message_id=msg.get("id"),
                ))
    return messages


def parse_order_messages(payload: dict) -> list[IncomingOrder]:
    """Extracts every completed native Catalog/Cart checkout ("order"
    type messages) in one Meta webhook POST body - see IncomingOrder's
    docstring. A message with no product_items (shouldn't happen for a
    real checkout, but seen in malformed/test payloads) is skipped
    rather than producing an empty order."""
    orders = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {c.get("wa_id"): c.get("profile", {}).get("name")
                        for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                if msg.get("type") != "order":
                    continue
                order_data = msg.get("order", {})
                items_raw = order_data.get("product_items", [])
                if not items_raw:
                    continue
                sender_phone = msg.get("from", "")
                items = [
                    OrderItem(
                        product_retailer_id=item.get("product_retailer_id", ""),
                        quantity=int(item.get("quantity", 0)),
                        item_price=float(item.get("item_price", 0)),
                        currency=item.get("currency", ""),
                    )
                    for item in items_raw
                ]
                orders.append(IncomingOrder(
                    sender_phone=sender_phone,
                    sender_name=contacts.get(sender_phone),
                    catalog_id=order_data.get("catalog_id", ""),
                    items=items,
                    note=order_data.get("text", ""),
                    message_id=msg.get("id"),
                ))
    return orders


def verify_webhook_subscription(query_params: dict, verify_token: str) -> str | None:
    """Meta's GET handshake when a webhook URL is first registered with
    the platform: echo back hub.challenge only if hub.verify_token
    matches what this app has configured, else the subscription request
    must be rejected (return None, caller responds 403)."""
    if (query_params.get("hub.mode") == "subscribe"
            and query_params.get("hub.verify_token") == verify_token):
        return query_params.get("hub.challenge")
    return None


class WhatsAppClient:
    def send_text(self, to_phone: str, message: str) -> None:
        raise NotImplementedError

    def send_template(self, to_phone: str, template_name: str, language_code: str,
                       parameters: list[str]) -> None:
        """A pre-approved message template, required for anything sent
        outside the 24-hour customer service window - see
        CUSTOMER_SERVICE_WINDOW above. `parameters` fill the template's
        body placeholders ({{1}}, {{2}}, ...) in order."""
        raise NotImplementedError


class LoggingWhatsAppClient(WhatsAppClient):
    """Default client: records every message that would have been sent
    instead of calling any external API. Lets the whole order flow run
    and be tested with no WhatsApp credentials at all."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.sent_templates: list[tuple[str, str, str, list[str]]] = []

    def send_text(self, to_phone: str, message: str) -> None:
        self.sent.append((to_phone, message))

    def send_template(self, to_phone: str, template_name: str, language_code: str,
                       parameters: list[str]) -> None:
        self.sent_templates.append((to_phone, template_name, language_code, parameters))


class MetaCloudAPIClient(WhatsAppClient):
    """The real implementation — not used unless credentials are
    configured (see get_client() below). Still needs the direct-API-vs-
    BSP decision from docs/ARCHITECTURE.md §7 made before this is what a
    real pilot actually talks to."""

    BASE_URL = "https://graph.facebook.com/v21.0"

    def __init__(self, access_token: str, phone_number_id: str):
        self.access_token = access_token
        self.phone_number_id = phone_number_id

    def send_text(self, to_phone: str, message: str) -> None:
        import requests
        requests.post(
            f"{self.BASE_URL}/{self.phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "text",
                "text": {"body": message},
            },
            timeout=10,
        )

    def send_template(self, to_phone: str, template_name: str, language_code: str,
                       parameters: list[str]) -> None:
        """Real payload shape (sourced 2026-09-16 - Meta's own docs
        weren't fetchable, confirmed instead against AWS End User
        Messaging Social's docs and multiple BSPs converging on the same
        structure): messaging_product/to/type as usual, but `template`
        carries name + language + a body component whose parameters fill
        the template's {{1}}, {{2}}, ... placeholders in order. The
        template itself must already exist and be approved in Meta
        Business Manager with a matching placeholder count - this client
        can't create or verify one, same operational-setup-step category
        as Phase 5b's Meta Commerce Catalog retailer_id assumption."""
        import requests
        requests.post(
            f"{self.BASE_URL}/{self.phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self.access_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language_code},
                    "components": [{
                        "type": "body",
                        "parameters": [{"type": "text", "text": p} for p in parameters],
                    }],
                },
            },
            timeout=10,
        )


def is_within_customer_service_window(last_customer_contact: str | None,
                                       now: datetime | None = None) -> bool:
    """True only if `last_customer_contact` (an ISO timestamp - an
    order's placed_at, today's only record of "when did this customer
    last write in") is both present and within the last 24 hours.
    Unknown last-contact (a manually-uploaded invoice, with no order and
    so no record of the customer ever messaging in) fails CLOSED to
    False - never assumes a fresh conversation it has no evidence for,
    the same "never guess" invariant as everywhere else in this
    project."""
    if not last_customer_contact:
        return False
    now = now or datetime.now(timezone.utc)
    contact_at = datetime.fromisoformat(last_customer_contact)
    if contact_at.tzinfo is None:
        contact_at = contact_at.replace(tzinfo=timezone.utc)
    return (now - contact_at) < CUSTOMER_SERVICE_WINDOW


def get_client() -> WhatsAppClient:
    """Real client if WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID are
    set, else the logging stub. Neither is set in any test or the
    default local run, by design."""
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if token and phone_id:
        return MetaCloudAPIClient(token, phone_id)
    return LoggingWhatsAppClient()
