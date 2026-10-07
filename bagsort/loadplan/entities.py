"""配载子系统的只增记录及其 JSON 编解码。

三张记录表对应三条"逐笔可核"的账：

* :class:`WeighingRecord` —— 称重记录：每件行李过秤一笔，缺秤即无记录；
* :class:`EnvelopeConfig` —— 额度账：每航班一个载重上限与重心窗口版本；
* :class:`LoadDecision` —— 转盘装载：每提交一件给一笔结论（确认/拒绝/换装）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .enums import DecisionKind, RejectReason


def _clean(raw: Any) -> Any:
    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class WeighingRecord:
    """一件行李的一次过秤结果。补秤会再写一条，旧记录保留不删。"""

    bag_id: str
    weight_g: int
    arm_cm: int
    at: str
    weighing_seq: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "weight_g": self.weight_g,
            "arm_cm": self.arm_cm,
            "at": self.at,
            "weighing_seq": self.weighing_seq,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WeighingRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            weight_g=int(raw["weight_g"]),
            arm_cm=int(raw.get("arm_cm", 0)),
            at=str(raw.get("at", "")),
            weighing_seq=int(raw.get("weighing_seq", 0)),
        )


@dataclass(frozen=True)
class EnvelopeConfig:
    """一个航班的载重额度与重心窗口（带版本号，改配即升版）。"""

    flight_id: str
    deadload_cap_g: int
    cg_min_cm: float
    cg_max_cm: float
    base_weight_g: int
    base_moment_gcm: int
    piece_max_g: int
    version: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "deadload_cap_g": self.deadload_cap_g,
            "cg_min_cm": self.cg_min_cm,
            "cg_max_cm": self.cg_max_cm,
            "base_weight_g": self.base_weight_g,
            "base_moment_gcm": self.base_moment_gcm,
            "piece_max_g": self.piece_max_g,
            "version": self.version,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EnvelopeConfig":
        return cls(
            flight_id=str(raw["flight_id"]),
            deadload_cap_g=int(raw["deadload_cap_g"]),
            cg_min_cm=float(raw["cg_min_cm"]),
            cg_max_cm=float(raw["cg_max_cm"]),
            base_weight_g=int(raw.get("base_weight_g", 0)),
            base_moment_gcm=int(raw.get("base_moment_gcm", 0)),
            piece_max_g=int(raw.get("piece_max_g", 0)),
            version=int(raw.get("version", 1)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class LoadDecision:
    """一件行李在一个批次上的配载结论。

    ``seq`` 是本航班结论账的序号，``position`` 是确认后在装载顺序中的位序
    （0 起），被拒绝或撤下时为 -1。``balance_weight_g`` / ``balance_cg_cm``
    记录该笔结论落定后整舱的合计值，逐笔核对额度账时用。
    """

    seq: int
    flight_id: str
    batch: int
    bag_id: str
    kind: DecisionKind
    reason: RejectReason | None
    weight_g: int
    arm_cm: int
    position: int
    policy: str
    detail: dict[str, Any] = field(default_factory=dict)
    swapped_for: str = ""
    balance_weight_g: int = 0
    balance_cg_cm: float = 0.0
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "flight_id": self.flight_id,
            "batch": self.batch,
            "bag_id": self.bag_id,
            "kind": self.kind.value,
            "reason": self.reason.value if self.reason else "",
            "weight_g": self.weight_g,
            "arm_cm": self.arm_cm,
            "position": self.position,
            "policy": self.policy,
            "detail": _clean(self.detail),
            "swapped_for": self.swapped_for,
            "balance_weight_g": self.balance_weight_g,
            "balance_cg_cm": self.balance_cg_cm,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadDecision":
        reason_value = str(raw.get("reason", ""))
        return cls(
            seq=int(raw["seq"]),
            flight_id=str(raw["flight_id"]),
            batch=int(raw.get("batch", 0)),
            bag_id=str(raw["bag_id"]),
            kind=DecisionKind(str(raw["kind"])),
            reason=RejectReason(reason_value) if reason_value else None,
            weight_g=int(raw.get("weight_g", 0)),
            arm_cm=int(raw.get("arm_cm", 0)),
            position=int(raw.get("position", -1)),
            policy=str(raw.get("policy", "")),
            detail=_clean(raw.get("detail", {})),
            swapped_for=str(raw.get("swapped_for", "")),
            balance_weight_g=int(raw.get("balance_weight_g", 0)),
            balance_cg_cm=float(raw.get("balance_cg_cm", 0.0)),
            at=str(raw.get("at", "")),
        )
