"""Unit tests: reading source and target data from Azure Blob Storage / ADLS Gen2 paths."""

import io

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import config
from app.services import blob
from app.services.blob import BlobError, parse_path


@pytest.mark.parametrize(
    ("path", "account", "container", "name"),
    [
        ("https://acme.blob.core.windows.net/raw/sales/customers.csv", "acme", "raw", "sales/customers.csv"),
        ("https://acme.dfs.core.windows.net/raw/sales/customers.csv", "acme", "raw", "sales/customers.csv"),
        ("abfss://raw@acme.dfs.core.windows.net/sales/out/", "acme", "raw", "sales/out/"),
        ("wasbs://raw@acme.blob.core.windows.net/a%20b.csv", "acme", "raw", "a b.csv"),
        ("  https://ACME.blob.core.windows.net/raw/x.parquet ", "acme", "raw", "x.parquet"),
    ],
)
def test_azure_paths_are_understood(path, account, container, name):
    ref = parse_path(path)
    assert (ref.account, ref.container, ref.path) == (account, container, name)
    assert ref.uri.startswith(f"https://{account}.blob.core.windows.net/{container}")


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("", "Enter a storage path"),
        ("https://acme.blob.core.windows.net/raw/x.csv?sv=2024&sig=abc", "SAS token"),
        ("https://evil.example.com/raw/x.csv", "Azure storage address"),
        ("https://acme.blob.core.windows.net.evil.com/raw/x.csv", "Azure storage address"),
        ("https://acme.blob.core.windows.net:8443/raw/x.csv", "port"),
        ("https://u:p@acme.blob.core.windows.net/raw/x.csv", "user name"),
        ("abfs://raw@acme.dfs.core.windows.net/x.csv", "unencrypted"),
        ("abfss://acme.dfs.core.windows.net/x.csv", "abfss://<container>@"),
        ("s3://bucket/x.csv", "https://, abfss://"),
        ("https://acme.blob.core.windows.net/Raw_Data/x.csv", "not a valid container"),
        ("https://acme.blob.core.windows.net/raw/../secret/x.csv", "'..'"),
        ("file:///etc/passwd", "https://, abfss://"),
    ],
)
def test_unsafe_or_wrong_paths_are_refused_with_a_clear_message(path, message):
    with pytest.raises(BlobError, match=message):
        parse_path(path)


def test_allowed_accounts_limit_which_storage_can_be_read(monkeypatch):
    monkeypatch.setenv("QA_BLOB_ALLOWED_ACCOUNTS", "acme, other")
    config.reset_settings()
    assert parse_path("https://other.blob.core.windows.net/raw/x.csv").account == "other"
    with pytest.raises(BlobError, match="not in the allowed list") as e:
        parse_path("https://stranger.blob.core.windows.net/raw/x.csv")
    assert e.value.status == 403


def _parquet(rows):
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf)
    return buf.getvalue()


def test_check_a_single_file(azure):
    azure("acme/raw/sales/customers.csv", "customers.csv")
    out = blob.check("https://acme.blob.core.windows.net/raw/sales/customers.csv")
    assert out["kind"] == "file" and out["format"] == "csv" and out["file_count"] == 1


def test_check_a_spark_folder_ignores_markers_and_empty_files(azure):
    azure("acme/raw/out/part-0001.parquet", _parquet([{"ID": 1}]))
    azure("acme/raw/out/part-0002.parquet", _parquet([{"ID": 2}]))
    azure("acme/raw/out/_SUCCESS", b"")
    azure("acme/raw/out/_committed_123", b"{}")
    azure("acme/raw/out/.part-0001.parquet.crc", b"x")
    azure("acme/raw/outside.parquet", _parquet([{"ID": 9}]))  # same prefix "out" but not in the folder
    out = blob.check("abfss://raw@acme.dfs.core.windows.net/out")
    assert out["kind"] == "folder" and out["format"] == "parquet"
    assert [f["name"] for f in out["files"]] == ["out/part-0001.parquet", "out/part-0002.parquet"]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"acme/raw/mix/a.csv": b"A\n1\n", "acme/raw/mix/b.parquet": None}, "mixes file types"),
        ({"acme/raw/mix/a.xlsx": b"x", "acme/raw/mix/b.xlsx": b"y"}, "Excel files cannot be combined"),
        ({"acme/raw/mix/readme.md": b"x"}, "Nothing found"),
    ],
)
def test_folders_that_cannot_be_read_as_one_table(azure, files, message):
    for k, v in files.items():
        azure(k, v if v is not None else _parquet([{"A": 1}]))
    with pytest.raises(BlobError, match=message):
        blob.check("https://acme.blob.core.windows.net/raw/mix/")


def test_unsupported_single_file_and_size_and_count_limits(azure, monkeypatch):
    azure("acme/raw/doc.pdf", b"%PDF")
    with pytest.raises(BlobError, match="not a supported file"):
        blob.check("https://acme.blob.core.windows.net/raw/doc.pdf")
    for i in range(4):
        azure(f"acme/raw/many/p{i}.csv", b"A\n1\n")
    monkeypatch.setenv("QA_BLOB_MAX_FILES", "3")
    config.reset_settings()
    with pytest.raises(BlobError, match="more than 3 files"):
        blob.check("https://acme.blob.core.windows.net/raw/many")
    monkeypatch.setenv("QA_BLOB_MAX_FILES", "500")
    monkeypatch.setenv("QA_MAX_UPLOAD_BYTES", "5")
    config.reset_settings()
    with pytest.raises(BlobError, match="above the configured limit") as e:
        blob.check("https://acme.blob.core.windows.net/raw/many")
    assert e.value.status == 413


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (type("ClientAuthenticationError", (Exception,), {})("no login"), 401, "az login"),
        (type("HttpResponseError", (Exception,), {"status_code": 403})("forbidden"), 403, "Storage Blob Data Reader"),
        (type("ServiceRequestError", (Exception,), {})("dns"), 502, "Could not reach"),
        (RuntimeError("boom"), 502, "Azure storage error"),
    ],
)
def test_azure_errors_become_clear_messages(azure, error, status, message):
    azure.state["error"] = error
    with pytest.raises(BlobError, match=message) as e:
        blob.check("https://acme.blob.core.windows.net/raw/x.csv")
    assert e.value.status == status


def test_missing_container(azure):
    with pytest.raises(BlobError, match="Container 'nope' was not found"):
        blob.check("https://acme.blob.core.windows.net/nope/x.csv")


def test_fetch_one_file_keeps_its_name(azure, tmp_path):
    azure("acme/raw/sales/customers.csv", "customers.csv")
    local, name, ref, _ = blob.fetch("https://acme.blob.core.windows.net/raw/sales/customers.csv", tmp_path)
    assert name == "customers.csv" and local.read_bytes().startswith(b"CUSTOMER_ID")
    assert not list(tmp_path.glob(".blob_*")), "the download folder is cleaned up"


def test_fetch_a_folder_combines_the_parts_into_one_table(azure, tmp_path):
    azure("acme/raw/tx/part-1.csv", b"ID,AMT\n1,10.5\n2,3\n")
    azure("acme/raw/tx/part-2.csv", b"ID,AMT\n3,7\n")
    local, name, _, listing = blob.fetch("https://acme.blob.core.windows.net/raw/tx", tmp_path)
    t = pq.read_table(local)
    assert name == "tx.parquet" and listing["kind"] == "folder" and t.num_rows == 3
    assert sorted(t.column("ID").to_pylist()) == [1, 2, 3]

    azure("acme/raw/pq/a.parquet", _parquet([{"ID": 1, "X": "a"}]))
    azure("acme/raw/pq/b.parquet", _parquet([{"ID": 2, "Y": "b"}]))  # columns are matched by name
    local, *_ = blob.fetch("https://acme.blob.core.windows.net/raw/pq/", tmp_path)
    assert set(pq.read_table(local).column_names) == {"ID", "X", "Y"}


def test_fetching_the_same_folder_twice_gives_the_same_bytes(azure, tmp_path):
    for i in range(3):
        azure(f"acme/raw/tx/part-{i}.parquet", _parquet([{"ID": i * 10 + j} for j in range(50)]))
    a, *_ = blob.fetch("https://acme.blob.core.windows.net/raw/tx", tmp_path)
    b, *_ = blob.fetch("https://acme.blob.core.windows.net/raw/tx", tmp_path)
    assert a.read_bytes() == b.read_bytes()
