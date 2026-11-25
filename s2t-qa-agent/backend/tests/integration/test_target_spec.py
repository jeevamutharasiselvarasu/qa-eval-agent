"""Requirements per target column: open items gate the SQL; in-app edits generate it and survive Jira re-retrieval."""

from conftest import SAMPLES


def _run(client, target="customer_master_target.csv"):
    rid = client.post("/api/runs", json={"jira_key": "DATA-1234"}).json()["id"]
    assert client.post(f"/api/runs/{rid}/jira/retrieve").status_code == 200
    for f, role in [("customers.csv", "sources"), ("transactions.csv", "sources"), (target, "target")]:
        with open(SAMPLES / f, "rb") as fh:
            assert client.post(f"/api/runs/{rid}/datasets/{role}", files={"file": (f, fh)}).status_code == 200
    assert client.post(f"/api/runs/{rid}/profile").status_code == 200
    assert client.post(f"/api/runs/{rid}/mappings/generate").status_code == 200
    return rid


def _col(spec, name):
    return next(c for c in spec["columns"] if c["target"] == name)


def test_spec_lists_each_target_column_with_its_source_and_rule(client):
    rid = _run(client)
    spec = client.get(f"/api/runs/{rid}/target-spec").json()
    assert [c["target"] for c in spec["columns"]] == ["CUSTOMER_ID", "CUSTOMER_NAME", "SEGMENT", "NET_AMOUNT", "TXN_COUNT"]
    name = _col(spec, "CUSTOMER_NAME")
    assert name["source_columns"] == ["customers.CUSTOMER_NAME"] and name["rule"] == "UPPER(TRIM(customers.CUSTOMER_NAME))"
    assert name["state"] == "CLEAR" and name["origin"] == "JIRA"
    net = _col(spec, "NET_AMOUNT")
    assert net["rule"] == "ROUND(SUM(transactions.AMOUNT - transactions.DISCOUNT), 2)"  # unmatched default shown separately
    assert net["default_when_unmatched"] == 0 and net["group_by"] == ["CUSTOMER_ID"]
    assert net["state"] == "QUESTION" and len(net["open_questions"]) == 2
    assert _col(spec, "CUSTOMER_ID")["is_key"]
    assert spec["table"]["key"] == ["CUSTOMER_ID"] and spec["table"]["joins"] and spec["table"]["filters"]
    # open questions -> not ready -> no SQL, and generation is refused with the open items
    assert not spec["ready"] and spec["sql"]["state"] == "WAITING_FOR_REQUIREMENTS"
    r = client.post(f"/api/runs/{rid}/transformations/generate", json={"mode": "SQL"})
    assert r.status_code == 409 and "NET_AMOUNT" in r.json()["detail"]
    assert client.get(f"/api/runs/{rid}/transformations").json()["current"] is None


def test_editing_the_rule_resolves_the_question_and_generates_sql(client):
    rid = _run(client)
    spec = client.get(f"/api/runs/{rid}/target-spec").json()
    qs = {("null" if "null" in q["description"].lower() else "neg"): q["id"] for q in _col(spec, "NET_AMOUNT")["open_questions"]}
    bad = client.put(f"/api/runs/{rid}/target-spec/columns/NET_AMOUNT", json={"expression": "SUM(transactions.NOPE)"})
    assert bad.status_code == 400 and "not found" in bad.json()["detail"]
    sub = client.put(f"/api/runs/{rid}/target-spec/columns/NET_AMOUNT", json={"expression": "(SELECT 1)"})
    assert sub.status_code == 400
    r = client.put(
        f"/api/runs/{rid}/target-spec/columns/NET_AMOUNT",
        json={"expression": "ROUND(SUM(AMOUNT - COALESCE(DISCOUNT, 0)), 2)", "resolve": {qs["null"]: {"status": "CLARIFIED"}}},
    )
    assert r.status_code == 200, r.text
    spec = r.json()["spec"]
    net = _col(spec, "NET_AMOUNT")
    assert net["origin"] == "EDITED" and "COALESCE(transactions.DISCOUNT, 0)" in net["rule"]
    assert net["jira_rule"] and net["state"] == "QUESTION"  # the negative-value question is still open
    assert not r.json()["sync"]["generated"]
    r = client.post(f"/api/runs/{rid}/target-spec/questions/{qs['neg']}", json={"status": "CLARIFIED", "note": "Negative is allowed."})
    assert r.status_code == 200 and r.json()["sync"]["generated"] and r.json()["sync"]["test_pack_built"]
    spec = r.json()["spec"]
    assert spec["ready"] and spec["sql"]["state"] == "CURRENT"
    code = client.get(f"/api/runs/{rid}/transformations").json()["current"]["code"]
    assert 'COALESCE("transactions"."DISCOUNT", 0)' in code and "Generated from the requirements" in code
    # null-discount tests now run against the confirmed rule instead of being blocked
    tcs = client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"]
    null_disc = next(t for t in tcs if "DISCOUNT is null" in t["title"])
    assert null_disc["state"] == "READY" and null_disc["ac_ids"] == ["AC5"]
    # the edit survives a Jira re-retrieval and the SQL is not regenerated needlessly
    v = client.get(f"/api/runs/{rid}/target-spec").json()["sql"]["version"]
    assert client.post(f"/api/runs/{rid}/jira/retrieve").status_code == 200
    spec = client.get(f"/api/runs/{rid}/target-spec").json()
    assert _col(spec, "NET_AMOUNT")["origin"] == "EDITED" and spec["ready"] and spec["sql"]["version"] == v


def test_accepting_a_limitation_generates_sql_but_keeps_dependent_tests_blocked(client):
    rid = _run(client)
    spec = client.get(f"/api/runs/{rid}/target-spec").json()
    for q in spec["questions"]:
        if q["status"] == "OPEN":
            r = client.post(f"/api/runs/{rid}/target-spec/questions/{q['id']}", json={"status": "ACCEPTED_LIMITATION", "note": ""})
            assert r.status_code == 400  # a note is required
            client.post(f"/api/runs/{rid}/target-spec/questions/{q['id']}", json={"status": "ACCEPTED_LIMITATION", "note": "Unknown"})
    spec = client.get(f"/api/runs/{rid}/target-spec").json()
    assert spec["ready"] and spec["sql"]["state"] == "CURRENT"
    tcs = client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"]
    assert next(t for t in tcs if "DISCOUNT is null" in t["title"])["state"] == "BLOCKED"


def test_table_rules_edit_and_hand_written_sql_is_not_overwritten(client):
    rid = _run(client)
    for q in client.get(f"/api/runs/{rid}/target-spec").json()["questions"]:
        if q["status"] == "OPEN":
            client.post(f"/api/runs/{rid}/target-spec/questions/{q['id']}", json={"status": "CLARIFIED", "note": "ok"})
    bad = client.put(
        f"/api/runs/{rid}/target-spec/table-rules",
        json={"joins": [{"type": "LEFT", "left": "customers", "right": "transactions", "keys": ["NOPE"]}]},
    )
    assert bad.status_code == 400
    r = client.put(
        f"/api/runs/{rid}/target-spec/table-rules",
        json={"filters": [{"dataset": "transactions", "column": "status", "op": "!=", "value": "CANCELLED"}]},
    )
    assert r.status_code == 200, r.text
    spec = r.json()["spec"]
    assert [f["expression"] for f in spec["table"]["filters"]] == ["transactions.STATUS != 'CANCELLED'"]
    code = client.get(f"/api/runs/{rid}/transformations").json()["current"]["code"]
    assert "!= 'CANCELLED'" in code
    # a person edits the SQL: later requirement edits do not overwrite it, they flag it as outdated
    client.post(f"/api/runs/{rid}/transformations", json={"mode": "SQL", "code": code + "\n-- reviewed"})
    r = client.put(f"/api/runs/{rid}/target-spec/table-rules", json={"filters": []})
    assert r.status_code == 200 and not r.json()["sync"]["generated"]
    assert r.json()["spec"]["sql"]["state"] == "MANUAL_OUTDATED"
    r = client.post(f"/api/runs/{rid}/target-spec/sync", json={"force": True})
    assert r.json()["sync"]["generated"] and r.json()["spec"]["sql"]["state"] == "CURRENT"
    assert "CANCELLED" not in client.get(f"/api/runs/{rid}/transformations").json()["current"]["code"]


def test_missing_rule_is_an_open_item_until_the_user_writes_it(client):
    rid = _run(client)
    spec = client.get(f"/api/runs/{rid}/target-spec").json()
    # remove the Jira rule for SEGMENT by rejecting it, then the column has no rule
    for r in client.get(f"/api/runs/{rid}/requirements").json()["requirements"]:
        if r["kind"] == "DIRECT_MAPPING" and r["spec"].get("target") == "SEGMENT":
            client.patch(f"/api/runs/{rid}/requirements/{r['id']}", json={"status": "REJECTED"})
    for r in client.get(f"/api/runs/{rid}/requirements").json()["requirements"]:
        if r["kind"] == "MAPPING_SPEC" and r["spec"].get("target") == "SEGMENT":
            client.patch(f"/api/runs/{rid}/requirements/{r['id']}", json={"status": "REJECTED"})
    spec = client.post(f"/api/runs/{rid}/target-spec/sync", json={}).json()["spec"]
    seg = _col(spec, "SEGMENT")
    assert seg["state"] == "SUGGESTED" and seg["rule"] == "customers.SEGMENT"  # name match only: needs confirmation
    assert any(i["ref"] == "SEGMENT" for i in spec["open_items"])
    r = client.put(f"/api/runs/{rid}/target-spec/columns/SEGMENT", json={"expression": seg["rule"]})
    assert r.status_code == 200 and _col(r.json()["spec"], "SEGMENT")["state"] == "CLEAR"
