from reconciler.whatsapp import (
    LoggingWhatsAppClient, get_client, parse_webhook_payload, verify_webhook_subscription,
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
