"""申诉登记、材料补正、受理审查、复核决定与撤回的事务用例。

流转规则:
- 只有处罚决定当事人或持有效授权的代理人可以发起申诉;
- 同一争议决定、同一法定事由的重复提交合并到在办案件,不生成新案;
- 受理后按规则中止该决定的催缴等执行动作并挂起台账;
- 驳回(维持)、变更、撤销结论在同一事务内恢复或调整台账与执行动作;
- 所有期限由注入时钟计算,逾期原因、回避、引用证据版本与送达均可查询。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any, Mapping

from .appeal_models import (
    DELIVERY_METHODS,
    LEGAL_GROUNDS,
    OPEN_STATES,
    RECONSIDERATION_CONCLUSIONS,
    parse_evidence_refs,
    parse_materials,
    parse_money,
    require_text,
)
from .appeal_storage import initialize, rows, transaction
from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed

ROLE_PERMISSIONS = {
    "officer": {"decision.register", "appeal.read"},
    "party": {"appeal.file", "appeal.withdraw", "material.supplement", "authorization.register", "appeal.read"},
    "agent": {"appeal.file", "appeal.withdraw", "material.supplement", "appeal.read"},
    "handler": {"correction.request", "delivery.record", "appeal.read"},
    "reviewer": {"acceptance.review", "reconsideration.decide", "recusal.record", "appeal.read"},
    "auditor": {"appeal.read", "audit.read"},
}

# 受理后需要中止的执行动作种类
SUSPENDABLE_ACTION_KINDS = ("dunning_notice", "late_fee_accrual")


class AppealService:
    """在单个 SQLite 连接上提供申诉复核全流程。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        clock=None,
        *,
        correction_days: int = 5,
        acceptance_days: int = 5,
        reconsideration_days: int = 30,
    ) -> None:
        if min(correction_days, acceptance_days, reconsideration_days) <= 0:
            raise ValueError("各项期限天数必须大于零")
        self.connection = connection
        self.clock = clock or SystemClock()
        self.correction_days = correction_days
        self.acceptance_days = acceptance_days
        self.reconsideration_days = reconsideration_days
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _due(self, days: int) -> str:
        return utc_text(self.clock.now() + timedelta(days=days))

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM appeal_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS.get(user["role"], set()):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO appeal_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), self._now()),
        )

    # ------------------------------------------------------------------
    # 基础资料
    # ------------------------------------------------------------------

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO appeal_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def _decision_row(self, decision_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM penalty_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"处罚决定不存在: {decision_id}")
        return row

    def _appeal_row(self, appeal_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"申诉不存在: {appeal_id}")
        return row

    def _appeal_with_decision(self, appeal_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT a.*,d.party_id AS party_id,d.issued_by AS decision_issued_by,d.status AS decision_status "
            "FROM appeals a JOIN penalty_decisions d ON d.decision_id=a.decision_id WHERE a.appeal_id=?",
            (appeal_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"申诉不存在: {appeal_id}")
        return row

    # ------------------------------------------------------------------
    # 处罚决定与执行动作
    # ------------------------------------------------------------------

    def register_decision(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记处罚决定,同时建立罚款台账与催缴执行动作。"""
        actor = self._require(actor_id, "decision.register")
        decision_id = require_text(raw.get("decision_id"), "决定编号")
        case_record_id = require_text(raw.get("case_record_id"), "案件编号")
        party_id = require_text(raw.get("party_id"), "当事人")
        violation_summary = require_text(raw.get("violation_summary"), "违法事实")
        legal_basis = require_text(raw.get("legal_basis"), "法律依据")
        fine_amount = parse_money(raw.get("fine_amount_cny"), "罚款金额")
        evidence = parse_evidence_refs(raw.get("evidence"), "决定引用证据")
        if not evidence:
            raise ValidationFailed("处罚决定必须引用证据版本")
        party = self._user(party_id)
        if party["role"] != "party":
            raise ValidationFailed("当事人必须具有 party 角色")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO penalty_decisions(decision_id,case_record_id,party_id,violation_summary,legal_basis,"
                    "fine_amount_cny,status,revision,issued_by,issued_at) VALUES(?,?,?,?,?,?,'active',1,?,?)",
                    (decision_id, case_record_id, party_id, violation_summary, legal_basis, fine_amount, actor["user_id"], now),
                )
                for ref in evidence:
                    self.connection.execute(
                        "INSERT INTO decision_evidence(decision_id,evidence_id,evidence_version) VALUES(?,?,?)",
                        (decision_id, ref.evidence_id, ref.evidence_version),
                    )
                self.connection.execute(
                    "INSERT INTO ledger_entries(decision_id,kind,amount_cny,status,source,created_at) "
                    "VALUES(?,'fine',?,'outstanding','decision_issued',?)",
                    (decision_id, fine_amount, now),
                )
                for kind in SUSPENDABLE_ACTION_KINDS:
                    self.connection.execute(
                        "INSERT INTO enforcement_actions(action_id,decision_id,kind,status,created_at,updated_at) "
                        "VALUES(?,?,?,'active',?,?)",
                        (f"{decision_id}:{kind}", decision_id, kind, now, now),
                    )
                self._audit(
                    "decision", decision_id, "decision.registered", actor["user_id"],
                    {"party_id": party_id, "fine_amount_cny": fine_amount,
                     "evidence": [{"evidence_id": r.evidence_id, "evidence_version": r.evidence_version} for r in evidence]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"处罚决定编号已存在: {decision_id}") from exc
        return self.decision(actor_id, decision_id)

    def decision(self, actor_id: str, decision_id: str) -> dict[str, Any]:
        self._require(actor_id, "appeal.read")
        row = self._decision_row(decision_id)
        return {
            "decision": dict(row),
            "evidence": rows(self.connection, "SELECT * FROM decision_evidence WHERE decision_id=? ORDER BY evidence_id", (decision_id,)),
            "ledger": rows(self.connection, "SELECT * FROM ledger_entries WHERE decision_id=? ORDER BY entry_id", (decision_id,)),
            "actions": rows(self.connection, "SELECT * FROM enforcement_actions WHERE decision_id=? ORDER BY action_id", (decision_id,)),
            "appeals": rows(self.connection, "SELECT appeal_id,legal_ground,appellant_id,appellant_kind,state,merge_count,registered_at FROM appeals WHERE decision_id=? ORDER BY registered_at", (decision_id,)),
        }

    def decision_ledger(self, actor_id: str, decision_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "appeal.read")
        self._decision_row(decision_id)
        return rows(self.connection, "SELECT * FROM ledger_entries WHERE decision_id=? ORDER BY entry_id", (decision_id,))

    def appeals_for_decision(self, actor_id: str, decision_id: str) -> list[dict[str, Any]]:
        """承办人查看关联到争议决定的全部申诉。"""
        self._require(actor_id, "appeal.read")
        self._decision_row(decision_id)
        return rows(self.connection, "SELECT * FROM appeals WHERE decision_id=? ORDER BY registered_at,appeal_id", (decision_id,))

    def dunning_queue(self, actor_id: str) -> dict[str, Any]:
        """催缴流程读取的执行队列:active 为可催缴,suspended 为申诉中止。"""
        self._require(actor_id, "appeal.read")
        query = (
            "SELECT a.*,d.party_id,d.fine_amount_cny,d.status AS decision_status FROM enforcement_actions a "
            "JOIN penalty_decisions d ON d.decision_id=a.decision_id WHERE a.status=? ORDER BY a.created_at,a.action_id"
        )
        return {
            "active": rows(self.connection, query, ("active",)),
            "suspended": rows(self.connection, query, ("suspended",)),
        }

    # ------------------------------------------------------------------
    # 代理授权
    # ------------------------------------------------------------------

    def register_authorization(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """当事人登记对代理人的授权,代理人凭有效授权代为申诉。"""
        self._require(actor_id, "authorization.register")
        party_id = require_text(raw.get("party_id"), "当事人")
        agent_id = require_text(raw.get("agent_id"), "代理人")
        scope = require_text(raw.get("scope"), "授权范围")
        if actor_id != party_id:
            raise Forbidden("只有当事人本人可以登记授权")
        if self._user(party_id)["role"] != "party":
            raise ValidationFailed("当事人必须具有 party 角色")
        if self._user(agent_id)["role"] != "agent":
            raise ValidationFailed("代理人必须具有 agent 角色")
        try:
            valid_from = parse_utc(require_text(raw.get("valid_from"), "授权起始时间"), "授权起始时间")
            valid_until = parse_utc(require_text(raw.get("valid_until"), "授权截止时间"), "授权截止时间")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if valid_until <= valid_from:
            raise ValidationFailed("授权截止时间必须晚于起始时间")
        authorization_id = "auth-" + uuid.uuid4().hex[:16]
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO agent_authorizations(authorization_id,party_id,agent_id,scope,valid_from,valid_until,"
                "status,created_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                (authorization_id, party_id, agent_id, scope, utc_text(valid_from), utc_text(valid_until), actor_id, self._now()),
            )
            self._audit("authorization", authorization_id, "authorization.registered", actor_id, {"party_id": party_id, "agent_id": agent_id})
        return {"authorization_id": authorization_id, "party_id": party_id, "agent_id": agent_id, "status": "active"}

    def _qualify_appellant(self, actor: sqlite3.Row, decision: sqlite3.Row) -> tuple[str, str | None]:
        """校验发起资格:当事人本人,或持有效授权的代理人。"""
        if actor["user_id"] == decision["party_id"]:
            return "party", None
        if actor["role"] != "agent":
            raise Forbidden("只有处罚决定当事人或授权的代理人可以发起申诉")
        now = self.clock.now()
        candidates = self.connection.execute(
            "SELECT * FROM agent_authorizations WHERE party_id=? AND agent_id=? AND status='active'",
            (decision["party_id"], actor["user_id"]),
        ).fetchall()
        for authorization in candidates:
            if parse_utc(authorization["valid_from"]) <= now <= parse_utc(authorization["valid_until"]):
                return "agent", authorization["authorization_id"]
        raise Forbidden("代理人没有覆盖当前时间的有效授权")

    # ------------------------------------------------------------------
    # 申诉登记与合并
    # ------------------------------------------------------------------

    def file_appeal(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记申诉;同一决定同一法定事由的在办案件自动合并。"""
        actor = self._require(actor_id, "appeal.file")
        decision_id = require_text(raw.get("decision_id"), "处罚决定编号")
        legal_ground = require_text(raw.get("legal_ground"), "法定事由")
        statement = require_text(raw.get("statement"), "申诉陈述")
        if legal_ground not in LEGAL_GROUNDS:
            raise ValidationFailed(f"法定事由必须是: {', '.join(sorted(LEGAL_GROUNDS))}")
        materials = parse_materials(raw.get("materials"))
        decision = self._decision_row(decision_id)
        if decision["status"] == "revoked":
            raise InvalidState("处罚决定已撤销,没有可申诉的对象")
        appellant_kind, authorization_id = self._qualify_appellant(actor, decision)
        closed = self.connection.execute(
            "SELECT appeal_id FROM appeals WHERE decision_id=? AND legal_ground=? AND state IN ('decided','rejected')",
            (decision_id, legal_ground),
        ).fetchone()
        if closed is not None:
            raise Conflict("同一法定事由的申诉已办结,不得重复提出")
        appeal_id = "appeal-" + uuid.uuid4().hex[:16]
        now = self._now()
        acceptance_due = self._due(self.acceptance_days)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO appeals(appeal_id,decision_id,legal_ground,appellant_id,appellant_kind,authorization_id,"
                    "statement,state,merge_count,registered_at,updated_at) VALUES(?,?,?,?,?,?,?,'registered',0,?,?)",
                    (appeal_id, decision_id, legal_ground, actor["user_id"], appellant_kind, authorization_id, statement, now, now),
                )
                for material in materials:
                    self._insert_material(appeal_id, material.kind, material.content, "initial", actor["user_id"], now)
                self._create_deadline(appeal_id, "acceptance", self.acceptance_days)
                self._audit(
                    "appeal", appeal_id, "appeal.registered", actor["user_id"],
                    {"decision_id": decision_id, "legal_ground": legal_ground, "appellant_kind": appellant_kind,
                     "authorization_id": authorization_id, "materials": len(materials)},
                )
        except sqlite3.IntegrityError:
            # 同一法定事由已有在办案件:合并而不是生成新案
            return self._merge_appeal(actor, decision, legal_ground, statement, materials)
        return {"appeal_id": appeal_id, "merged": False, "state": "registered", "acceptance_due_at": acceptance_due}

    def _insert_material(self, appeal_id: str, kind: str, content: str, source: str, submitted_by: str, submitted_at: str) -> None:
        self.connection.execute(
            "INSERT INTO appeal_materials(appeal_id,kind,content,source,submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
            (appeal_id, kind, content, source, submitted_by, submitted_at),
        )

    def _merge_appeal(self, actor, decision, legal_ground, statement, materials) -> dict[str, Any]:
        existing = self.connection.execute(
            "SELECT * FROM appeals WHERE decision_id=? AND legal_ground=? AND state IN ('registered','correcting','accepted')",
            (decision["decision_id"], legal_ground),
        ).fetchone()
        if existing is None:
            raise Conflict("申诉登记冲突,请查询后重试")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self._insert_material(existing["appeal_id"], "statement", statement, "merged", actor["user_id"], now)
            for material in materials:
                self._insert_material(existing["appeal_id"], material.kind, material.content, "merged", actor["user_id"], now)
            self.connection.execute(
                "UPDATE appeals SET merge_count=merge_count+1,updated_at=? WHERE appeal_id=?",
                (now, existing["appeal_id"]),
            )
            self._audit(
                "appeal", existing["appeal_id"], "appeal.merged", actor["user_id"],
                {"legal_ground": legal_ground, "merged_materials": 1 + len(materials)},
            )
        return {
            "appeal_id": existing["appeal_id"],
            "merged": True,
            "merge_count": existing["merge_count"] + 1,
            "state": existing["state"],
        }

    # ------------------------------------------------------------------
    # 期限管理
    # ------------------------------------------------------------------

    def _create_deadline(self, appeal_id: str, kind: str, days: int) -> None:
        self.connection.execute(
            "INSERT INTO appeal_deadlines(appeal_id,kind,due_at,status) VALUES(?,?,?,'open')",
            (appeal_id, kind, self._due(days)),
        )

    def _open_deadline(self, appeal_id: str, kind: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM appeal_deadlines WHERE appeal_id=? AND kind=? AND status='open' "
            "ORDER BY deadline_id DESC LIMIT 1",
            (appeal_id, kind),
        ).fetchone()

    def _void_open_deadlines(self, appeal_id: str) -> None:
        self.connection.execute(
            "UPDATE appeal_deadlines SET status='void',completed_at=? WHERE appeal_id=? AND status='open'",
            (self._now(), appeal_id),
        )

    def _finish_deadline(self, appeal_id: str, kind: str, overdue_reason: str | None) -> str | None:
        """完成指定期限;已逾期时必须记录逾期原因,返回实际记录的逾期原因。"""
        deadline = self._open_deadline(appeal_id, kind)
        if deadline is None:
            return None
        if parse_utc(self._now()) > parse_utc(deadline["due_at"]):
            reason = (overdue_reason or "").strip()
            if not reason:
                raise ValidationFailed("已超过办理期限,必须说明逾期原因")
            self.connection.execute(
                "UPDATE appeal_deadlines SET status='met',completed_at=?,overdue_reason=? WHERE deadline_id=?",
                (self._now(), reason, deadline["deadline_id"]),
            )
            return reason
        self.connection.execute(
            "UPDATE appeal_deadlines SET status='met',completed_at=? WHERE deadline_id=?",
            (self._now(), deadline["deadline_id"]),
        )
        return None

    # ------------------------------------------------------------------
    # 材料补正
    # ------------------------------------------------------------------

    def request_material_correction(self, actor_id: str, appeal_id: str, required_items: str, reason: str) -> dict[str, Any]:
        """承办人要求补正材料,补正期间受理审查期限重新起算。"""
        actor = self._require(actor_id, "correction.request")
        required_items = require_text(required_items, "补正内容")
        reason = require_text(reason, "补正理由")
        with transaction(self.connection, immediate=True):
            appeal = self._appeal_row(appeal_id)
            if appeal["state"] != "registered":
                raise InvalidState("只有已登记待审查的申诉可以要求补正")
            due_at = self._due(self.correction_days)
            cursor = self.connection.execute(
                "INSERT INTO material_corrections(appeal_id,required_items,reason,due_at,status,created_by,created_at) "
                "VALUES(?,?,?,?,'pending',?,?)",
                (appeal_id, required_items, reason, due_at, actor["user_id"], self._now()),
            )
            self._void_open_deadlines(appeal_id)
            self._create_deadline(appeal_id, "correction", self.correction_days)
            self.connection.execute(
                "UPDATE appeals SET state='correcting',updated_at=? WHERE appeal_id=?",
                (self._now(), appeal_id),
            )
            self._audit("appeal", appeal_id, "appeal.correction_requested", actor["user_id"], {"required_items": required_items, "due_at": due_at})
        return {"appeal_id": appeal_id, "state": "correcting", "correction_id": int(cursor.lastrowid), "due_at": due_at}

    def _check_appellant(self, actor: sqlite3.Row, appeal: sqlite3.Row) -> None:
        if actor["user_id"] not in {appeal["appellant_id"], appeal["party_id"]}:
            raise Forbidden("只有申诉提交人或当事人本人可以执行该操作")

    def submit_supplement(self, actor_id: str, appeal_id: str, raw_materials: Any) -> dict[str, Any]:
        """当事人或代理人提交补正材料;逾期未补正的申诉不予受理。"""
        actor = self._require(actor_id, "material.supplement")
        materials = parse_materials(raw_materials)
        if not materials:
            raise ValidationFailed("补正材料不能为空")
        with transaction(self.connection, immediate=True):
            appeal = self._appeal_with_decision(appeal_id)
            if appeal["state"] != "correcting":
                raise InvalidState("申诉当前不在补正阶段")
            self._check_appellant(actor, appeal)
            correction = self.connection.execute(
                "SELECT * FROM material_corrections WHERE appeal_id=? AND status='pending' ORDER BY correction_id DESC LIMIT 1",
                (appeal_id,),
            ).fetchone()
            if correction is None:
                raise InvalidState("没有待补正的通知")
            now = self._now()
            if parse_utc(now) > parse_utc(correction["due_at"]):
                self.connection.execute(
                    "UPDATE material_corrections SET status='expired' WHERE correction_id=?",
                    (correction["correction_id"],),
                )
                deadline = self._open_deadline(appeal_id, "correction")
                if deadline is not None:
                    self.connection.execute(
                        "UPDATE appeal_deadlines SET status='overdue',completed_at=?,overdue_reason=? WHERE deadline_id=?",
                        (now, "当事人逾期未补正材料", deadline["deadline_id"]),
                    )
                self._void_open_deadlines(appeal_id)
                self.connection.execute(
                    "UPDATE appeals SET state='rejected',updated_at=?,closed_at=? WHERE appeal_id=?",
                    (now, now, appeal_id),
                )
                self._audit("appeal", appeal_id, "appeal.rejected", actor["user_id"], {"reason": "逾期未补正材料"})
                return {"appeal_id": appeal_id, "state": "rejected", "reason": "逾期未补正材料"}
            for material in materials:
                self._insert_material(appeal_id, material.kind, material.content, "supplement", actor["user_id"], now)
            self.connection.execute(
                "UPDATE material_corrections SET status='fulfilled',fulfilled_at=? WHERE correction_id=?",
                (now, correction["correction_id"]),
            )
            deadline = self._open_deadline(appeal_id, "correction")
            if deadline is not None:
                self.connection.execute(
                    "UPDATE appeal_deadlines SET status='met',completed_at=? WHERE deadline_id=?",
                    (now, deadline["deadline_id"]),
                )
            self._create_deadline(appeal_id, "acceptance", self.acceptance_days)
            acceptance_due = self._due(self.acceptance_days)
            self.connection.execute(
                "UPDATE appeals SET state='registered',updated_at=? WHERE appeal_id=?",
                (now, appeal_id),
            )
            self._audit("appeal", appeal_id, "appeal.supplemented", actor["user_id"], {"materials": len(materials)})
        return {"appeal_id": appeal_id, "state": "registered", "acceptance_due_at": acceptance_due}

    # ------------------------------------------------------------------
    # 受理审查与复核决定
    # ------------------------------------------------------------------

    def _check_reviewer(self, actor: sqlite3.Row, appeal: sqlite3.Row) -> None:
        if actor["user_id"] == appeal["decision_issued_by"]:
            raise Forbidden("原处罚决定承办人应当回避,不能审查该申诉")
        recused = self.connection.execute(
            "SELECT 1 FROM appeal_recusals WHERE appeal_id=? AND reviewer_id=?",
            (appeal["appeal_id"], actor["user_id"]),
        ).fetchone()
        if recused is not None:
            raise Forbidden("审查人员已登记回避该申诉")

    def review_acceptance(self, actor_id: str, appeal_id: str, accept: bool, reason: str, overdue_reason: str | None = None) -> dict[str, Any]:
        """受理审查:受理则中止执行动作并挂起台账,不予受理则结案。"""
        actor = self._require(actor_id, "acceptance.review")
        reason = require_text(reason, "审查意见")
        with transaction(self.connection, immediate=True):
            appeal = self._appeal_with_decision(appeal_id)
            if appeal["state"] != "registered":
                raise InvalidState("申诉不在受理审查阶段")
            self._check_reviewer(actor, appeal)
            recorded_overdue = self._finish_deadline(appeal_id, "acceptance", overdue_reason)
            now = self._now()
            conclusion = "accept" if accept else "reject"
            self.connection.execute(
                "INSERT INTO appeal_reviews(appeal_id,stage,conclusion,reason,reviewer_id,reviewed_at,overdue_reason) "
                "VALUES(?,'acceptance',?,?,?,?,?)",
                (appeal_id, conclusion, reason, actor["user_id"], now, recorded_overdue),
            )
            if accept:
                self._create_deadline(appeal_id, "reconsideration", self.reconsideration_days)
                self.connection.execute(
                    "UPDATE enforcement_actions SET status='suspended',suspended_by_appeal_id=?,updated_at=? "
                    "WHERE decision_id=? AND status='active'",
                    (appeal_id, now, appeal["decision_id"]),
                )
                self.connection.execute(
                    "UPDATE ledger_entries SET status='suspended' WHERE decision_id=? AND status='outstanding'",
                    (appeal["decision_id"],),
                )
                self.connection.execute(
                    "UPDATE penalty_decisions SET status='suspended',revision=revision+1 WHERE decision_id=?",
                    (appeal["decision_id"],),
                )
                self.connection.execute(
                    "UPDATE appeals SET state='accepted',updated_at=? WHERE appeal_id=?",
                    (now, appeal_id),
                )
                self._audit("appeal", appeal_id, "appeal.accepted", actor["user_id"], {"reason": reason, "overdue_reason": recorded_overdue})
            else:
                self.connection.execute(
                    "UPDATE appeals SET state='rejected',updated_at=?,closed_at=? WHERE appeal_id=?",
                    (now, now, appeal_id),
                )
                self._audit("appeal", appeal_id, "appeal.rejected", actor["user_id"], {"reason": reason, "overdue_reason": recorded_overdue})
        state = "accepted" if accept else "rejected"
        result: dict[str, Any] = {"appeal_id": appeal_id, "state": state}
        if accept:
            result["reconsideration_due_at"] = self._due(self.reconsideration_days)
        return result

    def decide_reconsideration(self, actor_id: str, appeal_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """复核决定:驳回(维持)、变更或撤销,台账与执行动作在同一事务内恢复或调整。"""
        actor = self._require(actor_id, "reconsideration.decide")
        conclusion = raw.get("conclusion")
        if conclusion not in RECONSIDERATION_CONCLUSIONS:
            raise ValidationFailed(f"复核结论必须是: {', '.join(RECONSIDERATION_CONCLUSIONS)}")
        reason = require_text(raw.get("reason"), "决定理由")
        citations = parse_evidence_refs(raw.get("evidence_citations"), "引用证据")
        if not citations:
            raise ValidationFailed("复核决定必须引用证据版本")
        new_amount = None
        if conclusion == "modify":
            new_amount = parse_money(raw.get("new_fine_amount_cny"), "变更后罚款金额")
        with transaction(self.connection, immediate=True):
            appeal = self._appeal_with_decision(appeal_id)
            if appeal["state"] != "accepted":
                raise InvalidState("申诉未受理,不能作出复核决定")
            self._check_reviewer(actor, appeal)
            recorded_overdue = self._finish_deadline(appeal_id, "reconsideration", raw.get("overdue_reason"))
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO appeal_reviews(appeal_id,stage,conclusion,reason,new_fine_amount_cny,reviewer_id,reviewed_at,overdue_reason) "
                "VALUES(?,'reconsideration',?,?,?,?,?,?)",
                (appeal_id, conclusion, reason, new_amount, actor["user_id"], now, recorded_overdue),
            )
            review_id = int(cursor.lastrowid)
            for ref in citations:
                self.connection.execute(
                    "INSERT INTO review_evidence_citations(review_id,evidence_id,evidence_version) VALUES(?,?,?)",
                    (review_id, ref.evidence_id, ref.evidence_version),
                )
            decision_id = appeal["decision_id"]
            if conclusion == "uphold":
                self._resume_enforcement(decision_id, now)
                self.connection.execute(
                    "UPDATE ledger_entries SET status='outstanding' WHERE decision_id=? AND status='suspended'",
                    (decision_id,),
                )
                self.connection.execute(
                    "UPDATE penalty_decisions SET status='active',revision=revision+1 WHERE decision_id=?",
                    (decision_id,),
                )
            elif conclusion == "modify":
                old_fine = self.connection.execute(
                    "SELECT entry_id FROM ledger_entries WHERE decision_id=? AND kind='fine' AND status='suspended' "
                    "ORDER BY entry_id DESC LIMIT 1",
                    (decision_id,),
                ).fetchone()
                if old_fine is not None:
                    self.connection.execute(
                        "UPDATE ledger_entries SET status='adjusted' WHERE entry_id=?",
                        (old_fine["entry_id"],),
                    )
                self.connection.execute(
                    "INSERT INTO ledger_entries(decision_id,kind,amount_cny,status,supersedes_entry_id,source,appeal_id,created_at) "
                    "VALUES(?,'fine',?,'outstanding',?,'review_modify',?,?)",
                    (decision_id, new_amount, None if old_fine is None else old_fine["entry_id"], appeal_id, now),
                )
                self.connection.execute(
                    "UPDATE ledger_entries SET status='outstanding' WHERE decision_id=? AND status='suspended'",
                    (decision_id,),
                )
                self._resume_enforcement(decision_id, now)
                self.connection.execute(
                    "UPDATE penalty_decisions SET status='modified',fine_amount_cny=?,revision=revision+1 WHERE decision_id=?",
                    (new_amount, decision_id),
                )
            else:  # revoke
                self.connection.execute(
                    "UPDATE ledger_entries SET status='waived' WHERE decision_id=? AND status IN ('outstanding','suspended')",
                    (decision_id,),
                )
                self.connection.execute(
                    "UPDATE enforcement_actions SET status='cancelled',updated_at=? WHERE decision_id=? AND status IN ('active','suspended')",
                    (now, decision_id),
                )
                self.connection.execute(
                    "UPDATE penalty_decisions SET status='revoked',revision=revision+1 WHERE decision_id=?",
                    (decision_id,),
                )
            self.connection.execute(
                "UPDATE appeals SET state='decided',updated_at=?,closed_at=? WHERE appeal_id=?",
                (now, now, appeal_id),
            )
            self._audit(
                "appeal", appeal_id, "appeal.decided", actor["user_id"],
                {"review_id": review_id, "conclusion": conclusion, "new_fine_amount_cny": new_amount,
                 "overdue_reason": recorded_overdue},
            )
        return {"appeal_id": appeal_id, "state": "decided", "conclusion": conclusion, "review_id": review_id}

    def _resume_enforcement(self, decision_id: str, now: str) -> None:
        self.connection.execute(
            "UPDATE enforcement_actions SET status='active',suspended_by_appeal_id=NULL,updated_at=? "
            "WHERE decision_id=? AND status='suspended'",
            (now, decision_id),
        )

    # ------------------------------------------------------------------
    # 撤回、回避与送达
    # ------------------------------------------------------------------

    def withdraw_appeal(self, actor_id: str, appeal_id: str, reason: str) -> dict[str, Any]:
        """撤回申诉;已受理的在同一事务内恢复执行动作与台账。"""
        actor = self._require(actor_id, "appeal.withdraw")
        reason = require_text(reason, "撤回原因")
        with transaction(self.connection, immediate=True):
            appeal = self._appeal_with_decision(appeal_id)
            if appeal["state"] not in OPEN_STATES:
                raise InvalidState("只有进行中的申诉可以撤回")
            self._check_appellant(actor, appeal)
            now = self._now()
            if appeal["state"] == "correcting":
                self.connection.execute(
                    "UPDATE material_corrections SET status='cancelled' WHERE appeal_id=? AND status='pending'",
                    (appeal_id,),
                )
            if appeal["state"] == "accepted":
                self._resume_enforcement(appeal["decision_id"], now)
                self.connection.execute(
                    "UPDATE ledger_entries SET status='outstanding' WHERE decision_id=? AND status='suspended'",
                    (appeal["decision_id"],),
                )
                self.connection.execute(
                    "UPDATE penalty_decisions SET status='active',revision=revision+1 WHERE decision_id=?",
                    (appeal["decision_id"],),
                )
            self._void_open_deadlines(appeal_id)
            self.connection.execute(
                "UPDATE appeals SET state='withdrawn',updated_at=?,closed_at=? WHERE appeal_id=?",
                (now, now, appeal_id),
            )
            self._audit("appeal", appeal_id, "appeal.withdrawn", actor["user_id"], {"reason": reason})
        return {"appeal_id": appeal_id, "state": "withdrawn"}

    def recuse_reviewer(self, actor_id: str, appeal_id: str, reason: str) -> dict[str, Any]:
        """审查人员登记回避,登记后不能再审查该申诉。"""
        actor = self._require(actor_id, "recusal.record")
        reason = require_text(reason, "回避原因")
        self._appeal_row(appeal_id)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO appeal_recusals(appeal_id,reviewer_id,reason,created_at) VALUES(?,?,?,?)",
                    (appeal_id, actor["user_id"], reason, self._now()),
                )
                self._audit("appeal", appeal_id, "appeal.recused", actor["user_id"], {"reason": reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该审查人员已登记回避") from exc
        return {"recusal_id": int(cursor.lastrowid), "appeal_id": appeal_id, "reviewer_id": actor["user_id"]}

    def record_delivery(self, actor_id: str, appeal_id: str, method: str, recipient: str) -> dict[str, Any]:
        """登记复核决定的最终送达,每件申诉只登记一次。"""
        actor = self._require(actor_id, "delivery.record")
        if method not in DELIVERY_METHODS:
            raise ValidationFailed(f"送达方式必须是: {', '.join(DELIVERY_METHODS)}")
        recipient = require_text(recipient, "收件人")
        with transaction(self.connection, immediate=True):
            appeal = self._appeal_row(appeal_id)
            if appeal["state"] != "decided":
                raise InvalidState("复核决定作出后才能登记送达")
            try:
                cursor = self.connection.execute(
                    "INSERT INTO appeal_deliveries(appeal_id,method,recipient,delivered_by,delivered_at) VALUES(?,?,?,?,?)",
                    (appeal_id, method, recipient, actor["user_id"], self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该申诉已登记最终送达") from exc
            self._audit("appeal", appeal_id, "appeal.delivered", actor["user_id"], {"method": method, "recipient": recipient})
        return {"delivery_id": int(cursor.lastrowid), "appeal_id": appeal_id, "method": method, "recipient": recipient}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def appeal_detail(self, actor_id: str, appeal_id: str) -> dict[str, Any]:
        self._require(actor_id, "appeal.read")
        appeal = self._appeal_with_decision(appeal_id)
        review_rows = rows(
            self.connection,
            "SELECT * FROM appeal_reviews WHERE appeal_id=? ORDER BY review_id",
            (appeal_id,),
        )
        for review in review_rows:
            review["evidence_citations"] = rows(
                self.connection,
                "SELECT evidence_id,evidence_version FROM review_evidence_citations WHERE review_id=? ORDER BY evidence_id",
                (review["review_id"],),
            )
        delivery = self.connection.execute(
            "SELECT * FROM appeal_deliveries WHERE appeal_id=?", (appeal_id,)
        ).fetchone()
        return {
            "appeal": dict(appeal),
            "materials": rows(self.connection, "SELECT * FROM appeal_materials WHERE appeal_id=? ORDER BY material_id", (appeal_id,)),
            "corrections": rows(self.connection, "SELECT * FROM material_corrections WHERE appeal_id=? ORDER BY correction_id", (appeal_id,)),
            "reviews": review_rows,
            "deadlines": rows(self.connection, "SELECT * FROM appeal_deadlines WHERE appeal_id=? ORDER BY deadline_id", (appeal_id,)),
            "recusals": rows(self.connection, "SELECT * FROM appeal_recusals WHERE appeal_id=? ORDER BY recusal_id", (appeal_id,)),
            "delivery": None if delivery is None else dict(delivery),
        }

    def overdue_report(self, actor_id: str) -> dict[str, Any]:
        """逾期查询:当前已逾期未办结的期限,以及已记录逾期原因的期限。"""
        self._require(actor_id, "appeal.read")
        now = self.clock.now()
        currently_overdue = []
        for row in rows(self.connection, "SELECT * FROM appeal_deadlines WHERE status='open' ORDER BY due_at"):
            if parse_utc(row["due_at"]) < now:
                currently_overdue.append(row)
        return {
            "now": self._now(),
            "currently_overdue": currently_overdue,
            "recorded": rows(
                self.connection,
                "SELECT * FROM appeal_deadlines WHERE overdue_reason IS NOT NULL ORDER BY deadline_id",
            ),
        }

    def recusals(self, actor_id: str, appeal_id: str | None = None, reviewer_id: str | None = None) -> list[dict[str, Any]]:
        self._require(actor_id, "appeal.read")
        query = "SELECT * FROM appeal_recusals"
        clauses, args = [], []
        if appeal_id is not None:
            clauses.append("appeal_id=?")
            args.append(appeal_id)
        if reviewer_id is not None:
            clauses.append("reviewer_id=?")
            args.append(reviewer_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return rows(self.connection, query + " ORDER BY recusal_id", tuple(args))

    def review_evidence(self, actor_id: str, appeal_id: str) -> list[dict[str, Any]]:
        """复核决定引用的证据版本。"""
        self._require(actor_id, "appeal.read")
        self._appeal_row(appeal_id)
        return rows(
            self.connection,
            "SELECT c.review_id,c.evidence_id,c.evidence_version FROM review_evidence_citations c "
            "JOIN appeal_reviews r ON r.review_id=c.review_id "
            "WHERE r.appeal_id=? AND r.stage='reconsideration' ORDER BY c.evidence_id",
            (appeal_id,),
        )

    def delivery(self, actor_id: str, appeal_id: str) -> dict[str, Any]:
        self._require(actor_id, "appeal.read")
        self._appeal_row(appeal_id)
        row = self.connection.execute(
            "SELECT * FROM appeal_deliveries WHERE appeal_id=?", (appeal_id,)
        ).fetchone()
        if row is None:
            raise NotFound("该申诉尚未登记送达")
        return dict(row)

    def audit_events(self, actor_id: str, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        return rows(
            self.connection,
            "SELECT * FROM appeal_audit_events WHERE entity_type=? AND entity_id=? ORDER BY event_id",
            (entity_type, entity_id),
        )
