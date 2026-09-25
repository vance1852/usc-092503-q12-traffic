"""离线命令行验收入口：走通登记、补正、受理中止、复核变更与送达全流程。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .clock import FrozenClock
from .service import PenaltyAppealService
from .storage import connect


def _material_item(number: int, title: str) -> dict[str, str]:
    return {
        "material_id": f"mat-{number}",
        "title": title,
        "kind": "statement",
        "content_sha256": f"{number:064d}",
    }


def run(database: str = ":memory:") -> dict[str, object]:
    connection = connect(database)
    clock = FrozenClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
    service = PenaltyAppealService(connection, clock)
    service.bootstrap()
    admin = "admin"
    for user_id, role in (("clerk-1", "clerk"), ("reviewer-1", "reviewer"), ("auditor-1", "auditor")):
        service.create_user(admin, user_id, user_id, role)

    service.register_party(admin, "party-zhang", "individual", "张某")
    service.register_party(admin, "party-li", "individual", "李某")
    service.grant_authorization(admin, {
        "authorization_id": "auth-1",
        "party_id": "party-zhang",
        "agent_party_id": "party-li",
        "power": "full",
        "valid_from": "2026-08-01T00:00:00Z",
        "document_sha256": "a" * 64,
    })
    decision = service.register_decision(admin, {
        "decision_id": "pen-2026-001",
        "decision_number": "公交决字[2026]第001号",
        "subject_party_id": "party-zhang",
        "title": "超速行驶处罚",
        "decided_by_staff": "officer-wang",
        "decided_at": "2026-08-20T10:00:00Z",
        "terms": [
            {"action_type": "payment_demand", "amount_text": "200.00", "due_at": "2026-09-20T10:00:00Z"},
            {"action_type": "demerit_points", "note": "记6分"},
        ],
    })

    # 代理人持授权发起申诉；同一法定事由再次提交应合并
    first = service.register_appeal("clerk-1", {
        "appeal_id": "appeal-001",
        "decision_id": "pen-2026-001",
        "legal_ground_code": "fact_unclear",
        "applicant_party_id": "party-li",
        "representative_party_id": "party-li",
        "materials": [_material_item(1, "行政复议申请书")],
        "evidence_refs": [{
            "evidence_id": "speed-photo-7",
            "evidence_version": "v2",
            "title": "测速照片（修正版）",
            "content_sha256": "b" * 64,
        }],
    })
    second = service.register_appeal("clerk-1", {
        "appeal_id": "appeal-002",
        "decision_id": "pen-2026-001",
        "legal_ground_code": "fact_unclear",
        "applicant_party_id": "party-li",
        "representative_party_id": "party-li",
        "materials": [_material_item(2, "情况说明")],
    })

    # 材料补正与补交
    service.issue_correction_notice("clerk-1", "appeal-001", ["补充签字页"], cure_days=7)
    clock.advance(days=2)
    service.cure_resubmit("clerk-1", "appeal-001", [_material_item(3, "签字页")])

    # 受理：催缴动作应中止，记分动作不自动中止
    service.review_acceptance("clerk-1", "appeal-001", True, "材料齐全，予以受理")
    actions_after_accept = {a["action_type"]: a["state"] for a in service.list_enforcement_actions("clerk-1", "pen-2026-001")}

    # 指派复核人员并作出变更决定
    service.assign_reviewer("admin", "appeal-001", "reviewer-1")
    clock.advance(days=10)
    service.decide_review("reviewer-1", "appeal-001", "modified", "测速数据采信修正版本，处罚金额调整", [
        {"action_id": "pen-2026-001:payment_demand", "adjustment": "modify", "amount_text": "100.00"},
    ])
    service.record_service(
        "clerk-1", "appeal-001", "final_decision", "electronic", "party-li", "c" * 64
    )

    history = service.appeal_history("auditor-1", "appeal-001")
    chain = service.audit_chain("auditor-1")
    return {
        "status": "ok",
        "decision_id": decision["decision"]["decision_id"],
        "first_appeal": first,
        "second_appeal_merged": second.get("merged"),
        "actions_after_acceptance": actions_after_accept,
        "final_status": service.get_appeal("appeal-001")["appeal"]["status"],
        "ledger_entries": [(item["entry_type"], item["action_id"]) for item in history["ledger"]],
        "served": len(history["services"]),
        "audit_valid": chain["valid"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--database", default=":memory:")
    args = parser.parse_args()
    print(json.dumps(run(args.database), ensure_ascii=False))


if __name__ == "__main__":
    main()
