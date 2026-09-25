"""处罚申诉领域的输入校验与常量。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# 法定申诉（行政复议）事由
LEGAL_GROUNDS = frozenset({
    "fact_unclear",        # 主要事实不清、证据不足
    "wrong_basis",         # 适用依据错误
    "procedure_violated",  # 违反法定程序
    "overreach",           # 超越或滥用职权
    "punishment_unfair",   # 处罚明显不当
})

ACTION_TYPES = frozenset({
    "payment_demand", "license_suspension", "demerit_points", "detention",
})

SERVICE_METHODS = frozenset({"direct", "mail", "electronic", "announcement"})
OVERDUE_STAGES = frozenset({"acceptance", "correction", "review"})


def required_text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def optional_text(value: object, field: str, maximum: int = 512) -> str | None:
    if value is None:
        return None
    return required_text(value, field, maximum)


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def sha256_text(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not HEX64.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制 SHA-256")
    return result


def legal_ground(value: object) -> str:
    result = required_text(value, "legal_ground_code", 48)
    if result not in LEGAL_GROUNDS:
        raise ValidationFailed("legal_ground_code 不是受支持的法定事由")
    return result


def material_list(raw: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, list) or not raw:
        raise ValidationFailed("申请材料至少包含一项")
    materials: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"第 {index + 1} 项材料必须是对象")
        materials.append({
            "material_id": identifier(item.get("material_id"), f"materials[{index}].material_id"),
            "title": required_text(item.get("title"), f"materials[{index}].title", 200),
            "kind": required_text(item.get("kind"), f"materials[{index}].kind", 48),
            "content_sha256": sha256_text(item.get("content_sha256"), f"materials[{index}].content_sha256"),
        })
    return tuple(materials)
