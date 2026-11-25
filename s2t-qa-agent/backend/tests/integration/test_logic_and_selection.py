"""Test cases derived from the transformation logic, and running only the selected test cases."""

import time

from conftest import complete_requirements, wait_exec
from test_target_spec import _run

from app.services.logic_tests import Logic, props


def _ready(client):
    rid = _run(client)
    complete_requirements(client, rid)
    return rid


def _approve(client, rid):
    tv = client.get(f"/api/runs/{rid}/transformations").json()["current"]
    assert client.post(f"/api/runs/{rid}/transformations/{tv['id']}/approve", json={"decision": "APPROVED"}).status_code == 200
    r = client.post(f"/api/runs/{rid}/test-pack/approve", json={"decision": "APPROVED"})
    assert r.status_code == 200, r.text


def test_sql_analysis_finds_the_properties_the_logic_guarantees():
    lg = Logic("""SELECT c.ID, UPPER(TRIM(c.NAME)) AS NAME, COALESCE(ROUND(SUM(t.AMT), 2), 0) AS TOTAL, COUNT(t.ID) AS N,
                  CASE WHEN c.TIER = 'G' THEN 'GOLD' WHEN c.TIER = 'S' THEN 'SILVER' END AS TIER, LEFT(c.CODE, 3) AS CODE,
                  ABS(c.DELTA) AS DELTA
                  FROM cust c INNER JOIN (SELECT * FROM txn WHERE STATUS = 'OK') t ON c.ID = t.CID GROUP BY ALL""")
    assert not lg.error
    p = {n: props(e) for n, e in lg.columns}
    assert p["NAME"].keys() >= {"UPPER", "TRIMMED"}
    assert p["TOTAL"]["NOT_NULL"] and p["TOTAL"]["MAX_DECIMALS"] == 2
    assert p["N"]["NON_NEGATIVE_INTEGER"]
    assert p["TIER"]["ALLOWED_VALUES"] == ["GOLD", "SILVER"] and p["TIER"]["_ALLOW_NULL"]
    assert p["CODE"]["MAX_LENGTH"] == 3 and p["DELTA"]["NON_NEGATIVE"]
    assert lg.base[0] == "cust" and lg.joins[0]["type"] == "INNER" and lg.joins[0]["table"] == "txn"
    assert lg.joins[0]["conds"] == ['"t"."STATUS" = \'OK\''] or "STATUS" in lg.joins[0]["conds"][0]
    assert lg.group == ["ID", "NAME", "TIER", "CODE", "DELTA"]


def test_logic_tests_are_generated_from_the_sql_and_catch_target_defects(client):
    rid = _ready(client)
    tcs = client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"]
    tl = {t["title"]: t for t in tcs if t["basis"] == "TRANSFORMATION_LOGIC"}
    assert all(t["id"].startswith("TC-TL-") and t["ac_ids"] == [] for t in tl.values())
    assert "CUSTOMER_NAME is upper-case in the uploaded target" in tl
    assert any("LEFT JOIN keeps every customers record" in k for k in tl)
    assert any("one row per CUSTOMER_ID" in k for k in tl)
    assert any("NET_AMOUNT has at most 2 decimal" in k for k in tl)
    assert all("Derived from the SQL" in t["rationale"] for t in tl.values())
    pack = client.get(f"/api/runs/{rid}/test-pack").json()
    assert pack["by_basis"]["TRANSFORMATION_LOGIC"] == len(tl) and pack["by_basis"]["ACCEPTANCE_CRITERIA"] > 0
    _approve(client, rid)
    client.put(f"/api/runs/{rid}/test-cases/selection", json={"only": [t["id"] for t in tl.values()]})
    ex = client.post(f"/api/runs/{rid}/run-validation", json={}).json()
    assert [e["track"] for e in ex] == ["ACTUAL"]
    res = {r["test_case_id"]: r for r in wait_exec(client, ex[0]["id"])["results"]}
    assert set(res) == {t["id"] for t in tl.values()}
    upper = tl["CUSTOMER_NAME is upper-case in the uploaded target"]["id"]
    assert res[upper]["status"] == "FAIL"  # sample target has 'Anita Kapoor' for C105


def test_hand_written_sql_with_case_inner_join_and_filter_gets_its_own_tests(client):
    rid = _ready(client)
    sql = """SELECT c.CUSTOMER_ID, UPPER(TRIM(c.CUSTOMER_NAME)) AS CUSTOMER_NAME,
       CASE WHEN c.SEGMENT = 'Retail' THEN 'RETAIL' ELSE 'OTHER' END AS SEGMENT,
       COALESCE(ROUND(SUM(t.AMOUNT - COALESCE(t.DISCOUNT, 0)), 2), 0) AS NET_AMOUNT, COUNT(t.TXN_ID) AS TXN_COUNT
FROM customers c INNER JOIN transactions t ON c.CUSTOMER_ID = t.CUSTOMER_ID
GROUP BY ALL"""
    assert client.post(f"/api/runs/{rid}/transformations", json={"mode": "SQL", "code": sql}).status_code == 200
    tcs = client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"]
    tl = {t["title"]: t for t in tcs if t["basis"] == "TRANSFORMATION_LOGIC"}
    seg = tl["SEGMENT only contains the values the CASE expression can return"]
    assert seg["validation"]["arg"] == ["OTHER", "RETAIL"]
    inner = next(t for k, t in tl.items() if "INNER JOIN" in k)
    assert inner["validation"]["type"] == "KEY_SUBSET"
    _approve(client, rid)
    client.put(f"/api/runs/{rid}/test-cases/selection", json={"only": [seg["id"], inner["id"]]})
    ex = client.post(f"/api/runs/{rid}/run-validation", json={}).json()
    res = {r["test_case_id"]: r for r in wait_exec(client, ex[0]["id"])["results"]}
    assert res[seg["id"]]["status"] == "FAIL"  # the uploaded target holds the original segment names
    assert res[inner["id"]]["status"] == "FAIL"  # C108-like customers without transactions are in the target


def test_only_selected_tests_run_and_the_report_says_so(client):
    rid = _ready(client)
    _approve(client, rid)
    tcs = client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"]
    pick = [t["id"] for t in tcs if t["validation"].get("scope") and t["state"] == "READY"][:3] + [
        t["id"] for t in tcs if not t["validation"].get("scope") and t["state"] == "READY"
    ][:2]
    r = client.put(f"/api/runs/{rid}/test-cases/selection", json={"only": []})
    assert r.json()["selected"] == []
    assert client.post(f"/api/runs/{rid}/run-validation", json={}).status_code == 400
    assert client.put(f"/api/runs/{rid}/test-cases/selection", json={"only": ["TC-NOPE"]}).status_code == 400
    client.put(f"/api/runs/{rid}/test-cases/selection", json={"only": pick})
    st = client.get(f"/api/runs/{rid}/test-pack").json()
    assert st["pack_approved"] and st["selected"] == 5  # choosing tests needs no re-approval
    ex = client.post(f"/api/runs/{rid}/run-validation", json={}).json()
    done = [wait_exec(client, e["id"], 240) for e in ex]
    ran = {r["test_case_id"] for d in done for r in d["results"]}
    assert ran == set(pick)
    for _ in range(50):
        reps = client.get(f"/api/runs/{rid}/reports").json()
        if reps:
            break
        time.sleep(0.3)
    assert reps
    data = client.get(f"/api/runs/{rid}/report-data").json()
    assert data["summary"]["selection"]["selected"] == 5 and data["summary"]["counts"]["total"] == 5
    assert len(data["summary"]["selection"]["not_selected"]) == len(tcs) - 5
    # the same selection again reuses the results; a different one runs again
    again = client.post(f"/api/runs/{rid}/run-validation", json={}).json()
    assert {e["id"] for e in again} == {e["id"] for e in ex}
    client.put(f"/api/runs/{rid}/test-cases/selection", json={"deselect": [pick[0]]})
    third = client.post(f"/api/runs/{rid}/run-validation", json={}).json()
    assert {e["id"] for e in third} != {e["id"] for e in ex}
    # a rebuilt pack keeps the choice
    client.post(f"/api/runs/{rid}/test-pack")
    sel = {t["id"] for t in client.get(f"/api/runs/{rid}/test-cases").json()["test_cases"] if t["selected"]}
    assert sel == set(pick[1:])
