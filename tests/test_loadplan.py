"""载重与重心配载：称重、额度账、装载顺序、越限拒绝与换装回滚。"""

from __future__ import annotations

import pytest

from bagsort.loadplan.enums import (
    DecisionKind,
    OverlimitPolicy,
    RejectReason,
)
from bagsort.loadplan.physics import (
    LoadConstraints,
    PlacedPiece,
    evaluate,
    prefixes_hold,
    trial,
)
from bagsort.loadplan.policy import Candidate, plan_batch
from bagsort.store.store import Store


def _envelope(
    plant,
    flight,
    cap=100_000,
    cg_min=0.0,
    cg_max=200.0,
    base_weight=0,
    base_moment=0,
    piece_max=0,
):
    return plant.set_envelope(
        flight.id,
        deadload_cap_g=cap,
        cg_min_cm=cg_min,
        cg_max_cm=cg_max,
        base_weight_g=base_weight,
        base_moment_gcm=base_moment,
        piece_max_g=piece_max,
    )


def _weigh(plant, bags, weights, arms=None):
    arms = arms or [0] * len(bags)
    for bag, weight, arm in zip(bags, weights, arms):
        plant.weigh_bag(bag.id, weight, arm)


# ---------------------------------------------------------------------------
# 纯物理层
# ---------------------------------------------------------------------------


def test_weight_and_center_of_gravity_math():
    constraints = LoadConstraints(
        deadload_cap_g=100_000,
        cg_min_cm=90.0,
        cg_max_cm=110.0,
        base_weight_g=50_000,
        base_moment_gcm=5_000_000,
    )
    result = evaluate([PlacedPiece("a", 30_000, 120)], constraints)
    # (5,000,000 + 3,600,000) / 80,000 = 107.5
    assert result.feasible is True
    assert result.cg_cm == pytest.approx(107.5)
    assert result.headroom_g == 20_000


def test_deadload_cap_is_enforced():
    constraints = LoadConstraints(deadload_cap_g=80_000, cg_min_cm=-999, cg_max_cm=999)
    result = trial(
        [PlacedPiece("a", 60_000, 0)], PlacedPiece("b", 25_000, 0), constraints
    )
    assert result.feasible is False
    assert result.reason == "deadload_exceeded"


def test_center_of_gravity_window_is_enforced():
    constraints = LoadConstraints(deadload_cap_g=10**9, cg_min_cm=90, cg_max_cm=110)
    result = evaluate([PlacedPiece("a", 100_000, 130)], constraints)
    assert result.feasible is False
    assert result.reason == "cg_out_of_window"


def test_single_piece_overweight_is_rejected_before_totals():
    constraints = LoadConstraints(
        deadload_cap_g=10**9, cg_min_cm=-999, cg_max_cm=999, piece_max_g=32_000
    )
    result = trial([], PlacedPiece("a", 40_000, 0), constraints)
    assert result.feasible is False
    assert result.reason == "piece_overweight"


def test_every_prefix_must_hold():
    constraints = LoadConstraints(deadload_cap_g=100, cg_min_cm=-999, cg_max_cm=999)
    order = [PlacedPiece("a", 60, 0), PlacedPiece("b", 60, 0)]
    assert prefixes_hold(order[:1], constraints) is True
    assert prefixes_hold(order, constraints) is False


def test_constraints_reject_invalid_envelope():
    with pytest.raises(ValueError):
        LoadConstraints(deadload_cap_g=-1, cg_min_cm=0, cg_max_cm=1)
    with pytest.raises(ValueError):
        LoadConstraints(deadload_cap_g=10, cg_min_cm=100, cg_max_cm=0)
    with pytest.raises(ValueError):
        PlacedPiece("a", 0, 0)


# ---------------------------------------------------------------------------
# 策略：拒最后一件 vs 较轻件换装
# ---------------------------------------------------------------------------


def _candidate(bag, weight, arm=0):
    return Candidate(bag.id, weight, arm)


def test_reject_last_policy_keeps_confirmed_order():
    plant_constraints = LoadConstraints(
        deadload_cap_g=80, cg_min_cm=-999, cg_max_cm=999
    )
    plan = plan_batch(
        "f",
        1,
        [Candidate("a", 60), Candidate("b", 30), Candidate("c", 10)],
        plant_constraints,
        policy=OverlimitPolicy.REJECT_LAST,
    )
    kinds = [(step.bag_id, step.kind.value) for step in plan.steps]
    assert kinds == [("a", "confirmed"), ("b", "rejected"), ("c", "confirmed")]
    assert [piece.bag_id for piece in plan.order] == ["a", "c"]


def test_swap_lighter_rolls_back_confirmed_piece():
    # 基础业载 50：H=30 -> 80 可行；X=10 -> 90 越限（上限88）；
    # 回滚更重的 H、X 换入 -> 60；T=8 -> 68。
    constraints = LoadConstraints(
        deadload_cap_g=88, cg_min_cm=-999, cg_max_cm=999, base_weight_g=50
    )
    plan = plan_batch(
        "f",
        1,
        [Candidate("H", 30), Candidate("X", 10), Candidate("T", 8)],
        constraints,
        policy=OverlimitPolicy.SWAP_LIGHTER,
    )
    kinds = [step.kind for step in plan.steps]
    assert DecisionKind.ROLLED_BACK in kinds
    assert DecisionKind.PULLED_OFF in kinds
    assert DecisionKind.SWAPPED_IN in kinds
    swap = next(step for step in plan.steps if step.kind is DecisionKind.SWAPPED_IN)
    assert swap.bag_id == "X" and swap.swapped_for == "H"
    # 回滚后的最终顺序载重与上限同时成立
    assert prefixes_hold(plan.order, constraints)
    assert [piece.bag_id for piece in plan.order] == ["X", "T"]


def test_swap_prefers_lightest_victim_and_writes_gained_headroom():
    # 基础 30：A=40、B=30 已确认（70+30=100），X=25 会到 125 超上限 124；
    # 回滚更轻、位序更靠后的 B（-30+25）-> 95，可行；回滚 A 则 -> 85。
    constraints = LoadConstraints(
        deadload_cap_g=124, cg_min_cm=-999, cg_max_cm=999, base_weight_g=30
    )
    plan = plan_batch(
        "f", 1, [Candidate("A", 40), Candidate("B", 30), Candidate("X", 25)],
        constraints, policy=OverlimitPolicy.SWAP_LIGHTER,
    )
    swap = next(step for step in plan.steps if step.kind is DecisionKind.SWAPPED_IN)
    # 应回滚更轻、更靠后的 B（撤件影响最小），而不是更重的 A
    assert swap.swapped_for == "B"
    assert swap.detail["gained_headroom_g"] == 5
    assert prefixes_hold(plan.order, constraints)


def test_swap_falls_back_to_reject_when_no_heavier_confirmed_piece():
    # 基础 60：a=30 -> 90 可行；b=50 -> 140 越限，且没有比 b 更重的确认件可换。
    constraints = LoadConstraints(
        deadload_cap_g=100, cg_min_cm=-999, cg_max_cm=999, base_weight_g=60
    )
    plan = plan_batch(
        "f", 1, [Candidate("a", 30), Candidate("b", 50)],
        constraints, policy=OverlimitPolicy.SWAP_LIGHTER,
    )
    rejection = next(step for step in plan.steps if step.kind is DecisionKind.REJECTED)
    assert rejection.reason is RejectReason.NO_FEASIBLE_SWAP
    assert rejection.detail["swap_attempted"] is True


def test_single_piece_overweight_never_triggers_swap():
    constraints = LoadConstraints(
        deadload_cap_g=10**9, cg_min_cm=-999, cg_max_cm=999, piece_max_g=32_000
    )
    plan = plan_batch(
        "f", 1, [Candidate("a", 10_000), Candidate("big", 40_000)],
        constraints, policy=OverlimitPolicy.SWAP_LIGHTER,
    )
    rejection = next(step for step in plan.steps if step.kind is DecisionKind.REJECTED)
    assert rejection.reason is RejectReason.PIECE_OVERWEIGHT
    assert all(step.kind is not DecisionKind.ROLLED_BACK for step in plan.steps)


# ---------------------------------------------------------------------------
# 称重与额度账
# ---------------------------------------------------------------------------


def test_weighing_is_recorded_and_re_weighing_supersedes(plant, flight):
    _envelope(plant, flight)
    bag = plant.check_in("WB1", flight.id)
    first = plant.weigh_bag(bag.id, 12_000, 50)
    second = plant.weigh_bag(bag.id, 14_500, 50)
    current = plant.load.ledger.weighing(bag.id)
    assert current.weighing_seq == second.weighing_seq == first.weighing_seq + 1
    assert current.weight_g == 14_500
    # 称重计数器只随记录数增长
    assert plant.store.state().weighing_counter == 2


def test_weighing_rejects_non_positive_weight(plant, flight):
    bag = plant.check_in("WB2", flight.id)
    with pytest.raises(Exception):
        plant.weigh_bag(bag.id, 0, 50)


def test_envelope_is_versioned(plant, flight):
    first = _envelope(plant, flight, cap=100_000)
    second = _envelope(plant, flight, cap=90_000)
    assert first.version == 1 and second.version == 2
    assert plant.load.ledger.envelope(flight.id).deadload_cap_g == 90_000


# ---------------------------------------------------------------------------
# 编排器：入场闸、拒绝理由、逐笔账与审计
# ---------------------------------------------------------------------------


def test_missing_weight_is_rejected_with_clear_reason_and_can_be_resubmitted(plant, flight):
    _envelope(plant, flight, cap=10**9, cg_min=-999, cg_max=999)
    bag = plant.check_in("NW1", flight.id)
    decision = plant.submit_loading(bag.id, flight.id, 1)
    assert decision.accepted is False
    assert decision.reason == RejectReason.MISSING_WEIGHT.value
    assert "缺称重记录" in decision.reason_text
    # 缺秤不是终态：补秤后重新提交可确认
    plant.weigh_bag(bag.id, 10_000, 0)
    again = plant.submit_loading(bag.id, flight.id, 1)
    assert again.accepted is True
    assert again.kind == DecisionKind.CONFIRMED.value


def test_terminal_rejection_cannot_be_resubmitted(plant, flight):
    _envelope(plant, flight, cap=80_000, cg_min=-999, cg_max=999, piece_max=32_000)
    bag = plant.check_in("OW1", flight.id)
    plant.weigh_bag(bag.id, 40_000, 0)
    first = plant.submit_loading(bag.id, flight.id, 1)
    assert first.reason == RejectReason.PIECE_OVERWEIGHT.value
    second = plant.submit_loading(bag.id, flight.id, 1)
    assert second.accepted is False
    assert second.reason == RejectReason.ALREADY_COUNTED.value


def test_same_bag_cannot_count_in_two_batches(plant, flight):
    _envelope(plant, flight, cap=10**9, cg_min=-999, cg_max=999)
    bag = plant.check_in("DB1", flight.id)
    plant.weigh_bag(bag.id, 10_000, 0)
    assert plant.submit_loading(bag.id, flight.id, 1).accepted is True
    second = plant.submit_loading(bag.id, flight.id, 2)
    assert second.accepted is False
    assert second.reason == RejectReason.ALREADY_COUNTED.value
    # 载重账上该件只计一次
    assert plant.load.account(flight.id)["balance_g"] == 10_000


def test_batch_plan_writes_order_decisions_and_audit_chain(plant, flight):
    _envelope(
        plant, flight, cap=100_000, cg_min=90, cg_max=110,
        base_weight=50_000, base_moment=5_000_000,
    )
    bags = [plant.check_in(f"PB{i}", flight.id) for i in range(2)]
    _weigh(plant, bags, [20_000, 25_000], [100, 100])
    result = plant.plan_loading(flight.id, [bag.id for bag in bags], batch=1)
    assert result.weight_g == 95_000
    assert result.order == [bag.id for bag in bags]
    assert result.policy == OverlimitPolicy.SWAP_LIGHTER.value
    assert plant.audit.verify() is True
    # 越限/拒绝/确认结论都进了审计留痕
    kinds = plant.audit.by_kind()
    assert kinds.get("loadplan_confirmed") == 2


def test_rejection_reasons_flow_into_export_rows(plant, flight):
    from bagsort.reporting import csv_export

    _envelope(plant, flight, cap=80_000, cg_min=-999, cg_max=999, piece_max=32_000)
    heavy = plant.check_in("HV1", flight.id)
    plant.weigh_bag(heavy.id, 40_000, 0)
    plant.submit_loading(heavy.id, flight.id, 1)
    rows = csv_export.loadplan_rows(plant.load.ledger.decisions(flight.id))
    assert rows[0]["reason"] == RejectReason.PIECE_OVERWEIGHT.value
    text = csv_export.render(
        csv_export.LOADPLAN_COLUMNS, csv_export.loadplan_rows(
            plant.load.ledger.decisions(flight.id)
        )
    )
    assert "piece_overweight" in text


def test_swap_rollback_keeps_weight_account_and_cg_window_consistent(plant, flight):
    envelope = _envelope(
        plant, flight, cap=88_000, cg_min=0, cg_max=200,
        base_weight=50_000, base_moment=5_000_000, piece_max=40_000,
    )
    bags = [plant.check_in(f"SW{i}", flight.id) for i in range(3)]
    _weigh(plant, bags, [30_000, 10_000, 8_000], [100, 100, 100])
    result = plant.plan_loading(flight.id, [b.id for b in bags], batch=1)
    heavy, light, tail = bags
    assert result.pulled_off == [heavy.id]
    assert result.order == [light.id, tail.id]
    # 回滚后载重上限与重心窗口同时成立
    assert result.weight_g == 68_000
    assert envelope.cg_min_cm <= result.cg_cm <= envelope.cg_max_cm
    assert result.headroom_g == 20_000
    account = plant.load.account(flight.id)
    # 余额逐笔守恒：最终余额 = 基础 + 在账两件
    assert account["balance_g"] == 68_000
    deltas = [row["delta_g"] for row in account["entries"]]
    assert sum(deltas) == 18_000  # +30 -30 +10 +8
    # 重放装载顺序与账面一致，且逐前缀可行
    from bagsort.loadplan.physics import LoadConstraints

    constraints = plant.load._to_constraints(envelope)
    pieces = []
    records = plant.load.ledger.weighings()
    by_id = {record.bag_id: record for record in records}
    for bag_id in plant.load.loading_order(flight.id):
        record = by_id[bag_id]
        pieces.append(PlacedPiece(bag_id, record.weight_g, record.arm_cm))
    assert prefixes_hold(pieces, constraints)
    assert plant.audit.verify() is True


def test_closed_flight_or_missing_envelope_rejects_batch(plant, flight):
    bag = plant.check_in("CF1", flight.id)
    plant.weigh_bag(bag.id, 10_000, 0)
    # 未设置包线
    result = plant.plan_loading(flight.id, [bag.id], batch=1)
    assert result.rejected == [bag.id]
    assert result.decisions[0].reason == RejectReason.FLIGHT_NOT_PLANNABLE.value
    # 关闭航班
    _envelope(plant, flight, cap=10**9, cg_min=-999, cg_max=999)
    plant.close_flight(flight.id)
    decision = plant.submit_loading(bag.id, flight.id, 1)
    assert decision.reason == RejectReason.FLIGHT_NOT_PLANNABLE.value


def test_weight_account_survives_restart(plant, flight):
    _envelope(plant, flight, cap=10**9, cg_min=-999, cg_max=999)
    bag = plant.check_in("PV1", flight.id)
    plant.weigh_bag(bag.id, 12_345, 70)
    plant.submit_loading(bag.id, flight.id, 1)
    plant.store.persist()
    from bagsort.app import Plant

    reopened = Store.open(plant.store.path)
    rebuilt = Plant.build(reopened, clock=plant.clock)
    assert rebuilt.load.account(flight.id)["balance_g"] == 12_345
    assert rebuilt.load.loading_order(flight.id) == [bag.id]
    assert rebuilt.audit.verify() is True


def test_policy_can_be_switched(plant, flight):
    _envelope(plant, flight, cap=80_000, cg_min=-999, cg_max=999)
    plant.load.set_policy(OverlimitPolicy.REJECT_LAST)
    bags = [plant.check_in(f"PL{i}", flight.id) for i in range(2)]
    _weigh(plant, bags, [60_000, 30_000])
    result = plant.plan_loading(flight.id, [b.id for b in bags], batch=1)
    assert result.policy == OverlimitPolicy.REJECT_LAST.value
    assert result.pulled_off == []
    assert result.rejected == [bags[1].id]


def test_pulled_off_piece_is_not_silently_recounted_on_same_flight(plant, flight):
    _envelope(
        plant, flight, cap=88_000, cg_min=0, cg_max=200,
        base_weight=50_000, base_moment=5_000_000, piece_max=40_000,
    )
    bags = [plant.check_in(f"RC{i}", flight.id) for i in range(2)]
    _weigh(plant, bags, [30_000, 10_000], [100, 100])
    result = plant.plan_loading(flight.id, [b.id for b in bags], batch=1)
    heavy = bags[0]
    assert result.pulled_off == [heavy.id]
    again = plant.submit_loading(heavy.id, flight.id, batch=2)
    assert again.accepted is False
    assert again.reason == RejectReason.PULLED_OFF_RECOUNT.value
    assert "不得在本航班自动重投" in again.reason_text
    # 载重账不被第二次计入
    assert plant.load.account(flight.id)["balance_g"] == 60_000


def test_rollback_resumed_piece_stays_on_the_weight_account(plant, flight):
    # 槽位后面的已确认件撤板后原位恢复：仍是在账件，不重复、不漏计。
    _envelope(
        plant, flight, cap=120_000, cg_min=0, cg_max=200,
        base_weight=30_000, base_moment=3_000_000, piece_max=40_000,
    )
    bags = [plant.check_in(f"RS{i}", flight.id) for i in range(3)]
    # A=40,B=30 已确认（30+40+30=100）；X=25 -> 125 超上限120，回滚更轻的 B。
    _weigh(plant, bags, [40_000, 30_000, 25_000], [100, 100, 100])
    result = plant.plan_loading(flight.id, [b.id for b in bags], batch=1)
    # B 被撤下，X 换入，A 始终在账
    assert result.pulled_off == [bags[1].id]
    assert set(result.order) == {bags[0].id, bags[2].id}
    assert plant.load.account(flight.id)["balance_g"] == 95_000
    # 撤板注解里 A 不在回滚集合（只回滚 B 槽位及以后，A 在 B 之前）
    rollback_bags = {
        d.bag_id
        for d in plant.load.ledger.decisions(flight.id)
        if d.kind == DecisionKind.ROLLED_BACK
    }
    assert bags[0].id not in rollback_bags
