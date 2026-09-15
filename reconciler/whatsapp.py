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


@dataclass
class IncomingMessage:
    sender_phone: str
    sender_name: str | None
    text: str
    message_id: str | None = None


def parse_webhook_payload(payload: dict) -> list[IncomingMessage]:
    """Extracts every text message in one Meta webhook POST body. Ignores
    non-text messages (images, stickers, status callbacks) — Phase 5 is
    structured text ordering, not media parsing."""
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


class LoggingWhatsAppClient(WhatsAppClient):
    """Default client: records every message that would have been sent
    instead of calling any external API. Lets the whole order flow run
    and be tested with no WhatsApp credentials at all."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to_phone: str, message: str) -> None:
        self.sent.append((to_phone, message))


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


def get_client() -> WhatsAppClient:
    """Real client if WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID are
    set, else the logging stub. Neither is set in any test or the
    default local run, by design."""
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if token and phone_id:
        return MetaCloudAPIClient(token, phone_id)
    return LoggingWhatsAppClient()
