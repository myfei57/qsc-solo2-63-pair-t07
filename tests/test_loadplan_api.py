"""载重配载的控制台路由与导出件。"""

from __future__ import annotations

import json

from tests.support import make_server


def call(server, method, path, payload=None, query=None):
    response = server.call(method, path, payload, query)
    if response.content_type.startswith("application/json"):
        return response.status, json.loads(response.body)
    return response.status, response.body.decode("utf-8")


def _flight_with_envelope(server, code="CA1"):
    status, flight = call(
        server, "POST", "/api/flights", {"code": code, "carousel": "CR1"}
    )
    assert status == 201
    fid = flight["id"]
    status, envelope = call(
        server,
        "POST",
        "/api/load/envelope",
        {
            "flight_id": fid,
            "deadload_cap_g": 88_000,
            "cg_min_cm": 0,
            "cg_max_cm": 200,
            "base_weight_g": 50_000,
            "base_moment_gcm": 5_000_000,
            "piece_max_g": 40_000,
        },
    )
    assert status == 201 and envelope["version"] == 1
    return fid


def _checkin_and_weigh(server, fid, barcode, weight, arm=100):
    status, bag = call(
        server, "POST", "/api/bags/checkin", {"barcode": barcode, "flight_id": fid}
    )
    assert status == 201
    call(
        server,
        "POST",
        "/api/load/weigh",
        {"bag_id": bag["id"], "weight_g": weight, "arm_cm": arm},
    )
    return bag["id"]


def test_envelope_weigh_plan_account_routes(server):
    fid = _flight_with_envelope(server)
    heavy = _checkin_and_weigh(server, fid, "W0001", 30_000)
    light = _checkin_and_weigh(server, fid, "W0002", 10_000)
    tail = _checkin_and_weigh(server, fid, "W0003", 8_000)

    status, plan = call(
        server,
        "POST",
        "/api/load/plan",
        {"flight_id": fid, "batch": 1, "bag_ids": [heavy, light, tail]},
    )
    assert status == 201
    assert plan["pulled_off"] == [heavy]
    assert plan["order"] == [light, tail]

    status, account = call(server, "GET", f"/api/load/account/{fid}")
    assert status == 200
    assert account["balance_g"] == 68_000
    assert account["headroom_g"] == 20_000
    assert len(account["entries"]) == 5

    status, order = call(server, "GET", f"/api/load/order/{fid}")
    assert status == 200 and order["order"] == [light, tail]

    status, decisions = call(server, "GET", "/api/load/decisions", {"flight_id": fid})
    assert status == 200 and len(decisions) == 5
    # 越限与换装结论已进入审计链
    status, verify = call(server, "GET", "/api/audit/verify")
    assert verify == {"intact": True} or verify.get("intact") is True


def test_missing_weight_submit_is_rejected(server):
    fid = _flight_with_envelope(server)
    status, bag = call(
        server, "POST", "/api/bags/checkin", {"barcode": "NW1", "flight_id": fid}
    )
    status, decision = call(
        server,
        "POST",
        "/api/load/submit",
        {"bag_id": bag["id"], "flight_id": fid, "batch": 1},
    )
    assert status == 201
    assert decision["accepted"] is False
    assert decision["reason"] == "missing_weight"
    assert "缺称重记录" in decision["reason_text"]


def test_loadplan_and_weighing_csv_exports_carry_reasons(server):
    fid = _flight_with_envelope(server)
    heavy = _checkin_and_weigh(server, fid, "W0011", 30_000)
    light = _checkin_and_weigh(server, fid, "W0012", 10_000)
    call(
        server,
        "POST",
        "/api/load/plan",
        {"flight_id": fid, "batch": 1, "bag_ids": [heavy, light]},
    )
    status, csv_text = call(server, "GET", "/api/export/loadplan.csv")
    assert status == 200
    assert "pulled_off_for_swap" in csv_text
    assert "swapped_in" in csv_text
    assert "rolled_back" in csv_text
    status, weighing_csv = call(server, "GET", "/api/export/weighings.csv")
    assert status == 200
    assert weighing_csv.splitlines()[0] == "bag_id,weight_g,arm_cm,weighing_seq,at"


def test_policy_switch_and_envelope_reads(server):
    fid = _flight_with_envelope(server)
    status, payload = call(server, "POST", "/api/load/policy", {"policy": "reject_last"})
    assert status == 200 and payload["policy"] == "reject_last"
    status, payload = call(server, "GET", "/api/load/policy")
    assert payload["policy"] == "reject_last"
    status, envelope = call(server, "GET", f"/api/load/envelope/{fid}")
    assert envelope["deadload_cap_g"] == 88_000
    status, listing = call(server, "GET", "/api/load/envelopes")
    assert any(item["flight_id"] == fid for item in listing)


def test_load_routes_appear_in_catalog(server):
    status, catalog = call(server, "GET", "/api/catalog")
    paths = {item["path"] for item in catalog}
    assert "/api/load/plan" in paths
    assert "/api/export/loadplan.csv" in paths
    assert "/api/load/account/{flight}" in paths
