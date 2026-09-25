"""申诉复核流转的领域模型、法定事由词表与输入校验。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .errors import ValidationFailed

# 法定申诉事由:同一事由的重复提交会被合并到在办案件
LEGAL_GROUNDS = {
    "fact_dispute": "事实认定异议",
    "procedure_violation": "程序违法",
    "law_misapplication": "适用法律错误",
    "evidence_insufficient": "证据不足",
    "disproportionate_punishment": "量罚明显不当",
}

# 申诉状态机:registered → correcting → registered → accepted → decided
# 终止态:rejected(不予受理/逾期不补正)、withdrawn(撤回)、decided(已复核决定)
OPEN_STATES = ("registered", "correcting", "accepted")

# 受理审查结论
ACCEPTANCE_CONCLUSIONS = ("accept", "reject")
# 复核决定结论:驳回申诉维持原决定 / 变更 / 撤销
RECONSIDERATION_CONCLUSIONS = ("uphold", "modify", "revoke")

DELIVERY_METHODS = ("electronic", "postal", "in_person")


def require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationFailed(f"{field} 不能为空")
    return text


def parse_money(value: Any, field: str = "金额") -> str:
    """金额以分为单位的非负整数,统一存为文本避免浮点误差。"""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed(f"{field} 必须是数字") from exc
    if amount < 0 or amount != amount.to_integral_value():
        raise ValidationFailed(f"{field} 必须是非负整数(分)")
    return str(int(amount))


@dataclass(frozen=True)
class EvidenceRef:
    """被引用的证据及其版本。"""

    evidence_id: str
    evidence_version: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvidenceRef":
        return cls(
            require_text(raw.get("evidence_id"), "证据编号"),
            require_text(raw.get("evidence_version"), "证据版本"),
        )


@dataclass(frozen=True)
class MaterialInput:
    """申诉或补正提交的材料。"""

    kind: str
    content: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MaterialInput":
        return cls(
            require_text(raw.get("kind"), "材料类型"),
            require_text(raw.get("content"), "材料内容"),
        )


def parse_materials(raw_items: Any) -> list[MaterialInput]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, (list, tuple)):
        raise ValidationFailed("材料必须是数组")
    return [MaterialInput.from_dict(item) for item in raw_items]


def parse_evidence_refs(raw_items: Any, field: str) -> list[EvidenceRef]:
    if not isinstance(raw_items, (list, tuple)):
        raise ValidationFailed(f"{field} 必须是数组")
    return [EvidenceRef.from_dict(item) for item in raw_items]
