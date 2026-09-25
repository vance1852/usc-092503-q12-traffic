from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from penalty_ops.appeal_api import JsonApplication
from penalty_ops.appeal_service import AppealService
from penalty_ops.clock import FrozenClock
from penalty_ops.errors import Conflict, Forbidden, InvalidState, ValidationFailed

USERS = (
    ("officer1", "officer"),
    ("party1", "party"),
    ("party2", "party"),
    ("agent1", "agent"),
    ("handler1", "handler"),
    ("reviewer1", "reviewer"),
    ("reviewer2", "reviewer"),
    ("auditor1", "auditor"),
)

DECISION = {
    "decision_id": "D1",
    "case_record_id": "CASE-1",
    "party_id": "party1",
    "violation_summary": "驾驶机动车违反道路交通信号灯通行",
    "legal_basis": "道路交通安全法第三十八条",
    "fine_amount_cny": "20000",
    "evidence": [{"evidence_id": "EV-1", "evidence_version": "v2"}],
}

UPHOLD = {
    "conclusion": "uphold",
    "reason": "事实清楚,证据确凿,维持原决定",
    "evidence_citations": [{"evidence_id": "EV-1", "evidence_version": "v2"}],
}


class AppealServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.service = AppealService(self.connection, self.clock)
        for user_id, role in USERS:
            self.service.create_user(user_id, user_id, role)
        self.service.register_decision("officer1", DECISION)

    def tearDown(self) -> None:
        self.connection.close()

    def authorize(self, valid_until: str = "2026-12-31T00:00:00Z") -> dict:
        return self.service.register_authorization("party1", {
            "party_id": "party1",
            "agent_id": "agent1",
            "scope": "代为提出申诉、提交材料",
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": valid_until,
        })

    def file(self, actor: str = "party1", ground: str = "fact_dispute") -> dict:
        return self.service.file_appeal(actor, {
            "decision_id": "D1",
            "legal_ground": ground,
            "statement": "当事人对处罚决定不服",
            "materials": [{"kind": "身份证明", "content": "身份证复印件"}],
        })

    def accepted_appeal(self) -> str:
        appeal_id = self.file()["appeal_id"]
        self.service.review_acceptance("reviewer1", appeal_id, True, "材料齐全,予以受理")
        return appeal_id

    # --------------------------------------------------------------
    # 发起资格
    # --------------------------------------------------------------

    def test_only_party_or_authorized_agent_can_file(self) -> None:
        with self.assertRaises(Forbidden):
            self.file(actor="party2")  # 非本案当事人
        with self.assertRaises(Forbidden):
            self.file(actor="agent1")  # 无授权
        with self.assertRaises(Forbidden):
            self.file(actor="handler1")  # 承办人不能代群众发起
        self.authorize()
        filed = self.file(actor="agent1")
        self.assertFalse(filed["merged"])
        detail = self.service.appeal_detail("auditor1", filed["appeal_id"])
        self.assertEqual(detail["appeal"]["appellant_kind"], "agent")
        self.assertIsNotNone(detail["appeal"]["authorization_id"])

    def test_expired_authorization_cannot_file(self) -> None:
        self.authorize(valid_until="2026-09-20T00:00:00Z")
        with self.assertRaises(Forbidden):
            self.file(actor="agent1")

    def test_authorization_requires_party_and_agent_roles(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_authorization("officer1", {
                "party_id": "party1", "agent_id": "agent1", "scope": "x",
                "valid_from": "2026-09-01T00:00:00Z", "valid_until": "2026-10-01T00:00:00Z",
            })
        with self.assertRaises(ValidationFailed):
            self.service.register_authorization("party1", {
                "party_id": "party1", "agent_id": "party2", "scope": "x",
                "valid_from": "2026-09-01T00:00:00Z", "valid_until": "2026-10-01T00:00:00Z",
            })

    # --------------------------------------------------------------
    # 重复提交合并
    # --------------------------------------------------------------

    def test_same_ground_merges_instead_of_new_case(self) -> None:
        first = self.file()
        second = self.service.file_appeal("party1", {
            "decision_id": "D1",
            "legal_ground": "fact_dispute",
            "statement": "补充新的理由",
            "materials": [{"kind": "现场照片", "content": "路口照片两张"}],
        })
        self.assertTrue(second["merged"])
        self.assertEqual(first["appeal_id"], second["appeal_id"])
        detail = self.service.appeal_detail("auditor1", first["appeal_id"])
        self.assertEqual(detail["appeal"]["merge_count"], 1)
        self.assertIn("现场照片", {m["kind"] for m in detail["materials"]})
        self.assertIn("statement", {m["kind"] for m in detail["materials"]})
        appeals = self.service.appeals_for_decision("handler1", "D1")
        self.assertEqual(len(appeals), 1)

    def test_different_ground_creates_new_case(self) -> None:
        first = self.file()
        other = self.file(ground="procedure_violation")
        self.assertFalse(other["merged"])
        self.assertNotEqual(first["appeal_id"], other["appeal_id"])

    def test_decided_ground_cannot_be_filed_again(self) -> None:
        appeal_id = self.accepted_appeal()
        self.service.decide_reconsideration("reviewer1", appeal_id, UPHOLD)
        with self.assertRaises(Conflict):
            self.file()

    # --------------------------------------------------------------
    # 材料补正
    # --------------------------------------------------------------

    def test_material_correction_flow(self) -> None:
        appeal_id = self.file()["appeal_id"]
        correction = self.service.request_material_correction("handler1", appeal_id, "需补充授权委托书", "材料不齐全")
        self.assertEqual(correction["state"], "correcting")
        detail = self.service.appeal_detail("auditor1", appeal_id)
        deadlines = {(d["kind"], d["status"]) for d in detail["deadlines"]}
        self.assertIn(("acceptance", "void"), deadlines)  # 受理期限重新起算
        self.assertIn(("correction", "open"), deadlines)
        supplemented = self.service.submit_supplement("party1", appeal_id, [{"kind": "委托书", "content": "授权委托书"}])
        self.assertEqual(supplemented["state"], "registered")
        detail = self.service.appeal_detail("auditor1", appeal_id)
        self.assertEqual(detail["corrections"][0]["status"], "fulfilled")
        self.assertIn(("acceptance", "open"), {(d["kind"], d["status"]) for d in detail["deadlines"]})

    def test_overdue_supplement_rejects_appeal_with_reason(self) -> None:
        appeal_id = self.file()["appeal_id"]
        self.service.request_material_correction("handler1", appeal_id, "需补充材料", "材料不齐全")
        self.clock.advance(days=6)  # 超过 5 日补正期限
        result = self.service.submit_supplement("party1", appeal_id, [{"kind": "x", "content": "y"}])
        self.assertEqual(result["state"], "rejected")
        detail = self.service.appeal_detail("auditor1", appeal_id)
        self.assertEqual(detail["corrections"][0]["status"], "expired")
        report = self.service.overdue_report("auditor1")
        self.assertTrue(any("逾期未补正" in r["overdue_reason"] for r in report["recorded"]))

    def test_supplement_only_by_appellant(self) -> None:
        appeal_id = self.file()["appeal_id"]
        self.service.request_material_correction("handler1", appeal_id, "需补充材料", "材料不齐全")
        with self.assertRaises(Forbidden):
            self.service.submit_supplement("party2", appeal_id, [{"kind": "x", "content": "y"}])

    # --------------------------------------------------------------
    # 受理审查与执行中止
    # --------------------------------------------------------------

    def test_acceptance_suspends_enforcement_and_ledger(self) -> None:
        self.assertEqual(len(self.service.dunning_queue("officer1")["active"]), 2)
        appeal_id = self.file()["appeal_id"]
        result = self.service.review_acceptance("reviewer1", appeal_id, True, "予以受理")
        self.assertEqual(result["state"], "accepted")
        queue = self.service.dunning_queue("officer1")
        self.assertEqual(len(queue["active"]), 0)  # 催缴流程不再看到该决定
        self.assertEqual(len(queue["suspended"]), 2)
        decision = self.service.decision("officer1", "D1")
        self.assertEqual(decision["decision"]["status"], "suspended")
        self.assertTrue(all(e["status"] == "suspended" for e in decision["ledger"]))

    def test_acceptance_rejection_keeps_enforcement(self) -> None:
        appeal_id = self.file()["appeal_id"]
        result = self.service.review_acceptance("reviewer1", appeal_id, False, "不属于受理范围")
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(len(self.service.dunning_queue("officer1")["active"]), 2)
        decision = self.service.decision("officer1", "D1")
        self.assertEqual(decision["decision"]["status"], "active")

    def test_overdue_acceptance_requires_reason_and_is_queryable(self) -> None:
        appeal_id = self.file()["appeal_id"]
        self.clock.advance(days=6)  # 超过 5 日受理审查期限
        with self.assertRaises(ValidationFailed):
            self.service.review_acceptance("reviewer1", appeal_id, True, "予以受理")
        self.service.review_acceptance("reviewer1", appeal_id, True, "予以受理", overdue_reason="需跨部门核查证据")
        report = self.service.overdue_report("auditor1")
        self.assertTrue(any(r["overdue_reason"] == "需跨部门核查证据" for r in report["recorded"]))

    # --------------------------------------------------------------
    # 复核决定:驳回 / 变更 / 撤销
    # --------------------------------------------------------------

    def test_uphold_restores_enforcement_and_ledger(self) -> None:
        appeal_id = self.accepted_appeal()
        result = self.service.decide_reconsideration("reviewer1", appeal_id, UPHOLD)
        self.assertEqual(result["conclusion"], "uphold")
        queue = self.service.dunning_queue("officer1")
        self.assertEqual(len(queue["active"]), 2)
        decision = self.service.decision("officer1", "D1")
        self.assertEqual(decision["decision"]["status"], "active")
        self.assertEqual(decision["ledger"][0]["status"], "outstanding")

    def test_modify_adjusts_ledger_transactionally(self) -> None:
        appeal_id = self.accepted_appeal()
        self.service.decide_reconsideration("reviewer1", appeal_id, {
            "conclusion": "modify",
            "reason": "量罚过重,变更为 50 元",
            "new_fine_amount_cny": "5000",
            "evidence_citations": [{"evidence_id": "EV-1", "evidence_version": "v2"}],
        })
        decision = self.service.decision("officer1", "D1")
        self.assertEqual(decision["decision"]["status"], "modified")
        self.assertEqual(decision["decision"]["fine_amount_cny"], "5000")
        fine_entries = [e for e in decision["ledger"] if e["kind"] == "fine"]
        self.assertEqual({e["status"] for e in fine_entries}, {"adjusted", "outstanding"})
        new_entry = next(e for e in fine_entries if e["status"] == "outstanding")
        old_entry = next(e for e in fine_entries if e["status"] == "adjusted")
        self.assertEqual(new_entry["amount_cny"], "5000")
        self.assertEqual(new_entry["supersedes_entry_id"], old_entry["entry_id"])
        self.assertEqual(new_entry["appeal_id"], appeal_id)
        self.assertEqual(len(self.service.dunning_queue("officer1")["active"]), 2)

    def test_revoke_waives_ledger_and_cancels_actions(self) -> None:
        appeal_id = self.accepted_appeal()
        self.service.decide_reconsideration("reviewer1", appeal_id, {
            "conclusion": "revoke",
            "reason": "证据不足,撤销原决定",
            "evidence_citations": [{"evidence_id": "EV-1", "evidence_version": "v2"}],
        })
        decision = self.service.decision("officer1", "D1")
        self.assertEqual(decision["decision"]["status"], "revoked")
        self.assertTrue(all(e["status"] == "waived" for e in decision["ledger"]))
        queue = self.service.dunning_queue("officer1")
        self.assertEqual(len(queue["active"]), 0)
        self.assertEqual(len(queue["suspended"]), 0)
        with self.assertRaises(InvalidState):
            self.file(ground="procedure_violation")  # 已撤销决定不能再申诉

    def test_decision_requires_citations_and_modify_amount(self) -> None:
        appeal_id = self.accepted_appeal()
        with self.assertRaises(ValidationFailed):
            self.service.decide_reconsideration("reviewer1", appeal_id, {"conclusion": "uphold", "reason": "x"})
        with self.assertRaises(ValidationFailed):
            self.service.decide_reconsideration("reviewer1", appeal_id, {
                "conclusion": "modify", "reason": "x",
                "evidence_citations": [{"evidence_id": "EV-1", "evidence_version": "v2"}],
            })

    def test_overdue_reconsideration_records_reason(self) -> None:
        appeal_id = self.accepted_appeal()
        self.clock.advance(days=31)  # 超过 30 日复核期限
        with self.assertRaises(ValidationFailed):
            self.service.decide_reconsideration("reviewer1", appeal_id, UPHOLD)
        self.service.decide_reconsideration("reviewer1", appeal_id, dict(UPHOLD, overdue_reason="案情复杂集体讨论"))
        detail = self.service.appeal_detail("auditor1", appeal_id)
        review = next(r for r in detail["reviews"] if r["stage"] == "reconsideration")
        self.assertEqual(review["overdue_reason"], "案情复杂集体讨论")

    def test_evidence_citations_are_queryable(self) -> None:
        appeal_id = self.accepted_appeal()
        self.service.decide_reconsideration("reviewer1", appeal_id, {
            "conclusion": "uphold",
            "reason": "维持",
            "evidence_citations": [
                {"evidence_id": "EV-1", "evidence_version": "v2"},
                {"evidence_id": "EV-9", "evidence_version": "v1"},
            ],
        })
        citations = self.service.review_evidence("auditor1", appeal_id)
        self.assertEqual(
            {(c["evidence_id"], c["evidence_version"]) for c in citations},
            {("EV-1", "v2"), ("EV-9", "v1")},
        )
        decision = self.service.decision("officer1", "D1")
        self.assertEqual(decision["evidence"], [{"decision_id": "D1", "evidence_id": "EV-1", "evidence_version": "v2"}])

    # --------------------------------------------------------------
    # 回避
    # --------------------------------------------------------------

    def test_recusal_blocks_reviewer_and_is_queryable(self) -> None:
        appeal_id = self.file()["appeal_id"]
        self.service.recuse_reviewer("reviewer1", appeal_id, "与当事人系近亲属")
        with self.assertRaises(Forbidden):
            self.service.review_acceptance("reviewer1", appeal_id, True, "受理")
        self.service.review_acceptance("reviewer2", appeal_id, True, "受理")  # 未回避人员可以审查
        recusals = self.service.recusals("auditor1", appeal_id=appeal_id)
        self.assertEqual(len(recusals), 1)
        self.assertEqual(recusals[0]["reviewer_id"], "reviewer1")
        self.assertEqual(recusals[0]["reason"], "与当事人系近亲属")
        with self.assertRaises(Conflict):
            self.service.recuse_reviewer("reviewer1", appeal_id, "重复登记")

    # --------------------------------------------------------------
    # 撤回
    # --------------------------------------------------------------

    def test_withdrawal_from_accepted_resumes_enforcement(self) -> None:
        appeal_id = self.accepted_appeal()
        result = self.service.withdraw_appeal("party1", appeal_id, "双方自行和解")
        self.assertEqual(result["state"], "withdrawn")
        queue = self.service.dunning_queue("officer1")
        self.assertEqual(len(queue["active"]), 2)
        decision = self.service.decision("officer1", "D1")
        self.assertEqual(decision["decision"]["status"], "active")
        self.assertEqual(decision["ledger"][0]["status"], "outstanding")

    def test_withdrawal_during_correction_cancels_correction(self) -> None:
        appeal_id = self.file()["appeal_id"]
        self.service.request_material_correction("handler1", appeal_id, "需补充材料", "材料不齐全")
        self.service.withdraw_appeal("party1", appeal_id, "不再申诉")
        detail = self.service.appeal_detail("auditor1", appeal_id)
        self.assertEqual(detail["corrections"][0]["status"], "cancelled")
        self.assertEqual(detail["appeal"]["state"], "withdrawn")

    def test_withdrawal_permission_and_state(self) -> None:
        appeal_id = self.file()["appeal_id"]
        with self.assertRaises(Forbidden):
            self.service.withdraw_appeal("party2", appeal_id, "越权撤回")
        self.service.withdraw_appeal("party1", appeal_id, "撤回")
        with self.assertRaises(InvalidState):
            self.service.withdraw_appeal("party1", appeal_id, "重复撤回")

    # --------------------------------------------------------------
    # 送达
    # --------------------------------------------------------------

    def test_delivery_recorded_after_decision_and_queryable(self) -> None:
        appeal_id = self.accepted_appeal()
        with self.assertRaises(InvalidState):
            self.service.record_delivery("handler1", appeal_id, "electronic", "party1")
        self.service.decide_reconsideration("reviewer1", appeal_id, UPHOLD)
        delivery = self.service.record_delivery("handler1", appeal_id, "electronic", "party1")
        self.assertEqual(delivery["method"], "electronic")
        fetched = self.service.delivery("auditor1", appeal_id)
        self.assertEqual(fetched["recipient"], "party1")
        self.assertEqual(fetched["delivered_at"], "2026-09-25T08:00:00Z")
        with self.assertRaises(Conflict):
            self.service.record_delivery("handler1", appeal_id, "postal", "party1")

    # --------------------------------------------------------------
    # 权限与审计
    # --------------------------------------------------------------

    def test_role_permissions(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_decision("party1", DECISION)
        appeal_id = self.file()["appeal_id"]
        with self.assertRaises(Forbidden):
            self.service.request_material_correction("party1", appeal_id, "x", "y")
        with self.assertRaises(Forbidden):
            self.service.review_acceptance("handler1", appeal_id, True, "受理")
        with self.assertRaises(Forbidden):
            self.service.recuse_reviewer("party1", appeal_id, "回避")
        with self.assertRaises(Forbidden):
            self.service.record_delivery("reviewer1", appeal_id, "electronic", "party1")

    def test_audit_trail_records_flow(self) -> None:
        appeal_id = self.accepted_appeal()
        events = self.service.audit_events("auditor1", "appeal", appeal_id)
        event_types = [e["event_type"] for e in events]
        self.assertEqual(event_types, ["appeal.registered", "appeal.accepted"])
        with self.assertRaises(Forbidden):
            self.service.audit_events("party1", "appeal", appeal_id)

    def test_overdue_report_lists_open_deadlines(self) -> None:
        self.file()
        self.clock.advance(days=6)
        report = self.service.overdue_report("auditor1")
        self.assertEqual(len(report["currently_overdue"]), 1)
        self.assertEqual(report["currently_overdue"][0]["kind"], "acceptance")


class AppealApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.app = JsonApplication(AppealService(self.connection, clock))

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, actor: str | None = None):
        headers = {"X-Actor-Id": actor} if actor else {}
        return self.app.handle("POST", path, headers, json.dumps(payload).encode())

    def get(self, path: str, actor: str):
        return self.app.handle("GET", path, {"X-Actor-Id": actor})

    def test_full_flow_over_http(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        for user_id, role in USERS:
            response = self.post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
            self.assertEqual(response.status, 201)
        response = self.post("/decisions", DECISION, "officer1")
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["decision"]["status"], "active")
        response = self.post("/appeals", {
            "decision_id": "D1", "legal_ground": "fact_dispute",
            "statement": "不服处罚", "materials": [],
        }, "party1")
        self.assertEqual(response.status, 201)
        appeal_id = response.body["appeal_id"]
        response = self.post(f"/appeals/{appeal_id}/corrections", {"required_items": "补充证据", "reason": "材料不全"}, "handler1")
        self.assertEqual(response.status, 201)
        response = self.post(f"/appeals/{appeal_id}/supplements", {"materials": [{"kind": "证据", "content": "补充材料"}]}, "party1")
        self.assertEqual(response.status, 200)
        response = self.post(f"/appeals/{appeal_id}/acceptance", {"accept": True, "reason": "予以受理"}, "reviewer1")
        self.assertEqual(response.status, 200)
        queue = self.get("/dunning-queue", "officer1")
        self.assertEqual(len(queue.body["suspended"]), 2)
        response = self.post(f"/appeals/{appeal_id}/decision", UPHOLD, "reviewer1")
        self.assertEqual(response.status, 200)
        response = self.post(f"/appeals/{appeal_id}/delivery", {"method": "electronic", "recipient": "party1"}, "handler1")
        self.assertEqual(response.status, 201)
        detail = self.get(f"/appeals/{appeal_id}", "auditor1")
        self.assertEqual(detail.body["appeal"]["state"], "decided")
        self.assertEqual(detail.body["delivery"]["recipient"], "party1")
        citations = self.get(f"/appeals/{appeal_id}/evidence-citations", "auditor1")
        self.assertEqual(len(citations.body["citations"]), 1)
        overdue = self.get("/overdue", "auditor1")
        self.assertEqual(overdue.body["currently_overdue"], [])

    def test_error_shape_and_actor_header(self) -> None:
        response = self.post("/appeals", {"decision_id": "D1"})
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        self.post("/users", {"user_id": "party1", "display_name": "p", "role": "party"})
        response = self.post("/appeals", {"decision_id": "none", "legal_ground": "bad", "statement": "x"}, "party1")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("GET", "/nothing", {"X-Actor-Id": "party1"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
