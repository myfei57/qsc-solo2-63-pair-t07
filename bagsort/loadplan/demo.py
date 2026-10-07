"""载重配载的确定性演示场景。

不读墙钟、网络与随机源：固定一架次包线、固定一批行李的秤量与站位，逐件提交
给配载器，覆盖"确认—越限—换装回滚—缺秤拒绝"全链路，返回可断言的汇总。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bagsort.app import Plant


def run_load_demo(plant: "Plant") -> dict[str, object]:
    """构造一架次并排出装载顺序，触发一次较轻件换装。"""

    flight = plant.register_flight("LP7777", "CR1")
    plant.clock.advance(10)
    # 基础业载 50kg@100cm；货舱上限 88kg；重心窗口放宽到只看载重。
    plant.set_envelope(
        flight.id,
        deadload_cap_g=88_000,
        cg_min_cm=0.0,
        cg_max_cm=200.0,
        base_weight_g=50_000,
        base_moment_gcm=5_000_000,
        piece_max_g=40_000,
    )
    bags = [plant.check_in(f"LP{i:04d}", flight.id) for i in range(4)]
    heavy, trigger, tail, unweighed = bags
    plant.weigh_bag(heavy.id, 30_000, 100)
    plant.clock.advance(2)
    plant.weigh_bag(trigger.id, 10_000, 100)
    plant.weigh_bag(tail.id, 8_000, 100)
    # unweighed 故意不过秤，验证缺秤件的明确处置
    plant.clock.advance(3)

    result = plant.plan_loading(
        flight.id, [heavy.id, trigger.id, tail.id], batch=1
    )
    missing = plant.submit_loading(unweighed.id, flight.id, batch=1)
    account = plant.load.account(flight.id)
    summary: dict[str, object] = {
        "flight_id": flight.id,
        "policy": result.policy,
        "order": result.order,
        "weight_g": result.weight_g,
        "headroom_g": result.headroom_g,
        "rejected": result.rejected,
        "pulled_off": result.pulled_off,
        "missing_weight": missing.reason,
        "account_entries": len(account["entries"]),
        "account_balance_g": account["balance_g"],
        "audit_intact": plant.audit.verify(),
        "watermark": plant.store.watermark,
    }
    return summary


def load_demo_invariants_hold(summary: dict[str, object]) -> bool:
    return bool(
        summary["audit_intact"]
        and summary["policy"] == "swap_lighter"
        and len(summary["pulled_off"]) == 1
        and summary["missing_weight"] == "missing_weight"
        and summary["account_balance_g"] == 68_000
        and summary["headroom_g"] == 20_000
    )
