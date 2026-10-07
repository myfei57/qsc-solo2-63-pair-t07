"""Weight-and-balance load control: planner, refusals, swap, rollback, audit.

The constraint numbers below are hand-derived so the checks verify the physics
rather than repeating the implementation.
"""

from __future__ import annotations

import json

import pytest

from bagsort.app import Plant
from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, ValidationError
from bagsort.load import LoadLimits
from bagsort.load.constraints import compute_balance, feasible
from bagsort.load.export import export_bundle
from bagsort.load.planner import Candidate, plan as run_plan
from bagsort.store.store import Store


def limits(
    *,
    weight_cap: float = 200.0,
    cg_forward: float = 598.0,
    cg_aft: float = 602.0,
    lateral: float = 1e9,
    piece_cap: float = 50.0,
    oew: float = 1000.0,
    oew_arm: float = 600.0,
    positions=None,
) -> LoadLimits:
    positions = positions or (
        ("FWD-L", 500.0, "left", 2),
        ("FWD-R", 500.0, "right", 2),
        ("AFT-L", 700.0, "left", 2),
        ("AFT-R", 700.0, "right", 2),
    )
    return LoadLimits(
        weight_cap=weight_cap,
        cg_forward=cg_forward,
        cg_aft=cg_aft,
        lateral_moment_cap=lateral,
        piece_weight_cap=piece_cap,
        oew_weight=oew,
        oew_arm=oew_arm,
        positions=tuple(positions),
    )


@pytest.fixture()
def load_plant(plant, flight):
    plant.configure_load(flight.id, limits())
    return plant, flight


def _bag(plant, fid, code, weight: float | None = 30.0):
    bag = plant.check_in(code, fid)
    if weight is not None:
        result = plant.loadmaster.weigh(bag.id, weight)
        assert result.accepted
    return bag


def _load(plant, fid, bag_ids, batch: int = 1):
    plant.submit_load_batch(fid, bag_ids, batch=batch)
    return plant.confirm_load(fid)


# -- pure constraints -------------------------------------------------------


def test_balance_math_is_hand_checkable():
    """OEW 1000kg@600 plus 30kg at 500 => CG 615000/1030 = 597.087."""

    cfg = LoadLimits(
        weight_cap=200,
        cg_forward=0,
        cg_aft=2000,
        lateral_moment_cap=1e9,
        piece_weight_cap=50,
        oew_weight=1000,
        oew_arm=600,
        positions=(("FWD", 500.0, "center", 1),),
    )
    outcome = run_plan([], [Candidate("a", 30.0)], cfg)
    balance = compute_balance(outcome.entries, cfg)
    assert balance.cg_arm == pytest.approx(615000 / 1030, abs=1e-3)
    assert balance.total_weight == pytest.approx(1030.0)


def test_weight_and_cg_checked_together():
    """A set within the weight cap but outside the CG window is infeasible."""

    cfg = limits(cg_forward=599.0, cg_aft=601.0)
    outcome = run_plan([], [Candidate("a", 30.0), Candidate("b", 30.0)], cfg)
    assert feasible(outcome.entries, cfg)


# -- weighing ---------------------------------------------------------------


def test_overweight_piece_rejected_at_scale(load_plant):
    plant, flight = load_plant
    bag = plant.check_in("HEAVY", flight.id)
    result = plant.loadmaster.weigh(bag.id, 75.0)
    assert not result.accepted
    assert result.record.reason == "PIECE_OVERWEIGHT"
    assert plant.bag(bag.id).state.value == "load_rejected"
    again = plant.loadmaster.weigh(bag.id, 20.0)
    assert again.accepted
    assert plant.bag(bag.id).state.value == "weighed"


def test_non_positive_reading_logged_but_never_counted(load_plant):
    plant, flight = load_plant
    bag = plant.check_in("BAD", flight.id)
    result = plant.loadmaster.weigh(bag.id, 0)
    assert not result.accepted
    assert plant.loadmaster.accepted_weight(bag.id) is None


def test_missing_weighing_is_held_and_explained(plant, flight):
    plant.configure_load(
        flight.id,
        limits(cg_forward=0.0, cg_aft=2000.0, positions=(("X", 600.0, "center", 4),)),
    )
    unweighed = plant.check_in("NW", flight.id)
    weighed = _bag(plant, flight.id, "W", 30.0)
    draft = plant.submit_load_batch(
        flight.id, [unweighed.id, weighed.id], batch=1
    )
    refusal = next(r for r in draft.rejections if r.bag_id == unweighed.id)
    assert refusal.code == "MISSING_WEIGHING"
    assert refusal.evidence["held"] is True
    plant.confirm_load(flight.id)
    assert set(plant.loadmaster.ledger.counted_bags(flight.id)) == {weighed.id}


# -- sequencing and refusal reasons -----------------------------------------


def test_over_limit_refuses_last_piece_with_evidence(plant, flight):
    plant.configure_load(
        flight.id,
        limits(
            weight_cap=70.0,
            cg_forward=0.0,
            cg_aft=2000.0,
            positions=(("X", 500.0, "center", 3),),
        ),
    )
    bags = [_bag(plant, flight.id, f"B{i}", 30.0) for i in range(3)]
    draft = plant.submit_load_batch(flight.id, [b.id for b in bags])
    assert [e.bag_id for e in draft.entries] == [bags[0].id, bags[1].id]
    refusal = next(r for r in draft.rejections if r.code == "PLACEMENT_BREACH")
    assert refusal.bag_id == bags[2].id
    assert "越限" in refusal.reason
    codes = {b["code"] for b in refusal.evidence["breaches_with_piece"]}
    assert "WEIGHT_OVER_CAP" in codes
    plant.confirm_load(flight.id)
    assert plant.loadmaster.ledger.balance(flight.id) == pytest.approx(60.0)


def test_lateral_imbalance_refusal_reports_bound(load_plant):
    plant, flight = load_plant
    plant.configure_load(
        flight.id,
        limits(
            lateral=5000.0,
            cg_forward=0.0,
            cg_aft=2000.0,
            positions=(
                ("L", 600.0, "left", 1),
                ("R", 600.0, "right", 1),
            ),
        ),
    )
    left = _bag(plant, flight.id, "L", 30.0)
    right = _bag(plant, flight.id, "R", 10.0)
    draft = plant.submit_load_batch(flight.id, [left.id, right.id])
    plant.confirm_load(flight.id)
    codes = {r.code for r in plant.loadmaster.refusals(flight.id)}
    assert "PLACEMENT_BREACH" in codes
    # evidence shows the lateral moment observed against the cap
    refusal = next(r for r in plant.loadmaster.refusals(flight.id)
                   if r.code == "PLACEMENT_BREACH")
    breach_codes = {b["code"] for b in refusal.evidence["breaches_with_piece"]}
    assert "LATERAL_IMBALANCE" in breach_codes


def test_loading_sequence_is_contiguous(load_plant):
    plant, flight = load_plant
    bags = [_bag(plant, flight.id, f"S{i}", 30.0) for i in range(4)]
    _load(plant, flight.id, [b.id for b in bags])
    sequence = plant.loadmaster.sequence(flight.id)
    assert [entry.order for entry in sequence] == [0, 1, 2, 3]
    assert [entry.bag_id for entry in sequence] == [b.id for b in bags]


def test_same_bag_cannot_be_counted_in_two_batches(load_plant):
    plant, flight = load_plant
    shared = _bag(plant, flight.id, "SHARED", 30.0)
    other = _bag(plant, flight.id, "OTHER", 30.0)
    _load(plant, flight.id, [shared.id, other.id], batch=1)
    second = _bag(plant, flight.id, "SECOND", 20.0)
    draft = plant.submit_load_batch(flight.id, [shared.id, second.id], batch=2)
    assert [r.code for r in draft.rejections] == ["DUPLICATE_BATCH"]
    plant.confirm_load(flight.id)
    net = plant.loadmaster.ledger.net_by_bag(flight.id)
    assert net[shared.id] == pytest.approx(30.0)


# -- swap and rollback ------------------------------------------------------


def _narrow(plant, flight):
    plant.configure_load(
        flight.id,
        limits(
            positions=(
                ("FWD", 500.0, "center", 1),
                ("AFT", 700.0, "center", 1),
            )
        ),
    )


def test_swap_with_lighter_piece_reposts_weight(load_plant):
    plant, flight = load_plant
    _narrow(plant, flight)
    aft = _bag(plant, flight.id, "A", 30.0)
    fwd = _bag(plant, flight.id, "F", 30.0)
    _load(plant, flight.id, [aft.id, fwd.id])
    lighter = _bag(plant, flight.id, "L", 25.0)  # CG -> 599.526
    revision = plant.swap_load(flight.id, aft.id, lighter.id)
    assert revision.status == "confirmed"
    report = plant.loadmaster.loading_report(flight.id)
    assert report["feasible"]
    assert report["balance"]["cg_arm"] == pytest.approx(599.526, abs=1e-2)
    assert report["ledger_balance"] == pytest.approx(55.0)
    net = plant.loadmaster.ledger.net_by_bag(flight.id)
    assert net.get(aft.id) == 0.0
    assert net.get(lighter.id) == pytest.approx(25.0)
    assert plant.bag(aft.id).state.value == "weighed"
    assert plant.bag(lighter.id).state.value == "loaded"


def test_failed_swap_leaves_sequence_untouched_and_is_audited(load_plant):
    plant, flight = load_plant
    _narrow(plant, flight)
    aft = _bag(plant, flight.id, "A", 30.0)
    fwd = _bag(plant, flight.id, "F", 30.0)
    _load(plant, flight.id, [aft.id, fwd.id])
    light = _bag(plant, flight.id, "L", 5.0)  # CG 597.585 < 598
    before = [e.bag_id for e in plant.loadmaster.sequence(flight.id)]
    with pytest.raises(ConflictError):
        plant.swap_load(flight.id, aft.id, light.id)
    after = [e.bag_id for e in plant.loadmaster.sequence(flight.id)]
    assert before == after
    failed = [a for a in plant.audit.entries() if a.kind == "load_swap_failed"]
    assert len(failed) == 1
    assert "CG_TOO_FORWARD" in [b["code"] for b in failed[0].detail["breaches"]]
    assert plant.loadmaster.ledger.balance(flight.id) == pytest.approx(60.0)
    assert plant.bag(aft.id).state.value == "loaded"
    assert plant.bag(light.id).state.value == "weighed"
    assert plant.loadmaster.verify_flight(flight.id)["ok"]


def test_swap_requires_strictly_lighter_piece(load_plant):
    plant, flight = load_plant
    aft = _bag(plant, flight.id, "A", 30.0)
    heavy = _bag(plant, flight.id, "H", 30.0)
    _load(plant, flight.id, [aft.id])
    with pytest.raises(ConflictError):
        plant.swap_load(flight.id, aft.id, heavy.id)


def test_rollback_restores_weight_and_cg_consistency(load_plant):
    plant, flight = load_plant
    _narrow(plant, flight)
    aft = _bag(plant, flight.id, "A", 30.0)
    fwd = _bag(plant, flight.id, "F", 30.0)
    v1 = _load(plant, flight.id, [aft.id, fwd.id]).version
    lighter = _bag(plant, flight.id, "L", 25.0)
    plant.swap_load(flight.id, aft.id, lighter.id)
    assert plant.loadmaster.ledger.balance(flight.id) == pytest.approx(55.0)
    plant.rollback_load(flight.id, v1)
    report = plant.loadmaster.loading_report(flight.id)
    assert report["balance"]["cg_arm"] == pytest.approx(600.0, abs=1e-2)
    assert report["ledger_balance"] == pytest.approx(60.0)
    assert plant.bag(aft.id).state.value == "loaded"
    assert plant.bag(lighter.id).state.value == "weighed"
    checks = plant.loadmaster.verify_flight(flight.id)
    assert checks["within_weight_and_cg"]
    assert checks["weight_ledger_matches_plan"]


def test_rollback_rejects_non_ancestor_target(load_plant):
    plant, flight = load_plant
    a = _bag(plant, flight.id, "A", 20.0)
    b = _bag(plant, flight.id, "B", 20.0)
    _load(plant, flight.id, [a.id])
    _load(plant, flight.id, [b.id])
    with pytest.raises(ConflictError):
        plant.rollback_load(flight.id, 999)


# -- audit, export, replay --------------------------------------------------


def test_refusals_audited_and_exported_with_digest_anchor(plant, flight):
    plant.configure_load(
        flight.id,
        limits(
            weight_cap=40.0,
            cg_forward=0.0,
            cg_aft=2000.0,
            positions=(("X", 500.0, "center", 2),),
        ),
    )
    bags = [_bag(plant, flight.id, "A", 30.0), _bag(plant, flight.id, "B", 30.0)]
    plant.submit_load_batch(flight.id, [b.id for b in bags])
    plant.confirm_load(flight.id)
    refusals = plant.loadmaster.refusals(flight.id)
    assert refusals
    for refusal in refusals:
        assert refusal.audit_sequence > 0
        assert len(refusal.audit_digest) == 64
    assert plant.audit.verify()
    bundle = export_bundle(plant.loadmaster, flight.id)
    assert refusals[0].audit_digest in bundle["refusals.csv"]
    assert '"ok": true' in bundle["manifest.json"]
    assert "load" in bundle["ledger.csv"]


def test_replay_rebuilds_identical_ledgers(load_plant):
    plant, flight = load_plant
    bags = [_bag(plant, flight.id, f"R{i}", 30.0) for i in range(3)]
    _load(plant, flight.id, [b.id for b in bags])
    plant.store.persist()
    rebuilt = Plant.build(Store.open(plant.store.path), clock=Clock())
    assert rebuilt.audit.verify()
    before = plant.loadmaster.loading_report(flight.id)
    after = rebuilt.loadmaster.loading_report(flight.id)
    assert [e["bag_id"] for e in before["sequence"]] == [
        e["bag_id"] for e in after["sequence"]
    ]
    assert before["ledger_balance"] == after["ledger_balance"]
    assert rebuilt.loadmaster.verify_flight(flight.id)["ok"]


def test_verify_reports_ok_on_a_clean_flight(load_plant):
    plant, flight = load_plant
    bag = _bag(plant, flight.id, "V", 30.0)
    _load(plant, flight.id, [bag.id])
    assert plant.loadmaster.verify_flight(flight.id)["ok"]


def test_configuration_is_validated(load_plant):
    plant, flight = load_plant
    with pytest.raises(ValidationError):
        plant.configure_load(flight.id, limits(cg_forward=700.0, cg_aft=500.0))
    with pytest.raises(NotFoundError):
        plant.loadmaster.limits("T1-NOPE")


# -- HTTP console ------------------------------------------------------------


def _call(server, method, path, payload=None):
    response = server.call(method, path, payload)
    if response.content_type.startswith("application/json"):
        return response.status, json.loads(response.body)
    return response.status, response.body.decode("utf-8")


def test_full_flow_and_export_bundle_over_http(server):
    status, flight = _call(
        server, "POST", "/api/flights",
        {"code": "CA1234", "carousel": "CR1"},
    )
    assert status == 201
    fid = flight["id"]
    cfg = limits(
        weight_cap=40.0,
        cg_forward=0.0,
        cg_aft=2000.0,
        positions=(("X", 500.0, "center", 2),),
    ).as_dict()
    assert _call(server, "POST", f"/api/load/{fid}/limits", cfg)[0] == 201

    bag_ids = []
    for code, weight in (("A", 30.0), ("B", 30.0)):
        _, bag = _call(server, "POST", "/api/bags/checkin",
                       {"barcode": code, "flight_id": fid})
        _, weighed = _call(server, "POST", "/api/load/weigh",
                           {"bag_id": bag["id"], "weight": weight})
        assert weighed["accepted"]
        bag_ids.append(bag["id"])

    status, draft = _call(
        server, "POST", f"/api/load/{fid}/batches",
        {"bag_ids": bag_ids, "batch": 1},
    )
    assert status == 201 and len(draft["entries"]) == 1
    assert _call(server, "POST", f"/api/load/{fid}/confirm", {})[0] == 200

    _, report = _call(server, "GET", f"/api/load/{fid}/report")
    assert report["feasible"] and len(report["sequence"]) == 1
    assert report["ledger_balance"] == pytest.approx(30.0)
    _, verification = _call(server, "GET", f"/api/load/{fid}/verify")
    assert verification["ok"]

    _, refusals = _call(server, "GET", f"/api/load/{fid}/refusals")
    digest = refusals[0]["audit_digest"]
    _, bundle = _call(server, "GET", f"/api/export/load/{fid}/bundle.json")
    assert digest in bundle["refusals.csv"]
    assert '"ok": true' in bundle["manifest.json"]
    assert "load" in bundle["ledger.csv"]


def test_swap_over_http_rolls_back_on_infeasibility(server):
    status, flight = _call(
        server, "POST", "/api/flights",
        {"code": "CA7777", "carousel": "CR1"},
    )
    fid = flight["id"]
    cfg = limits(
        positions=(("FWD", 500.0, "center", 1), ("AFT", 700.0, "center", 1))
    ).as_dict()
    _call(server, "POST", f"/api/load/{fid}/limits", cfg)

    def weigh(code, weight):
        _, bag = _call(server, "POST", "/api/bags/checkin",
                       {"barcode": code, "flight_id": fid})
        _call(server, "POST", "/api/load/weigh",
              {"bag_id": bag["id"], "weight": weight})
        return bag["id"]

    aft, fwd = weigh("A", 30.0), weigh("F", 30.0)
    _call(server, "POST", f"/api/load/{fid}/batches",
          {"bag_ids": [aft, fwd], "batch": 1})
    _call(server, "POST", f"/api/load/{fid}/confirm", {})
    light = weigh("L", 5.0)
    status, error = _call(server, "POST", f"/api/load/{fid}/swap",
                          {"out_bag_id": aft, "in_bag_id": light})
    assert status == 409
    _, sequence = _call(server, "GET", f"/api/load/{fid}/sequence")
    assert [entry["bag_id"] for entry in sequence] == [aft, fwd]
