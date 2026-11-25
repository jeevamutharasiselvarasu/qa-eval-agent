"""Integration: a run whose sources and target are read from Azure Blob / ADLS paths (storage faked locally)."""

from conftest import SAMPLES

ACCT = "https://acme.blob.core.windows.net/raw"


def _run(client):
    rid = client.post("/api/runs", json={"jira_key": "DATA-1234"}).json()["id"]
    assert client.post(f"/api/runs/{rid}/jira/retrieve").status_code == 200
    return rid


def test_sources_and_target_from_storage_paths(client, azure):
    azure("acme/raw/in/customers.csv", "customers.csv")
    azure(
        "acme/raw/in/tx/part-1.csv",
        (SAMPLES / "transactions.csv").read_text().splitlines(keepends=True)[0]
        + "".join((SAMPLES / "transactions.csv").read_text().splitlines(keepends=True)[1:5]),
    )
    rest = (SAMPLES / "transactions.csv").read_text().splitlines(keepends=True)
    azure("acme/raw/in/tx/part-2.csv", rest[0] + "".join(rest[5:]))
    azure("acme/raw/in/tx/_SUCCESS", b"")
    azure("acme/raw/out/customer_master.csv", "customer_master_target.csv")
    rid = _run(client)

    chk = client.post("/api/storage/check", json={"path": "abfss://raw@acme.dfs.core.windows.net/in/tx"}).json()
    assert chk["kind"] == "folder" and chk["file_count"] == 2

    c = client.post(f"/api/runs/{rid}/datasets/sources/blob", json={"path": f"{ACCT}/in/customers.csv"})
    assert c.status_code == 200, c.text
    assert c.json()["alias"] == "customers" and c.json()["source_uri"] == f"{ACCT}/in/customers.csv"
    t = client.post(f"/api/runs/{rid}/datasets/sources/blob", json={"path": f"{ACCT}/in/tx/", "alias": "transactions"})
    assert t.status_code == 200, t.text
    assert t.json()["row_count"] == len(rest) - 1, "both part files are read as one table"
    g = client.post(f"/api/runs/{rid}/datasets/target/blob", json={"path": f"{ACCT}/out/customer_master.csv"})
    assert g.status_code == 200 and g.json()["role"] == "TARGET"
    assert client.post(f"/api/runs/{rid}/profile").status_code == 200
    audit_rows = client.get(f"/api/runs/{rid}/audit").json()
    reads = [e for e in audit_rows if e["action"] == "DATASET_READ_FROM_STORAGE"]
    assert len(reads) == 3 and all("acme.blob.core.windows.net" in str(e) for e in reads)


def test_reload_picks_up_new_data_and_keeps_the_alias(client, azure):
    path = azure("acme/raw/in/customers.csv", "customers.csv")
    rid = _run(client)
    ds = client.post(f"/api/runs/{rid}/datasets/sources/blob", json={"path": f"{ACCT}/in/customers.csv", "alias": "cust"}).json()
    same = client.post(f"/api/runs/{rid}/datasets/{ds['id']}/reload").json()
    assert same["unchanged"] is True
    path.write_bytes(path.read_bytes() + b"C999,New Person,RETAIL,new@example.com\r\n")
    r = client.post(f"/api/runs/{rid}/datasets/{ds['id']}/reload")
    assert r.status_code == 200, r.text
    new = r.json()["dataset"]
    assert r.json()["unchanged"] is False and new["alias"] == "cust" and new["row_count"] == ds["row_count"] + 1
    assert [d["alias"] for d in client.get(f"/api/runs/{rid}/datasets").json() if d["role"] == "SOURCE"] == ["cust"]


def test_failed_reload_keeps_the_previous_data(client, azure):
    path = azure("acme/raw/in/customers.csv", "customers.csv")
    rid = _run(client)
    ds = client.post(f"/api/runs/{rid}/datasets/sources/blob", json={"path": f"{ACCT}/in/customers.csv"}).json()
    path.write_bytes(b"\x00\x01 not a csv \xff" * 10)
    r = client.post(f"/api/runs/{rid}/datasets/{ds['id']}/reload")
    assert r.status_code in (400, 422) and "previous version is kept" in r.json()["detail"]
    left = [d for d in client.get(f"/api/runs/{rid}/datasets").json() if d["role"] == "SOURCE"]
    assert [d["id"] for d in left] == [ds["id"]]


def test_errors_reach_the_user(client, azure):
    rid = _run(client)
    r = client.post(f"/api/runs/{rid}/datasets/sources/blob", json={"path": f"{ACCT}/in/x.csv?sig=secret"})
    assert r.status_code == 400 and "SAS token" in r.json()["detail"]
    r = client.post(f"/api/runs/{rid}/datasets/sources/blob", json={"path": f"{ACCT}/in/missing/"})
    assert r.status_code == 404
    up = client.post(f"/api/runs/{rid}/datasets/sources", files={"file": ("c.csv", (SAMPLES / "customers.csv").read_bytes())}).json()
    r = client.post(f"/api/runs/{rid}/datasets/{up['id']}/reload")
    assert r.status_code == 400 and "uploaded as a file" in r.json()["detail"]
