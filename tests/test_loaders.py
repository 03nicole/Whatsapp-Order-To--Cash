import pandas as pd
import pytest

from reconciler.loaders import load_catalog, load_invoices, load_momo_statement


def _write_csv(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# --- load_invoices -----------------------------------------------------

def test_recognizes_aliased_headers(tmp_path):
    """A real distributor export won't use our canonical header names -
    loaders should recognize known aliases (see README: 'If a real file's
    columns aren't recognized')."""
    csv = _write_csv(tmp_path / "inv.csv", (
        "Invoice No,Customer,Phone,Amount,Date\n"
        "INV-1,ABC Traders,0977111111,1000,2026-01-01\n"
    ))
    df = load_invoices(csv)
    assert list(df.columns) >= list(df.columns)  # sanity: no crash
    assert df.loc[0, "invoice_id"] == "INV-1"
    assert df.loc[0, "customer_name"] == "ABC Traders"
    assert df.loc[0, "customer_phone"] == "0977111111"
    assert df.loc[0, "amount"] == 1000
    assert df.loc[0, "balance"] == 1000  # balance starts equal to amount


def test_missing_column_raises_with_helpful_message(tmp_path):
    csv = _write_csv(tmp_path / "inv.csv", (
        "Invoice No,Customer,Phone,Date\n"  # no amount column
        "INV-1,ABC Traders,0977111111,2026-01-01\n"
    ))
    with pytest.raises(ValueError) as exc:
        load_invoices(csv)
    msg = str(exc.value)
    assert "amount" in msg
    assert "Invoice No" in msg  # names the columns it did find


def test_drops_unparseable_rows(tmp_path, capsys):
    csv = _write_csv(tmp_path / "inv.csv", (
        "Invoice No,Customer,Phone,Amount,Date\n"
        "INV-1,ABC Traders,0977111111,1000,2026-01-01\n"
        "INV-2,Bad Row,0977222222,not-a-number,2026-01-02\n"
    ))
    df = load_invoices(csv)
    assert len(df) == 1
    assert df.loc[0, "invoice_id"] == "INV-1"
    assert "unparseable" in capsys.readouterr().out


def test_preserves_leading_zero_on_phone_from_csv(tmp_path):
    """CSV has no numeric cell type, so dtype=str in _read_any should keep the
    leading zero rather than pandas inferring it as a number."""
    csv = _write_csv(tmp_path / "inv.csv", (
        "Invoice No,Customer,Phone,Amount,Date\n"
        "INV-1,ABC Traders,0977111111,1000,2026-01-01\n"
    ))
    df = load_invoices(csv)
    assert df.loc[0, "customer_phone"] == "0977111111"


def test_cleans_float_like_id_from_xlsx(tmp_path):
    """Excel infers numeric columns as float64; a numeric invoice_id like 1001
    must come out as '1001', not '1001.0'."""
    xlsx = tmp_path / "inv.xlsx"
    pd.DataFrame({
        "Invoice No": [1001],
        "Customer": ["ABC Traders"],
        "Phone": ["0977111111"],
        "Amount": [1000],
        "Date": ["2026-01-01"],
    }).to_excel(xlsx, index=False)
    df = load_invoices(xlsx)
    assert df.loc[0, "invoice_id"] == "1001"


def test_warns_on_duplicate_invoice_id(tmp_path, capsys):
    csv = _write_csv(tmp_path / "inv.csv", (
        "Invoice No,Customer,Phone,Amount,Date\n"
        "INV-1,ABC Traders,0977111111,1000,2026-01-01\n"
        "INV-1,ABC Traders,0977111111,2000,2026-01-02\n"
    ))
    load_invoices(csv)
    out = capsys.readouterr().out
    assert "not unique" in out
    assert "INV-1" in out


def test_warns_on_non_positive_amount(tmp_path, capsys):
    csv = _write_csv(tmp_path / "inv.csv", (
        "Invoice No,Customer,Phone,Amount,Date\n"
        "INV-1,ABC Traders,0977111111,-500,2026-01-01\n"
    ))
    df = load_invoices(csv)
    assert len(df) == 1  # not dropped, just warned about
    assert "zero or negative amount" in capsys.readouterr().out


# --- load_momo_statement ------------------------------------------------

def test_momo_recognizes_aliased_headers_and_sorts_by_date(tmp_path):
    csv = _write_csv(tmp_path / "momo.csv", (
        "Txn ID,Date,Amount,Sender,Sender Phone,Narration\n"
        "TXN2,2026-01-02,500,Bob,0977222222,second\n"
        "TXN1,2026-01-01,1000,Alice,0977111111,first\n"
    ))
    df = load_momo_statement(csv)
    assert list(df["transaction_id"]) == ["TXN1", "TXN2"]  # sorted by date
    assert df.loc[0, "reference"] == "first"
    assert df.loc[0, "sender_phone"] == "0977111111"


def test_momo_drops_unparseable_rows(tmp_path, capsys):
    csv = _write_csv(tmp_path / "momo.csv", (
        "Txn ID,Date,Amount,Sender,Sender Phone,Narration\n"
        "TXN1,2026-01-01,1000,Alice,0977111111,ok\n"
        "TXN2,not-a-date,500,Bob,0977222222,bad\n"
    ))
    df = load_momo_statement(csv)
    assert len(df) == 1
    assert "unparseable" in capsys.readouterr().out


# --- load_catalog ------------------------------------------------------

def test_catalog_recognizes_aliased_headers(tmp_path):
    csv = _write_csv(tmp_path / "catalog.csv", (
        "SKU,Product Name,UOM,Price,Stock\n"
        "COKE-24,Coca-Cola 300ml 24-pack,box,120,50\n"
    ))
    df = load_catalog(csv)
    assert df.loc[0, "product_id"] == "COKE-24"
    assert df.loc[0, "name"] == "Coca-Cola 300ml 24-pack"
    assert df.loc[0, "unit_price"] == 120
    assert df.loc[0, "quantity_on_hand"] == 50
    assert df["quantity_on_hand"].dtype.kind == "i"  # int dtype, not float


def test_catalog_drops_unparseable_rows(tmp_path, capsys):
    csv = _write_csv(tmp_path / "catalog.csv", (
        "SKU,Product Name,UOM,Price,Stock\n"
        "COKE-24,Coca-Cola,box,120,50\n"
        "BAD-1,Bad Row,box,not-a-price,10\n"
    ))
    df = load_catalog(csv)
    assert len(df) == 1
    assert "unparseable" in capsys.readouterr().out


def test_catalog_deduplicates_product_id_keeping_last(tmp_path, capsys):
    csv = _write_csv(tmp_path / "catalog.csv", (
        "SKU,Product Name,UOM,Price,Stock\n"
        "COKE-24,Coca-Cola,box,120,50\n"
        "COKE-24,Coca-Cola,box,130,40\n"
    ))
    df = load_catalog(csv)
    assert len(df) == 1
    assert df.loc[0, "unit_price"] == 130
    assert "not unique" in capsys.readouterr().out
