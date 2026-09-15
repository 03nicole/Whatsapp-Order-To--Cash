import io
import re
from pathlib import Path

SAMPLE_DATA = Path(__file__).resolve().parent.parent / "sample_data"


def _upload_sample(client, business="WebTestBiz", date_window="45"):
    with open(SAMPLE_DATA / "invoices.csv", "rb") as inv, \
         open(SAMPLE_DATA / "momo_statement.csv", "rb") as momo:
        data = {
            "business": business,
            "date_window": date_window,
            "invoices": (io.BytesIO(inv.read()), "invoices.csv"),
            "momo_statement": (io.BytesIO(momo.read()), "momo_statement.csv"),
        }
        return client.post("/reconcile", data=data, content_type="multipart/form-data",
                            follow_redirects=True)


def test_index_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Run a reconciliation" in r.data


def test_reconcile_end_to_end_with_sample_data(client):
    r = _upload_sample(client)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Reconciliation complete" in body
    assert "WebTestBiz" in body
    # matches the pinned outcome counts in test_matcher.py's sample_data test
    assert ">6<" in body  # matched
    assert ">1<" in body  # partial


def test_reconcile_rejects_missing_files(client):
    r = client.post("/reconcile", data={"business": "X"},
                     content_type="multipart/form-data", follow_redirects=True)
    assert "Choose an invoices file" in r.get_data(as_text=True)


def test_reconcile_rejects_disallowed_extension(client, tmp_path):
    bad_file = tmp_path / "invoices.txt"
    bad_file.write_text("not a real invoice file")
    with open(bad_file, "rb") as invoices, open(SAMPLE_DATA / "momo_statement.csv", "rb") as momo:
        data = {
            "business": "X",
            "invoices": (invoices, "invoices.txt"),
            "momo_statement": (momo, "momo_statement.csv"),
        }
        r = client.post("/reconcile", data=data, content_type="multipart/form-data",
                         follow_redirects=True)
    assert "a .csv or .xlsx file" in r.get_data(as_text=True)


def test_download_serves_the_generated_report(client):
    r = _upload_sample(client)
    body = r.get_data(as_text=True)
    token = re.search(r"/download/([0-9a-f]+)", body).group(1)

    r = client.get(f"/download/{token}")
    assert r.status_code == 200
    assert r.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert len(r.data) > 1000


def test_download_with_unknown_token_flashes_and_redirects(client):
    r = client.get("/download/doesnotexist", follow_redirects=True)
    assert "expired" in r.get_data(as_text=True)


def test_aging_with_no_prior_run_flashes(client):
    r = client.get("/aging", query_string={"business": "NeverReconciled"}, follow_redirects=True)
    assert "No open invoices on record" in r.get_data(as_text=True)


def test_aging_after_a_reconcile_shows_open_invoices(client):
    _upload_sample(client)
    r = client.get("/aging", query_string={"business": "WebTestBiz"})
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Total outstanding" in body
    assert "8 open invoice" in body  # pinned to the same sample_data outcome


def test_reupload_does_not_undo_a_prior_partial_payment(client):
    """Web-UI-level version of the persistence guarantee already covered in
    test_db.py - re-running through the browser form shouldn't reset
    balances any more than re-running the CLI does."""
    _upload_sample(client)
    r = _upload_sample(client)  # same files again
    body = r.get_data(as_text=True)
    assert "Skipped (already processed in a previous run)" in body


def test_index_lists_known_businesses(client):
    _upload_sample(client, business="Acme Ltd")
    r = client.get("/")
    assert "Acme Ltd" in r.get_data(as_text=True)
