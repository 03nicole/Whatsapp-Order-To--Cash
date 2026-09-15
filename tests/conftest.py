import sys
from pathlib import Path

# Make `reconciler` importable regardless of how pytest is invoked (bare
# `pytest`, `python -m pytest`, or from a different cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest


def invoice_row(invoice_id, customer_name="", customer_phone="", amount=0.0, date="2026-01-01"):
    return dict(invoice_id=str(invoice_id), customer_name=customer_name, customer_phone=customer_phone,
                amount=float(amount), date=pd.Timestamp(date), balance=float(amount))


def txn_row(transaction_id, amount, date="2026-01-01", sender_name="", sender_phone="", reference=""):
    return dict(transaction_id=str(transaction_id), date=pd.Timestamp(date), amount=float(amount),
                sender_name=sender_name, sender_phone=sender_phone, reference=reference)


@pytest.fixture
def make_invoices():
    """Builds an invoices DataFrame shaped like loaders.load_invoices()'s output,
    from a list of dicts using invoice_row's defaults for any field left out."""
    def _make(rows):
        return pd.DataFrame([invoice_row(**r) for r in rows])
    return _make


@pytest.fixture
def make_momo():
    """Builds a MoMo transactions DataFrame shaped like loaders.load_momo_statement()'s
    output, from a list of dicts using txn_row's defaults for any field left out."""
    def _make(rows):
        return pd.DataFrame([txn_row(**r) for r in rows])
    return _make


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client pointed at a throwaway DB and upload dir, so
    tests never touch reconciliation.db or the shared temp upload folder.
    Shared by every *_app.py test file - each one used to define this
    fixture separately, and had already drifted (one had an extra
    WHATSAPP_VERIFY_TOKEN line the others lacked)."""
    import app as app_module  # imported lazily - only Flask-app tests need this
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(app_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(app_module, "WHATSAPP_VERIFY_TOKEN", "test-verify-token")
    app_module.UPLOAD_DIR.mkdir()
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def webhook_payload(text, sender_phone="260977111111", sender_name="ABC Traders", message_id="wamid.1"):
    """Builds a Meta WhatsApp webhook POST body containing one text
    message, matching the shape reconciler.whatsapp.parse_webhook_payload
    expects. Shared by every test that needs to simulate an incoming
    order message - each *_app.py test file used to build this shape by
    hand, with drifting field names and defaults."""
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"wa_id": sender_phone, "profile": {"name": sender_name}}],
                    "messages": [{"from": sender_phone, "id": message_id, "type": "text",
                                  "text": {"body": text}}],
                }
            }]
        }]
    }
