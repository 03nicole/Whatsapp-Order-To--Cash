from datetime import datetime, timedelta, timezone

from reconciler.whatsapp import (
    LoggingWhatsAppClient, get_client, is_within_customer_service_window,
    parse_order_messages, parse_webhook_payload, verify_webhook_subscription,
)


def _payload(messages, contacts=None):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": contacts or [],
                    "messages": messages,
                }
            }]
        }]
    }


def test_parses_a_text_message_with_sender_name():
    payload = _payload(
        messages=[{"from": "260977111111", "id": "wamid.1", "type": "text",
                   "text": {"body": "10 COKE-24"}}],
        contacts=[{"wa_id": "260977111111", "profile": {"name": "ABC Traders"}}],
    )
    messages = parse_webhook_payload(payload)
    assert len(messages) == 1
    m = messages[0]
    assert m.sender_phone == "260977111111"
    assert m.sender_name == "ABC Traders"
    assert m.text == "10 COKE-24"
    assert m.message_id == "wamid.1"


def test_tolerates_missing_contact_name():
    payload = _payload(messages=[{"from": "260977111111", "type": "text",
                                   "text": {"body": "10 COKE-24"}}])
    messages = parse_webhook_payload(payload)
    assert messages[0].sender_name is None


def test_ignores_non_text_messages():
    payload = _payload(messages=[
        {"from": "260977111111", "type": "image", "image": {"id": "media123"}},
        {"from": "260977111111", "type": "text", "text": {"body": "10 COKE-24"}},
    ])
    messages = parse_webhook_payload(payload)
    assert len(messages) == 1
    assert messages[0].text == "10 COKE-24"


def test_empty_payload_yields_no_messages():
    assert parse_webhook_payload({}) == []
    assert parse_webhook_payload({"entry": []}) == []


def test_verify_webhook_subscription_matches_token():
    challenge = verify_webhook_subscription(
        {"hub.mode": "subscribe", "hub.verify_token": "secret", "hub.challenge": "12345"},
        verify_token="secret",
    )
    assert challenge == "12345"


def test_verify_webhook_subscription_rejects_wrong_token():
    result = verify_webhook_subscription(
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
        verify_token="secret",
    )
    assert result is None


def test_verify_webhook_subscription_rejects_wrong_mode():
    result = verify_webhook_subscription(
        {"hub.mode": "unsubscribe", "hub.verify_token": "secret", "hub.challenge": "12345"},
        verify_token="secret",
    )
    assert result is None


def test_logging_client_records_sent_messages():
    client = LoggingWhatsAppClient()
    client.send_text("260977111111", "Your invoice total is K1,200.00")
    assert client.sent == [("260977111111", "Your invoice total is K1,200.00")]


def test_get_client_defaults_to_logging_client_without_credentials(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    assert isinstance(get_client(), LoggingWhatsAppClient)


def test_get_client_uses_real_client_when_credentials_configured(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    from reconciler.whatsapp import MetaCloudAPIClient
    assert isinstance(get_client(), MetaCloudAPIClient)


def test_logging_client_records_sent_templates():
    client = LoggingWhatsAppClient()
    client.send_template("260977111111", "payment_received", "en_US", ["INV-1", "K1,200.00"])
    assert client.sent_templates == [
        ("260977111111", "payment_received", "en_US", ["INV-1", "K1,200.00"]),
    ]


# --- is_within_customer_service_window --------------------------------------

def test_window_true_for_a_message_just_now():
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    last_contact = now.isoformat()
    assert is_within_customer_service_window(last_contact, now=now) is True


def test_window_true_at_23_hours_59_minutes():
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    last_contact = (now - timedelta(hours=23, minutes=59)).isoformat()
    assert is_within_customer_service_window(last_contact, now=now) is True


def test_window_false_at_exactly_24_hours():
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    last_contact = (now - timedelta(hours=24)).isoformat()
    assert is_within_customer_service_window(last_contact, now=now) is False


def test_window_false_a_few_days_later():
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    last_contact = (now - timedelta(days=3)).isoformat()
    assert is_within_customer_service_window(last_contact, now=now) is False


def test_window_false_when_last_contact_is_unknown():
    """No order behind this invoice - no evidence of a fresh
    conversation, so this must never guess "within the window"."""
    assert is_within_customer_service_window(None) is False


def test_window_handles_a_naive_timestamp_as_utc():
    """orders.placed_at is always tz-aware in practice (isoformat() from
    a tz-aware datetime), but the check shouldn't crash on a naive one -
    treats it as UTC rather than raising a tz-aware/naive comparison
    error."""
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    naive_last_contact = "2026-09-16T11:00:00"  # 1 hour ago, no tzinfo
    assert is_within_customer_service_window(naive_last_contact, now=now) is True


# --- parse_order_messages (native Catalog/Cart checkout) -------------------

def _order_payload(product_items, sender_phone="260977111111", sender_name="ABC Traders",
                    message_id="wamid.order1", note="", catalog_id="104954523425094"):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"wa_id": sender_phone, "profile": {"name": sender_name}}],
                    "messages": [{
                        "from": sender_phone, "id": message_id, "type": "order",
                        "order": {"catalog_id": catalog_id, "product_items": product_items,
                                  "text": note},
                    }],
                }
            }]
        }]
    }


def test_parses_a_completed_catalog_order():
    payload = _order_payload([
        {"product_retailer_id": "COKE-24", "quantity": 10, "item_price": 120.0, "currency": "ZMW"},
        {"product_retailer_id": "FANTA-24", "quantity": 5, "item_price": 110.0, "currency": "ZMW"},
    ], note="Deliver tomorrow morning please")
    orders = parse_order_messages(payload)
    assert len(orders) == 1
    order = orders[0]
    assert order.sender_phone == "260977111111"
    assert order.sender_name == "ABC Traders"
    assert order.catalog_id == "104954523425094"
    assert order.note == "Deliver tomorrow morning please"
    assert len(order.items) == 2
    assert order.items[0].product_retailer_id == "COKE-24"
    assert order.items[0].quantity == 10
    assert order.items[0].item_price == 120.0


def test_ignores_text_messages_when_parsing_orders():
    payload = {
        "entry": [{"changes": [{"value": {
            "contacts": [], "messages": [{"from": "260977111111", "type": "text",
                                          "text": {"body": "10 COKE-24"}}],
        }}]}]
    }
    assert parse_order_messages(payload) == []


def test_ignores_an_order_message_with_no_product_items():
    payload = _order_payload([])
    assert parse_order_messages(payload) == []


def test_parse_webhook_payload_ignores_order_type_messages():
    """The text-message parser and the order parser are separate passes -
    an order-type message must never show up as a free-text
    IncomingMessage."""
    payload = _order_payload([
        {"product_retailer_id": "COKE-24", "quantity": 10, "item_price": 120.0, "currency": "ZMW"},
    ])
    assert parse_webhook_payload(payload) == []
