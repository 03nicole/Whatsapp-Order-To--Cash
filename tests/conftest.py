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
