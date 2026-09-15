"""
Loaders for invoice files and MoMo statement files.

Real distributor exports will not have consistent column names — one business's
Excel says "Invoice No", another says "Inv#", another says "Invoice Number".
Rather than force every customer to reformat their files before you can even
look at their data, these loaders accept a list of known aliases per field and
pick whichever one is present. If a file doesn't match anything, it raises a
clear error naming the columns it found, so you can add a new alias in one
place instead of writing one-off parsing code per customer.

Add new column-name variants to the *_ALIASES dicts as you meet them in the
field — that list will grow faster than the matching logic itself.
"""

from __future__ import annotations

import pandas as pd
from pathlib import Path

INVOICE_ALIASES = {
    "invoice_id": ["invoice_id", "invoice no", "invoice number", "inv no",
                    "inv#", "invoice", "inv_no", "invoiceno"],
    "customer_name": ["customer_name", "customer", "customer name", "client",
                       "client name", "buyer", "name"],
    "customer_phone": ["customer_phone", "phone", "phone number", "mobile",
                        "contact", "customer contact", "cell"],
    "amount": ["amount", "total", "invoice amount", "value", "total amount",
               "amount due"],
    "date": ["date", "invoice date", "issued", "issue date", "created"],
}

MOMO_ALIASES = {
    "transaction_id": ["transaction_id", "transaction id", "txn id", "txn",
                        "ref id", "transaction reference", "trans id"],
    "amount": ["amount", "value", "credit", "credit amount", "amount received"],
    "date": ["date", "transaction date", "date/time", "datetime", "timestamp"],
    "sender_name": ["sender_name", "sender", "name", "payer", "payer name",
                     "from", "customer name"],
    "sender_phone": ["sender_phone", "sender phone", "phone", "sender number",
                      "msisdn", "from number", "payer number"],
    "reference": ["reference", "narration", "note", "notes", "description",
                  "memo", "remarks", "reference/note"],
}


def _normalize_columns(df: pd.DataFrame, aliases: dict) -> pd.DataFrame:
    """Rename whatever columns exist to the canonical field names in `aliases`."""
    lookup = {col.strip().lower(): col for col in df.columns}
    rename_map = {}
    missing = []
    for canonical, options in aliases.items():
        found = None
        for opt in options:
            if opt in lookup:
                found = lookup[opt]
                break
        if found is None:
            missing.append(canonical)
        else:
            rename_map[found] = canonical

    if missing:
        raise ValueError(
            f"Could not find a column for: {missing}. "
            f"Columns present in the file: {list(df.columns)}. "
            f"Add the real header name to the matching *_ALIASES list in loaders.py."
        )

    return df.rename(columns=rename_map)


def _clean_str(series: pd.Series) -> pd.Series:
    """Stringify a column safely. Pandas infers float64 for any numeric-looking
    column that also has a blank cell (a common case for phone numbers with
    missing entries), which turns '260966222222' into '260966222222.0' on a
    naive .astype(str). This collapses that back to a clean string."""
    def clean(v):
        if pd.isna(v):
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()
    return series.apply(clean)


def _read_any(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        # Left as default type inference: Excel numeric ID columns are handled
        # by _clean_str's float -> int-string cleanup, which needs the raw
        # float pandas infers. Forcing dtype=str here would feed _clean_str an
        # already-stringified "1001.0" and break that cleanup.
        return pd.read_excel(path)
    # dtype=str keeps every field as the literal text in the file — notably
    # preserving leading zeros on phone numbers (CSV has no numeric cell type
    # to lose them to). Blank cells still come through as real NaN, and
    # pd.to_numeric/pd.to_datetime downstream parse strings fine.
    return pd.read_csv(path, dtype=str)


def load_invoices(path: str | Path) -> pd.DataFrame:
    """
    Returns a DataFrame with columns:
    invoice_id, customer_name, customer_phone, amount, date, balance

    `balance` starts equal to `amount` — the matcher reduces it as payments
    are applied, so partial payments against the same invoice are handled
    correctly even across multiple transactions.
    """
    df = _read_any(path)
    df = _normalize_columns(df, INVOICE_ALIASES)
    df["invoice_id"] = _clean_str(df["invoice_id"])
    df["customer_name"] = _clean_str(df["customer_name"])
    df["customer_phone"] = _clean_str(df.get("customer_phone", pd.Series(dtype=object)))
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["balance"] = df["amount"]

    bad_rows = df[df["amount"].isna() | df["date"].isna()]
    if len(bad_rows):
        print(f"[loaders] Warning: {len(bad_rows)} invoice row(s) had an "
              f"unparseable amount or date and were dropped.")
        df = df.dropna(subset=["amount", "date"])

    non_positive = sorted(df.loc[df["amount"] <= 0, "invoice_id"].unique())
    if non_positive:
        print(f"[loaders] Warning: invoice(s) with a zero or negative amount "
              f"(credit notes?) will never appear as open and won't show up "
              f"in any report sheet: {non_positive}.")

    dupes = sorted(df.loc[df["invoice_id"].duplicated(keep=False), "invoice_id"].unique())
    if dupes:
        print(f"[loaders] Warning: invoice_id is not unique - repeated: {dupes}. "
              f"The matcher will still resolve payments correctly among the open ones, "
              f"but duplicate invoice numbers are worth confirming with the business.")

    return df.reset_index(drop=True)


def load_momo_statement(path: str | Path) -> pd.DataFrame:
    """
    Returns a DataFrame with columns:
    transaction_id, date, amount, sender_name, sender_phone, reference
    """
    df = _read_any(path)
    df = _normalize_columns(df, MOMO_ALIASES)
    df["transaction_id"] = _clean_str(df["transaction_id"])
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["sender_name"] = _clean_str(df.get("sender_name", pd.Series(dtype=object)))
    df["sender_phone"] = _clean_str(df.get("sender_phone", pd.Series(dtype=object)))
    df["reference"] = _clean_str(df.get("reference", pd.Series(dtype=object)))

    bad_rows = df[df["amount"].isna() | df["date"].isna()]
    if len(bad_rows):
        print(f"[loaders] Warning: {len(bad_rows)} transaction row(s) had an "
              f"unparseable amount or date and were dropped.")
        df = df.dropna(subset=["amount", "date"])

    return df.sort_values("date").reset_index(drop=True)
