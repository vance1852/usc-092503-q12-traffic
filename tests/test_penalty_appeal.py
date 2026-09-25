from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from penalty_appeal.api import JsonApplication
from penalty_appeal.clock import FrozenClock
from penalty_appeal.errors import Conflict, Forbidden, InvalidState
from penalty_appeal.service import PenaltyAppealService
from penalty_appeal.storage import connect


def material(number: int, title: str = "申请书") -> dict[str, str]:
    return {
        "material_id": f"mat-{number}",
        "title": title,
        "kind": "statement",
        "content_sha256": f"{number:064d}",
    }


EVIDENCE = {
    "evidence_id": "speed-photo-7",
    "evidence_version": "v2",
    "title": "测速照片修正版",
    "content_sha256": "b" * 64,
}


class AppealServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.clock = FrozenClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        self.service = PenaltyAppealService(self.connection, self.clock)
        self.service.bootstrap()
        for user_id, role in (
            ("clerk-1", "clerk"),
            ("reviewer-1", "reviewer"),
            ("reviewer-2", "reviewer"),
            ("officer-wang", "reviewer"),
            ("auditor-1", "auditor"),
        ):
            self.service.create_user("admin", user_id, user_id, role)
        self.service.register_party("admin", "party-zhang", "individual", "张某")
        self.service.register_party("admin", "party-li", "individual", "李某")
        self.service.register_party("admin", "party-stranger", "individual", "王某")
        self.service.grant_authorization("admin", {
            "authorization_id": "auth-full",
            "party_id": "party-zhang",
            "agent_party_id": "party-li",
            "power": "full",
            "valid_from": "2026-08-01T00:00:00Z",
            "document_sha256": "a" * 64,
        })
        self.service.register_decision("admin", {
            "decision_id": "pen-1",
            "decision_number": "决字001",
            "subject_party_id": "party-zhang",
            "title": "超速处罚",
            "decided_by_staff": "officer-wang",
            "decided_at": "2026-08-20T10:00:00Z",
            "terms": [
                {"action_type": "payment_demand", "amount_text": "200.00", "due_at": "2026-09-20T10:00:00Z"},
                {"action_type": "license_suspension", "note": "暂扣驾照"},
                {"action_type": "demerit_points", "note": "记6分"},
            ],
        })

    def tearDown(self) -> None:
        self.connection.close()

    def register_appeal(self, appeal_id: str = "ap-1", ground: str = "fact_unclear",
                        applicant: str = "party-li", rep: str | None = "party-li", **extra) -> dict:
        payload = {
            "appeal_id": appeal_id,
            "decision_id": "pen-1",
            "legal_ground_code": ground,
            "applicant_party_id": applicant,
            "materials": [material(1)],
            "evidence_refs": [EVIDENCE],
        }
        if rep is not None:
            payload["representative_party_id"] = rep
        payload.update(extra)
        return self.service.register_appeal("clerk-1", payload)

    def action_states(self) -> dict[str, str]:
        return {
            row["action_type"]: row["state"]
            for row in self.service.list_enforcement_actions("clerk-1", "pen-1")
        }

    # ----- 发起资格 -----

    def test_only_subject_or_authorized_agent_can_initiate(self) -> None:
        with self.assertRaises(Forbidden):
            self.register_appeal(applicant="party-stranger", rep=None)
        # 代理人凭全权委托可以发起
        result = self.register_appeal()
        self.assertFalse(result["merged"])
        # 当事人本人也可以发起
        self.service.register_appeal("clerk-1", {
            "appeal_id": "ap-self", "decision_id": "pen-1", "legal_ground_code": "wrong_basis",
            "applicant_party_id": "party-zhang", "materials": [material(5)],
        })

    def test_register_only_authorization_cannot_withdraw(self) -> None:
        self.service.grant_authorization("admin", {
            "authorization_id": "auth-register",
            "party_id": "party-zhang",
            "agent_party_id": "party-stranger",
            "power": "register",
            "valid_from": "2026-08-01T00:00:00Z",
            "document_sha256": "c" * 64,
        })
        self.service.register_appeal("clerk-1", {
            "appeal_id": "ap-r", "decision_id": "pen-1", "legal_ground_code": "overreach",
            "applicant_party_id": "party-stranger", "representative_party_id": "party-stranger",
            "authorization_id": "auth-register", "materials": [material(7)],
        })
        with self.assertRaises(Forbidden):
            self.service.withdraw_appeal("clerk-1", "ap-r", "party-stranger", "不想申诉了")
        # 补充撤回授权后可撤回
        self.service.grant_authorization("admin", {
            "authorization_id": "auth-withdraw",
            "party_id": "party-zhang",
            "agent_party_id": "party-stranger",
            "power": "withdraw",
            "valid_from": "2026-08-01T00:00:00Z",
            "document_sha256": "d" * 64,
        })
        result = self.service.withdraw_appeal("clerk-1", "ap-r", "party-stranger", "不想申诉了")
        self.assertEqual(result["appeal"]["status"], "withdrawn")

    def test_expired_authorization_is_rejected(self) -> None:
        self.service.grant_authorization("admin", {
            "authorization_id": "auth-expired",
            "party_id": "party-zhang",
            "agent_party_id": "party-stranger",
            "power": "full",
            "valid_from": "2026-07-01T00:00:00Z",
            "valid_to": "2026-08-01T00:00:00Z",
            "document_sha256": "e" * 64,
        })
        with self.assertRaises(Forbidden):
            self.register_appeal(applicant="party-stranger", rep="party-stranger")

    # ----- 合并与重复提交 -----

    def test_duplicate_same_ground_merges_instead_of_new_case(self) -> None:
        first = self.register_appeal("ap-1")
        second_payload = {
            "appeal_id": "ap-2", "decision_id": "pen-1", "legal_ground_code": "fact_unclear",
            "applicant_party_id": "party-li", "representative_party_id": "party-li",
            "materials": [material(2)],
        }
        second = self.service.register_appeal("clerk-1", second_payload)
        self.assertFalse(first["merged"])
        self.assertTrue(second["merged"])
        self.assertEqual(second["appeal_id"], "ap-1")
        detail = self.service.get_appeal("ap-1")
        self.assertEqual(len(detail["materials"]), 2)
        # 不同法定事由另立新案
        other = self.register_appeal("ap-3", ground="procedure_violated")
        self.assertFalse(other["merged"])

    def test_resubmit_same_ground_after_decision_conflicts(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        self.service.decide_review("reviewer-1", "ap-1", "upheld", "维持")
        with self.assertRaises(Conflict):
            self.register_appeal("ap-again")

    def test_idempotency_key_replays_and_detects_payload_change(self) -> None:
        payload = {
            "appeal_id": "ap-idem", "decision_id": "pen-1", "legal_ground_code": "wrong_basis",
            "applicant_party_id": "party-zhang", "materials": [material(9)],
            "idempotency_key": "key-1",
        }
        first = self.service.register_appeal("clerk-1", payload)
        second = self.service.register_appeal("clerk-1", payload)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.register_appeal("clerk-1", dict(payload, materials=[material(11)]))

    # ----- 期限与逾期原因 -----

    def test_application_window_requires_late_reason(self) -> None:
        self.service.register_decision("admin", {
            "decision_id": "pen-old", "decision_number": "决字旧", "subject_party_id": "party-zhang",
            "title": "旧处罚", "decided_at": "2026-06-01T10:00:00Z",
            "terms": [{"action_type": "payment_demand"}],
        })
        payload = {
            "appeal_id": "ap-late", "decision_id": "pen-old", "legal_ground_code": "fact_unclear",
            "applicant_party_id": "party-zhang", "materials": [material(3)],
        }
        with self.assertRaises(Exception):
            self.service.register_appeal("clerk-1", payload)
        result = self.service.register_appeal("clerk-1", dict(payload, late_reason="地震导致交通中断"))
        self.assertTrue(result["beyond_window"])

    def test_acceptance_overdue_must_be_explained_before_progress(self) -> None:
        self.register_appeal()
        self.clock.advance(days=6)
        detected = self.service.detect_overdue("clerk-1")
        self.assertEqual([item["stage"] for item in detected["detected"]], ["acceptance"])
        with self.assertRaises(InvalidState):
            self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.explain_overdue("clerk-1", "ap-1", "acceptance", "staff_shortage", "承办人培训")
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        history = self.service.appeal_history("auditor-1", "ap-1")
        self.assertEqual(history["overdue"][0]["reason_code"], "staff_shortage")
        self.assertIsNotNone(history["overdue"][0]["resolved_at"])

    def test_review_overdue_blocks_decision_until_explained(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        self.clock.advance(days=61)
        self.service.detect_overdue("auditor-1")
        with self.assertRaises(InvalidState):
            self.service.decide_review("reviewer-1", "ap-1", "upheld", "维持")
        self.service.explain_overdue("admin", "ap-1", "review", "awaiting_expertise", "等待鉴定意见")
        self.service.decide_review("reviewer-1", "ap-1", "upheld", "维持")

    # ----- 材料补正 -----

    def test_correction_cure_and_deemed_withdrawn(self) -> None:
        self.register_appeal()
        self.service.issue_correction_notice("clerk-1", "ap-1", ["补签字"], cure_days=5)
        self.assertEqual(self.service.get_appeal("ap-1")["appeal"]["status"], "materials_pending")
        self.clock.advance(days=2)
        self.service.cure_resubmit("clerk-1", "ap-1", [material(4, "签字页")])
        self.assertEqual(self.service.get_appeal("ap-1")["appeal"]["status"], "registered")

        self.service.issue_correction_notice("clerk-1", "ap-1", ["再补证据"], cure_days=3)
        self.clock.advance(days=4)
        with self.assertRaises(InvalidState):
            self.service.cure_resubmit("clerk-1", "ap-1", [material(6)])
        self.service.close_unremedied("clerk-1", "ap-1", "逾期未补正")
        self.assertEqual(self.service.get_appeal("ap-1")["appeal"]["status"], "withdrawn")

    # ----- 受理中止与复核结论 -----

    def test_acceptance_suspends_configured_actions(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.assertEqual(self.action_states(), {
            "payment_demand": "suspended",
            "license_suspension": "suspended",
            "demerit_points": "active",
        })

    def test_upheld_resumes_actions_transactionally(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        self.service.decide_review("reviewer-1", "ap-1", "upheld", "维持原处罚")
        self.assertEqual(set(self.action_states().values()), {"active"})
        history = self.service.appeal_history("auditor-1", "ap-1")
        types = [entry["entry_type"] for entry in history["ledger"]]
        self.assertEqual(types.count("suspend"), 2)
        self.assertEqual(types.count("resume"), 2)

    def test_revoked_terminates_every_action(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        self.service.decide_review("reviewer-1", "ap-1", "revoked", "事实不清，撤销")
        self.assertEqual(set(self.action_states().values()), {"terminated"})

    def test_modified_adjusts_ledger_and_resumes_unspecified(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        self.service.decide_review("reviewer-1", "ap-1", "modified", "金额调整", [
            {"action_id": "pen-1:payment_demand", "adjustment": "modify", "amount_text": "100.00",
             "due_at": "2026-10-20T10:00:00Z"},
            {"action_id": "pen-1:license_suspension", "adjustment": "terminate"},
        ])
        actions = {row["action_type"]: row for row in self.service.list_enforcement_actions("clerk-1", "pen-1")}
        self.assertEqual(actions["payment_demand"]["state"], "active")
        self.assertEqual(actions["payment_demand"]["current_amount_text"], "100.00")
        self.assertEqual(actions["license_suspension"]["state"], "terminated")
        self.assertEqual(actions["demerit_points"]["state"], "active")
        history = self.service.appeal_history("auditor-1", "ap-1")
        modify = [e for e in history["ledger"] if e["entry_type"] == "modify"][0]
        self.assertEqual(modify["amount_before"], "200.00")
        self.assertEqual(modify["amount_after"], "100.00")

    def test_modified_requires_adjustments(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        with self.assertRaises(Exception):
            self.service.decide_review("reviewer-1", "ap-1", "modified", "变更")

    def test_rejection_at_acceptance_does_not_suspend(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", False, "不属于受案范围")
        self.assertEqual(self.service.get_appeal("ap-1")["appeal"]["status"], "rejected")
        self.assertEqual(set(self.action_states().values()), {"active"})

    def test_withdraw_after_acceptance_resumes_suspension(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.withdraw_appeal("clerk-1", "ap-1", "party-li", "自愿撤回")
        self.assertEqual(set(self.action_states().values()), {"active"})

    # ----- 回避 -----

    def test_original_officer_and_recused_reviewer_blocked(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        with self.assertRaises(Forbidden):
            self.service.assign_reviewer("admin", "ap-1", "officer-wang")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        with self.assertRaises(Forbidden):
            self.service.decide_review("reviewer-2", "ap-1", "upheld", "未被指派")
        # 当事人申请回避，管理员批准后原指派失效
        self.service.request_recusal("clerk-1", "ap-1", "reviewer-1", "与当事人有利害关系", "party-li")
        self.service.decide_recusal("admin", 1, True, "准许回避")
        with self.assertRaises(Forbidden):
            self.service.decide_review("reviewer-1", "ap-1", "upheld", "已回避")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-2")
        self.service.decide_review("reviewer-2", "ap-1", "upheld", "维持")
        history = self.service.appeal_history("auditor-1", "ap-1")
        self.assertTrue(any(item["status"] == "approved" for item in history["recusals"]))

    def test_self_recuse(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        self.service.self_recuse("reviewer-1", "ap-1", "发现利害关系")
        with self.assertRaises(Forbidden):
            self.service.decide_review("reviewer-1", "ap-1", "upheld", "维持")

    # ----- 送达与证据版本 -----

    def test_final_service_completes_flow_and_records_versions(self) -> None:
        self.register_appeal()
        self.service.review_acceptance("clerk-1", "ap-1", True, "受理")
        self.service.assign_reviewer("admin", "ap-1", "reviewer-1")
        self.service.decide_review("reviewer-1", "ap-1", "upheld", "维持")
        self.service.record_service("clerk-1", "ap-1", "final_decision", "electronic", "party-li", "f" * 64)
        self.assertEqual(self.service.get_appeal("ap-1")["appeal"]["status"], "served")
        history = self.service.appeal_history("auditor-1", "ap-1")
        self.assertEqual(history["services"][0]["method"], "electronic")
        self.assertEqual(history["review_decision"]["evidence_snapshot"][0]["evidence_version"], "v2")
        with self.assertRaises(Conflict):
            self.service.record_service("clerk-1", "ap-1", "final_decision", "mail", "party-li", "9" * 64)

    def test_auditor_cannot_mutate(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_appeal("auditor-1", {
                "appeal_id": "ap-x", "decision_id": "pen-1", "legal_ground_code": "fact_unclear",
                "applicant_party_id": "party-zhang", "materials": [material(8)],
            })

    def test_audit_chain_detects_tampering(self) -> None:
        self.register_appeal()
        self.assertTrue(self.service.audit_chain("auditor-1")["valid"])
        self.connection.execute("UPDATE appeal_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("auditor-1")["valid"])


class AppealApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.clock = FrozenClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))
        self.service = PenaltyAppealService(self.connection, self.clock)
        self.service.bootstrap()
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def headers(self, actor: str = "admin") -> dict[str, str]:
        return {"X-Actor-Id": actor}

    def test_health_and_permission_boundary(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        missing = self.app.handle("POST", "/parties", {}, b'{"party_id":"p"}')
        self.assertEqual(missing.status, 422)
        self.assertEqual(missing.body["error"]["code"], "validation_failed")
        self.assertEqual(self.app.handle("GET", "/appeals/ap-1", self.headers("nope")).status, 404)
        self.assertEqual(self.app.handle("GET", "/nope", self.headers("admin")).status, 404)

    def test_full_flow_over_http(self) -> None:
        self.app.handle("POST", "/users", self.headers("admin"), b'{"user_id":"c1","display_name":"c1","role":"clerk"}')
        self.app.handle("POST", "/users", self.headers("admin"), b'{"user_id":"r1","display_name":"r1","role":"reviewer"}')
        self.app.handle("POST", "/parties", self.headers("admin"),
                        b'{"party_id":"zhang","party_type":"individual","name":"\xe5\xbc\xa0"}')
        decision = (
            '{"decision_id":"pen-1","decision_number":"001","subject_party_id":"zhang","title":"x",'
            '"decided_at":"2026-08-20T10:00:00Z","terms":[{"action_type":"payment_demand","amount_text":"200"}]}'
        ).encode()
        created = self.app.handle("POST", "/penalty_decisions", self.headers("admin"), decision)
        self.assertEqual(created.status, 201)
        appeal = (
            '{"appeal_id":"ap-1","decision_id":"pen-1","legal_ground_code":"fact_unclear",'
            '"applicant_party_id":"zhang","materials":[{"material_id":"m1","title":"t","kind":"k",'
            '"content_sha256":"' + "a" * 64 + '"}]}'
        ).encode()
        registered = self.app.handle("POST", "/appeals", self.headers("c1"), appeal)
        self.assertEqual(registered.status, 201)
        accepted = self.app.handle(
            "POST", "/appeals/ap-1/acceptance", self.headers("c1"),
            b'{"accept": true, "note": "shouli"}'
        )
        self.assertEqual(accepted.status, 200)
        self.assertEqual(self.app.handle(
            "POST", "/appeals/ap-1/reviewers", self.headers("admin"), b'{"reviewer_id":"r1"}'
        ).status, 201)
        decided = self.app.handle(
            "POST", "/appeals/ap-1/review_decision", self.headers("r1"), b'{"conclusion":"upheld","reason_text":"ok"}'
        )
        self.assertEqual(decided.status, 200)
        history = self.app.handle("GET", "/appeals/ap-1/history", self.headers("admin"))
        self.assertEqual(history.status, 200)
        self.assertEqual(len(history.body["ledger"]), 2)


if __name__ == "__main__":
    unittest.main()
