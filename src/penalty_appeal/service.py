"""处罚申诉登记、补正、受理、复核、撤回与执行联动的领域用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any, Mapping

from .clock import SystemClock, isoformat, parse_utc
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json
from .models import (
    ACTION_TYPES,
    SERVICE_METHODS,
    identifier,
    legal_ground,
    material_list,
    optional_text,
    required_text,
    sha256_text,
)
from .storage import initialize, transaction

APPLICATION_WINDOW_DAYS = 60
ACCEPTANCE_REVIEW_DAYS = 5
REVIEW_WINDOW_DAYS = 60
DEFAULT_CURE_DAYS = 7

ROLE_PERMISSIONS = {
    "clerk": {
        "party.write", "decision.write", "appeal.register", "correction.issue",
        "acceptance.review", "reviewer.assign", "service.record", "overdue.explain",
        "report.read",
    },
    "reviewer": {
        "report.read", "review.decide", "recusal.request",
    },
    "auditor": {"report.read", "audit.read"},
    "admin": {
        "party.write", "decision.write", "rule.write", "reviewer.assign",
        "recusal.decide", "service.record", "overdue.explain", "report.read", "audit.read",
    },
}

# 各阶段与其期限字段，供逾期扫描复用
_STAGE_DEADLINE = {
    "acceptance": "acceptance_due_at",
    "correction": "cure_deadline",
    "review": "review_due_at",
}
_STAGE_ACTIVE_STATUS = {
    "acceptance": ("registered",),
    "correction": ("materials_pending",),
    "review": ("accepted", "in_review"),
}


class PenaltyAppealService:
    """在单个 SQLite 连接上提供处罚申诉全部业务操作，期限统一由注入时钟决定。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础辅助 -----

    def bootstrap(self, admin_id: str = "admin", display_name: str = "系统管理员") -> dict[str, Any]:
        """建立初始管理员；库中已有任意工作人员时为幂等空操作。"""
        existing = self.connection.execute("SELECT COUNT(*) AS n FROM pa_users").fetchone()["n"]
        if existing:
            return {"user_id": admin_id, "role": "admin", "created": False}
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO pa_users(user_id,display_name,role,created_at) VALUES(?,?,'admin',?)",
                (admin_id, display_name, self._now_text()),
            )
        return {"user_id": admin_id, "role": "admin", "created": True}

    def _now_text(self) -> str:
        return isoformat(self.clock.now())

    def _now(self):
        return self.clock.now()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM pa_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"工作人员不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("工作人员已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
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
        previous = self.connection.execute(
            "SELECT event_hash FROM appeal_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now_text(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO appeal_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id, canonical_json(payload),
                previous_hash, event_hash, body["created_at"],
            ),
        )

    # ----- 工作人员、当事人与授权 -----

    def create_user(self, actor_id: str, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        if actor["role"] != "admin":
            raise Forbidden("只有管理员可以建立工作人员")
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("工作人员编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO pa_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now_text()),
                )
                self._audit("user", user_id.strip(), "user.created", actor_id, {"role": role})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"工作人员已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_party(
        self, actor_id: str, party_id: str, party_type: str, name: str, id_number: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "party.write")
        if party_type not in {"individual", "enterprise"}:
            raise ValidationFailed("party_type 必须是 individual 或 enterprise")
        name = required_text(name, "name", 128)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO parties(party_id,party_type,name,id_number,created_at) VALUES(?,?,?,?,?)",
                    (identifier(party_id, "party_id"), party_type, name, id_number, self._now_text()),
                )
                self._audit("party", party_id, "party.registered", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"当事人已存在: {party_id}") from exc
        return {"party_id": party_id, "party_type": party_type, "name": name}

    def grant_authorization(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "party.write")
        authorization_id = identifier(raw.get("authorization_id"), "authorization_id")
        party_id = identifier(raw.get("party_id"), "party_id")
        agent_party_id = identifier(raw.get("agent_party_id"), "agent_party_id")
        power = required_text(raw.get("power"), "power", 16)
        if power not in {"register", "withdraw", "full"}:
            raise ValidationFailed("power 必须是 register、withdraw 或 full")
        if party_id == agent_party_id:
            raise ValidationFailed("代理人不能是当事人本人")
        self._party(party_id)
        self._party(agent_party_id)
        valid_from = isoformat(parse_utc(required_text(raw.get("valid_from"), "valid_from"), "valid_from"))
        valid_to = raw.get("valid_to")
        if valid_to is not None:
            valid_to = isoformat(parse_utc(required_text(valid_to, "valid_to"), "valid_to"))
            if valid_to <= valid_from:
                raise ValidationFailed("valid_to 必须晚于 valid_from")
        document_sha256 = sha256_text(raw.get("document_sha256"), "document_sha256")
        scope_decision_id = raw.get("scope_decision_id")
        if scope_decision_id is not None:
            scope_decision_id = identifier(scope_decision_id, "scope_decision_id")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO party_authorizations(authorization_id,party_id,agent_party_id,scope_decision_id,"
                    "power,valid_from,valid_to,document_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (authorization_id, party_id, agent_party_id, scope_decision_id, power,
                     valid_from, valid_to, document_sha256, self._now_text()),
                )
                self._audit("authorization", authorization_id, "authorization.granted", actor_id,
                            {"party_id": party_id, "agent_party_id": agent_party_id, "power": power})
        except sqlite3.IntegrityError as exc:
            raise Conflict("授权编号冲突") from exc
        return {"authorization_id": authorization_id, "power": power}

    def _party(self, party_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM parties WHERE party_id=?", (party_id,)).fetchone()
        if row is None:
            raise NotFound(f"当事人不存在: {party_id}")
        return row

    def _valid_authorization(self, subject_party_id: str, agent_party_id: str, decision_id: str, needed_power: str) -> sqlite3.Row:
        now_text = self._now_text()
        rows = self.connection.execute(
            "SELECT * FROM party_authorizations WHERE party_id=? AND agent_party_id=? AND revoked=0 "
            "AND (scope_decision_id IS NULL OR scope_decision_id=?) ORDER BY created_at",
            (subject_party_id, agent_party_id, decision_id),
        ).fetchall()
        for row in rows:
            if row["valid_from"] > now_text:
                continue
            if row["valid_to"] is not None and row["valid_to"] < now_text:
                continue
            if row["power"] == needed_power or row["power"] == "full":
                return row
        raise Forbidden("代理人缺少覆盖该决定的有效授权")

    # ----- 处罚决定台账与执行动作 -----

    def register_decision(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        decision_id = identifier(raw.get("decision_id"), "decision_id")
        decision_number = required_text(raw.get("decision_number"), "decision_number", 64)
        subject_party_id = identifier(raw.get("subject_party_id"), "subject_party_id")
        self._party(subject_party_id)
        title = required_text(raw.get("title"), "title", 200)
        decided_by_staff = optional_text(raw.get("decided_by_staff"), "decided_by_staff", 64)
        decided_at = isoformat(parse_utc(required_text(raw.get("decided_at"), "decided_at"), "decided_at"))
        terms = raw.get("terms", [])
        if not isinstance(terms, list) or not terms:
            raise ValidationFailed("处罚决定至少包含一项处罚内容")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO penalty_decisions(decision_id,decision_number,subject_party_id,case_record_id,"
                    "title,decided_by_staff,decided_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (decision_id, decision_number, subject_party_id, raw.get("case_record_id"),
                     title, decided_by_staff, decided_at, self._now_text()),
                )
                for index, term in enumerate(terms):
                    if not isinstance(term, Mapping):
                        raise ValidationFailed(f"terms[{index}] 必须是对象")
                    action_type = required_text(term.get("action_type"), f"terms[{index}].action_type", 48)
                    if action_type not in ACTION_TYPES:
                        raise ValidationFailed(f"terms[{index}].action_type 不受支持")
                    amount = term.get("amount_text")
                    due_at = term.get("due_at")
                    if due_at is not None:
                        due_at = isoformat(parse_utc(required_text(due_at, f"terms[{index}].due_at"), "due_at"))
                    cursor = self.connection.execute(
                        "INSERT INTO penalty_terms(decision_id,action_type,amount_text,due_at,note,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (decision_id, action_type, amount, due_at, term.get("note"), self._now_text()),
                    )
                    term_id = cursor.lastrowid
                    self.connection.execute(
                        "INSERT INTO enforcement_actions(action_id,term_id,decision_id,action_type,state,"
                        "current_amount_text,current_due_at,created_at,updated_at) VALUES(?,?,?,?, 'active',?,?,?,?)",
                        (
                            f"{decision_id}:{action_type}", term_id, decision_id, action_type,
                            amount, due_at, self._now_text(), self._now_text(),
                        ),
                    )
                self._audit("penalty_decision", decision_id, "decision.registered", actor_id,
                            {"decision_number": decision_number, "terms": len(terms)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("处罚决定编号、文号或处罚内容冲突") from exc
        return self.get_decision(decision_id)

    def read_decision(self, actor_id: str, decision_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        return self.get_decision(decision_id)

    def read_appeal(self, actor_id: str, appeal_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        return self.get_appeal(appeal_id)

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM penalty_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise NotFound("处罚决定不存在")
        terms = self.connection.execute(
            "SELECT * FROM penalty_terms WHERE decision_id=? ORDER BY term_id", (decision_id,)
        ).fetchall()
        actions = self.connection.execute(
            "SELECT * FROM enforcement_actions WHERE decision_id=? ORDER BY action_id", (decision_id,)
        ).fetchall()
        return {
            "decision": dict(row),
            "terms": [dict(item) for item in terms],
            "enforcement_actions": [dict(item) for item in actions],
        }

    def list_enforcement_actions(self, actor_id: str, decision_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "report.read")
        return [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM enforcement_actions WHERE decision_id=? ORDER BY action_id", (decision_id,)
            ).fetchall()
        ]

    def set_suspension_rule(self, actor_id: str, action_type: str, suspend_on_acceptance: bool, note: str) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        if action_type not in ACTION_TYPES:
            raise ValidationFailed("action_type 不受支持")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO suspension_rules(action_type,suspend_on_acceptance,note) VALUES(?,?,?) "
                "ON CONFLICT(action_type) DO UPDATE SET suspend_on_acceptance=excluded.suspend_on_acceptance,"
                "note=excluded.note",
                (action_type, 1 if suspend_on_acceptance else 0, required_text(note, "note", 256)),
            )
            self._audit("suspension_rule", action_type, "rule.updated", actor_id,
                        {"suspend_on_acceptance": bool(suspend_on_acceptance)})
        return {"action_type": action_type, "suspend_on_acceptance": bool(suspend_on_acceptance)}

    # ----- 申诉登记与合并 -----

    def register_appeal(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "appeal.register")
        appeal_id = identifier(raw.get("appeal_id"), "appeal_id")
        decision_id = identifier(raw.get("decision_id"), "decision_id")
        ground = legal_ground(raw.get("legal_ground_code"))
        applicant_party_id = identifier(raw.get("applicant_party_id"), "applicant_party_id")
        representative_party_id = raw.get("representative_party_id")
        authorization_id = raw.get("authorization_id")
        materials = material_list(raw.get("materials"))
        evidence_refs = self._parse_evidence_refs(raw.get("evidence_refs", []))
        idempotency_key = raw.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = identifier(idempotency_key, "idempotency_key")
            request_digest = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
            cached = self._idempotent_response("appeal.register", idempotency_key, request_digest)
            if cached is not None:
                return cached
        else:
            request_digest = None

        decision = self.connection.execute(
            "SELECT * FROM penalty_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if decision is None:
            raise NotFound("处罚决定不存在")
        self._party(applicant_party_id)

        # 发起资格：当事人本人，或持有效授权（含发起/全权）的代理人
        capacity = "self"
        authorization_row = None
        if applicant_party_id == decision["subject_party_id"] and not representative_party_id:
            capacity = "self"
        elif representative_party_id:
            representative_party_id = identifier(representative_party_id, "representative_party_id")
            if applicant_party_id != representative_party_id:
                raise ValidationFailed("申请人与代理人不一致")
            authorization_row = self._valid_authorization(
                decision["subject_party_id"], representative_party_id, decision_id, "register"
            )
            if authorization_id is not None and authorization_row["authorization_id"] != authorization_id:
                raise ValidationFailed("授权编号与有效委托不匹配")
            capacity = "agent"
        else:
            raise Forbidden("只有决定当事人或其授权代理人可以发起申诉")

        now = self._now()
        now_text = isoformat(now)
        application_due_at = isoformat(parse_utc(decision["decided_at"]) + timedelta(days=APPLICATION_WINDOW_DAYS))
        beyond_window = now_text > application_due_at
        late_reason = optional_text(raw.get("late_reason"), "late_reason", 512)
        if beyond_window and not late_reason:
            raise ValidationFailed("超过法定申请期限，必须说明逾期原因")
        acceptance_due_at = isoformat(now + timedelta(days=ACCEPTANCE_REVIEW_DAYS))

        try:
            with transaction(self.connection, immediate=True):
                # 同一决定、同一法定事由存在未终结申诉时合并，而不是生成多案
                existing = self.connection.execute(
                    "SELECT appeal_id FROM appeals WHERE decision_id=? AND legal_ground_code=? "
                    "AND status IN ('registered','materials_pending','accepted','in_review')",
                    (decision_id, ground),
                ).fetchone()
                if existing is not None:
                    merged_id = existing["appeal_id"]
                    self._merge_applicant_and_materials(
                        merged_id, applicant_party_id, representative_party_id,
                        capacity, authorization_row, materials, evidence_refs, actor_id
                    )
                    response = {"appeal_id": merged_id, "merged": True, "status": self._appeal_status(merged_id)}
                    self._store_idempotency("appeal.register", idempotency_key, request_digest, response)
                    return response

                # 同一事由已作出终结性复核结论的，不再另立新案
                terminal = self.connection.execute(
                    "SELECT status FROM appeals WHERE decision_id=? AND legal_ground_code=? "
                    "AND status IN ('decided','served')",
                    (decision_id, ground),
                ).fetchone()
                if terminal is not None:
                    raise Conflict("同一决定同一法定事由已有复核结论，不能重复提交")

                self.connection.execute(
                    "INSERT INTO appeals(appeal_id,decision_id,legal_ground_code,status,application_due_at,"
                    "beyond_window,late_reason,registered_by_party_id,registered_by_staff,acceptance_due_at,"
                    "idempotency_key,revision,created_at,updated_at) "
                    "VALUES(?,?,?, 'registered',?,?,?,?,?,?,?,1,?,?)",
                    (
                        appeal_id, decision_id, ground, application_due_at,
                        1 if beyond_window else 0, late_reason, applicant_party_id, actor_id,
                        acceptance_due_at, idempotency_key, now_text, now_text,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO appeal_applicants(appeal_id,party_id,representative_party_id,capacity,"
                    "authorization_id,joined_at) VALUES(?,?,?,?,?,?)",
                    (
                        appeal_id, applicant_party_id,
                        representative_party_id if capacity == "agent" else None,
                        capacity,
                        None if authorization_row is None else authorization_row["authorization_id"],
                        now_text,
                    ),
                )
                self._insert_materials(appeal_id, materials, applicant_party_id, now_text)
                self._insert_evidence_refs(appeal_id, evidence_refs, applicant_party_id, now_text)
                self._audit("appeal", appeal_id, "appeal.registered", actor_id, {
                    "decision_id": decision_id,
                    "legal_ground_code": ground,
                    "applicant_party_id": applicant_party_id,
                    "capacity": capacity,
                    "beyond_window": beyond_window,
                    "materials": len(materials),
                })
                response = {
                    "appeal_id": appeal_id,
                    "merged": False,
                    "status": "registered",
                    "application_due_at": application_due_at,
                    "acceptance_due_at": acceptance_due_at,
                    "beyond_window": beyond_window,
                }
                self._store_idempotency("appeal.register", idempotency_key, request_digest, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("申诉编号或幂等键冲突") from exc
        return response

    def _merge_applicant_and_materials(
        self, appeal_id, applicant_party_id, representative_party_id, capacity,
        authorization_row, materials, evidence_refs, actor_id
    ) -> None:
        now_text = self._now_text()
        self.connection.execute(
            "INSERT INTO appeal_applicants(appeal_id,party_id,representative_party_id,capacity,"
            "authorization_id,joined_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(appeal_id,party_id) DO NOTHING",
            (
                appeal_id, applicant_party_id,
                representative_party_id if capacity == "agent" else None,
                capacity, None if authorization_row is None else authorization_row["authorization_id"],
                now_text,
            ),
        )
        self._insert_materials(appeal_id, materials, applicant_party_id, now_text)
        self._insert_evidence_refs(appeal_id, evidence_refs, applicant_party_id, now_text)
        self._audit("appeal", appeal_id, "appeal.merged", actor_id, {
            "applicant_party_id": applicant_party_id, "materials": len(materials)
        })

    def _insert_materials(self, appeal_id: str, materials, party_id: str, now_text: str) -> None:
        for material in materials:
            exists = self.connection.execute(
                "SELECT 1 FROM appeal_materials WHERE appeal_id=? AND content_sha256=?",
                (appeal_id, material["content_sha256"]),
            ).fetchone()
            if exists is not None:
                continue
            self.connection.execute(
                "INSERT INTO appeal_materials(material_id,appeal_id,title,kind,content_sha256,"
                "submitted_by_party_id,submitted_at) VALUES(?,?,?,?,?,?,?)",
                (material["material_id"], appeal_id, material["title"], material["kind"],
                 material["content_sha256"], party_id, now_text),
            )

    def _insert_evidence_refs(self, appeal_id: str, refs, party_id: str, now_text: str) -> None:
        for ref in refs:
            self.connection.execute(
                "INSERT INTO appeal_evidence_refs(appeal_id,evidence_id,evidence_version,title,"
                "content_sha256,submitted_by_party_id,created_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(appeal_id,evidence_id,evidence_version) DO NOTHING",
                (appeal_id, ref["evidence_id"], ref["evidence_version"], ref["title"],
                 ref["content_sha256"], party_id, now_text),
            )

    @staticmethod
    def _parse_evidence_refs(raw: Any) -> tuple[dict[str, str], ...]:
        if not isinstance(raw, list):
            raise ValidationFailed("evidence_refs 必须是数组")
        refs: list[dict[str, str]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"evidence_refs[{index}] 必须是对象")
            refs.append({
                "evidence_id": identifier(item.get("evidence_id"), f"evidence_refs[{index}].evidence_id"),
                "evidence_version": required_text(item.get("evidence_version"), f"evidence_refs[{index}].evidence_version", 64),
                "title": required_text(item.get("title"), f"evidence_refs[{index}].title", 200),
                "content_sha256": sha256_text(item.get("content_sha256"), f"evidence_refs[{index}].content_sha256"),
            })
        return tuple(refs)

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        if not key:
            return None
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM appeal_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同申诉内容")
        return json.loads(row["response_json"])

    def _store_idempotency(self, scope: str, key: str | None, request_digest: str | None, response: Mapping[str, Any]) -> None:
        if not key:
            return
        self.connection.execute(
            "INSERT INTO appeal_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(scope,idempotency_key) DO NOTHING",
            (scope, key, request_digest, canonical_json(response), self._now_text()),
        )

    def _appeal_status(self, appeal_id: str) -> str:
        row = self.connection.execute("SELECT status FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
        if row is None:
            raise NotFound("申诉不存在")
        return row["status"]

    # ----- 材料补正 -----

    def issue_correction_notice(
        self, actor_id: str, appeal_id: str, required_items: list[str], cure_days: int = DEFAULT_CURE_DAYS, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "correction.issue")
        if not isinstance(required_items, list) or not required_items or any(
            not isinstance(item, str) or not item.strip() for item in required_items
        ):
            raise ValidationFailed("补正事项至少包含一项文本")
        if not isinstance(cure_days, int) or not 1 <= cure_days <= 30:
            raise ValidationFailed("cure_days 必须是 1 到 30 的整数")
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            if row["status"] != "registered":
                raise InvalidState("只有已登记待受理的申诉可以发出补正通知")
            self._gate_overdue(row, "acceptance")
            cure_deadline = isoformat(self._now() + timedelta(days=cure_days))
            cursor = self.connection.execute(
                "INSERT INTO material_corrections(appeal_id,required_items_json,cure_deadline,note,"
                "issued_by,created_at) VALUES(?,?,?,?,?,?)",
                (appeal_id, canonical_json(required_items), cure_deadline, note, actor_id, self._now_text()),
            )
            self.connection.execute(
                "UPDATE appeals SET status='materials_pending',cure_deadline=?,revision=revision+1,updated_at=? "
                "WHERE appeal_id=?",
                (cure_deadline, self._now_text(), appeal_id),
            )
            self._audit("appeal", appeal_id, "correction.issued", actor_id,
                        {"correction_id": cursor.lastrowid, "cure_deadline": cure_deadline})
        return self.get_appeal(appeal_id)

    def cure_resubmit(self, actor_id: str, appeal_id: str, materials_raw: list[Mapping[str, Any]]) -> dict[str, Any]:
        self._require(actor_id, "appeal.register")
        materials = material_list(materials_raw)
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            if row["status"] != "materials_pending":
                raise InvalidState("只有待补正的申诉可以补交材料")
            if row["cure_deadline"] is None or self._now_text() > row["cure_deadline"]:
                raise InvalidState("已超过补正期限，逾期未补正按撤回处理")
            party_id = row["registered_by_party_id"]
            self._insert_materials(appeal_id, materials, party_id, self._now_text())
            self.connection.execute(
                "UPDATE material_corrections SET cured_at=? WHERE appeal_id=? AND cured_at IS NULL",
                (self._now_text(), appeal_id),
            )
            # 补正期间不计入受理审查期限，自材料补齐之日重新起算
            new_acceptance_due = isoformat(self._now() + timedelta(days=ACCEPTANCE_REVIEW_DAYS))
            self.connection.execute(
                "UPDATE appeals SET status='registered',cure_deadline=NULL,acceptance_due_at=?,"
                "revision=revision+1,updated_at=? WHERE appeal_id=?",
                (new_acceptance_due, self._now_text(), appeal_id),
            )
            self._audit("appeal", appeal_id, "correction.cured", actor_id,
                        {"materials": len(materials), "acceptance_due_at": new_acceptance_due})
        return self.get_appeal(appeal_id)

    def close_unremedied(self, actor_id: str, appeal_id: str, note: str) -> dict[str, Any]:
        """补正期限届满仍未补交的，按撤回处理并终结。"""
        self._require(actor_id, "correction.issue")
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            if row["status"] != "materials_pending":
                raise InvalidState("只有待补正的申诉可以按未补正撤回")
            if row["cure_deadline"] is not None and self._now_text() <= row["cure_deadline"]:
                raise InvalidState("补正期限尚未届满")
            self._close_appeal(appeal_id, "withdrawn", actor_id, "appeal.deemed_withdrawn",
                               {"reason": note, "cause": "逾期未补正"})
        return self.get_appeal(appeal_id)

    # ----- 受理审查 -----

    def review_acceptance(self, actor_id: str, appeal_id: str, accept: bool, note: str) -> dict[str, Any]:
        self._require(actor_id, "acceptance.review")
        note = required_text(note, "note", 512)
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            if row["status"] != "registered":
                raise InvalidState("只有待受理申诉可以作出受理审查结论")
            self._gate_overdue(row, "acceptance")
            self.connection.execute(
                "INSERT INTO acceptance_reviews(appeal_id,reviewer_id,conclusion,note,created_at) "
                "VALUES(?,?,?,?,?)",
                (appeal_id, actor_id, "accepted" if accept else "rejected", note, self._now_text()),
            )
            if not accept:
                self._close_appeal(appeal_id, "rejected", actor_id, "acceptance.rejected", {"note": note})
                self._resolve_overdue(appeal_id, "acceptance")
                return self.get_appeal(appeal_id)

            accepted_at = self._now_text()
            review_due_at = isoformat(self._now() + timedelta(days=REVIEW_WINDOW_DAYS))
            self.connection.execute(
                "UPDATE appeals SET status='accepted',accepted_at=?,review_due_at=?,revision=revision+1,"
                "updated_at=? WHERE appeal_id=?",
                (accepted_at, review_due_at, self._now_text(), appeal_id),
            )
            self._suspend_actions(appeal_id, row["decision_id"], actor_id)
            self._resolve_overdue(appeal_id, "acceptance")
            self._resolve_overdue(appeal_id, "correction")
            self._audit("appeal", appeal_id, "acceptance.accepted", actor_id,
                        {"review_due_at": review_due_at})
        return self.get_appeal(appeal_id)

    def _suspend_actions(self, appeal_id: str, decision_id: str, actor_id: str) -> None:
        actions = self.connection.execute(
            "SELECT a.* FROM enforcement_actions a JOIN suspension_rules r ON r.action_type=a.action_type "
            "WHERE a.decision_id=? AND a.state='active' AND r.suspend_on_acceptance=1 ORDER BY a.action_id",
            (decision_id,),
        ).fetchall()
        for action in actions:
            self.connection.execute(
                "UPDATE enforcement_actions SET state='suspended',suspended_appeal_id=?,"
                "revision=revision+1,updated_at=? WHERE action_id=? AND state='active'",
                (appeal_id, self._now_text(), action["action_id"]),
            )
            self.connection.execute(
                "INSERT INTO enforcement_suspensions(appeal_id,action_id,suspended_at) VALUES(?,?,?)",
                (appeal_id, action["action_id"], self._now_text()),
            )
            self._ledger(action["action_id"], appeal_id, "suspend", actor_id,
                         amount_before=action["current_amount_text"], amount_after=action["current_amount_text"],
                         due_before=action["current_due_at"], due_after=action["current_due_at"],
                         note="受理后按规则中止执行")

    # ----- 审查人员指派与回避 -----

    def assign_reviewer(self, actor_id: str, appeal_id: str, reviewer_id: str) -> dict[str, Any]:
        self._require(actor_id, "reviewer.assign")
        reviewer = self._user(reviewer_id)
        if reviewer["role"] != "reviewer":
            raise ValidationFailed("被指派人必须具备复核角色")
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            if row["status"] not in {"accepted", "in_review"}:
                raise InvalidState("只有已受理申诉可以指派复核人员")
            decision = self.connection.execute(
                "SELECT decided_by_staff FROM penalty_decisions WHERE decision_id=?", (row["decision_id"],)
            ).fetchone()
            if decision["decided_by_staff"] == reviewer_id:
                raise Forbidden("原处罚决定承办人应当回避，不能担任复核人员")
            recused = self.connection.execute(
                "SELECT 1 FROM recusals WHERE appeal_id=? AND reviewer_id=? AND status='approved'",
                (appeal_id, reviewer_id),
            ).fetchone()
            if recused is not None:
                raise Forbidden("该复核人员已被决定回避")
            active = self.connection.execute(
                "SELECT 1 FROM reviewer_assignments WHERE appeal_id=? AND reviewer_id=? AND active=1",
                (appeal_id, reviewer_id),
            ).fetchone()
            if active is None:
                cursor = self.connection.execute(
                    "INSERT INTO reviewer_assignments(appeal_id,reviewer_id,assigned_by,created_at) "
                    "VALUES(?,?,?,?)",
                    (appeal_id, reviewer_id, actor_id, self._now_text()),
                )
                assignment_id = cursor.lastrowid
            else:
                assignment_id = None
            self.connection.execute(
                "UPDATE appeals SET status='in_review',revision=revision+1,updated_at=? WHERE appeal_id=? "
                "AND status='accepted'",
                (self._now_text(), appeal_id),
            )
            self._audit("appeal", appeal_id, "reviewer.assigned", actor_id,
                        {"reviewer_id": reviewer_id, "assignment_id": assignment_id})
        return self.get_appeal(appeal_id)

    def request_recusal(
        self, actor_id: str, appeal_id: str, reviewer_id: str, reason: str, requested_by: str
    ) -> dict[str, Any]:
        """当事人申请审查人员回避，需管理员决定；复核人员自行回避在 self_recuse 中即时生效。"""
        self._require(actor_id, "report.read")
        reason = required_text(reason, "reason", 512)
        requested_by = required_text(requested_by, "requested_by", 64)
        with transaction(self.connection, immediate=True):
            self._locked_appeal(appeal_id)
            self._user(reviewer_id)
            applicant = self.connection.execute(
                "SELECT 1 FROM appeal_applicants WHERE appeal_id=? AND party_id=?",
                (appeal_id, requested_by),
            ).fetchone()
            if applicant is None:
                raise Forbidden("只有申诉当事人可以申请回避")
            try:
                cursor = self.connection.execute(
                    "INSERT INTO recusals(appeal_id,reviewer_id,reason,requested_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (appeal_id, reviewer_id, reason, requested_by, self._now_text()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("已存在回避申请") from exc
            self._audit("appeal", appeal_id, "recusal.requested", actor_id,
                        {"recusal_id": cursor.lastrowid, "reviewer_id": reviewer_id})
        return {"recusal_id": cursor.lastrowid, "status": "pending"}

    def decide_recusal(self, actor_id: str, recusal_id: int, approve: bool, note: str) -> dict[str, Any]:
        self._require(actor_id, "recusal.decide")
        with transaction(self.connection, immediate=True):
            recusal = self.connection.execute(
                "SELECT * FROM recusals WHERE recusal_id=?", (recusal_id,)
            ).fetchone()
            if recusal is None:
                raise NotFound("回避申请不存在")
            if recusal["status"] != "pending":
                raise InvalidState("回避申请已经处理")
            self.connection.execute(
                "UPDATE recusals SET status=?,decided_by=?,decided_at=? WHERE recusal_id=?",
                ("approved" if approve else "rejected", actor_id, self._now_text(), recusal_id),
            )
            if approve:
                self.connection.execute(
                    "UPDATE reviewer_assignments SET active=0 WHERE appeal_id=? AND reviewer_id=? AND active=1",
                    (recusal["appeal_id"], recusal["reviewer_id"]),
                )
            self._audit("appeal", recusal["appeal_id"], "recusal.decided", actor_id,
                        {"recusal_id": recusal_id, "approve": approve, "note": note})
        return {"recusal_id": recusal_id, "status": "approved" if approve else "rejected"}

    def self_recuse(self, actor_id: str, appeal_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "recusal.request")
        if self._user(actor_id)["role"] != "reviewer":
            raise Forbidden("只有复核人员可以自行回避")
        with transaction(self.connection, immediate=True):
            self._locked_appeal(appeal_id)
            assignment = self.connection.execute(
                "SELECT assignment_id FROM reviewer_assignments WHERE appeal_id=? AND reviewer_id=? AND active=1",
                (appeal_id, actor_id),
            ).fetchone()
            if assignment is None:
                raise InvalidState("当前复核人员未承担该申诉")
            cursor = self.connection.execute(
                "INSERT INTO recusals(appeal_id,reviewer_id,reason,requested_by,status,decided_at,created_at) "
                "VALUES(?,?,?,?, 'approved',?,?)",
                (appeal_id, actor_id, required_text(reason, "reason", 512), actor_id,
                 self._now_text(), self._now_text()),
            )
            self.connection.execute(
                "UPDATE reviewer_assignments SET active=0 WHERE assignment_id=?",
                (assignment["assignment_id"],),
            )
            self._audit("appeal", appeal_id, "recusal.self_approved", actor_id,
                        {"recusal_id": cursor.lastrowid})
        return {"recusal_id": cursor.lastrowid, "status": "approved"}

    # ----- 复核决定与执行台账联动 -----

    def decide_review(
        self,
        actor_id: str,
        appeal_id: str,
        conclusion: str,
        reason_text: str,
        adjustments: list[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "review.decide")
        if conclusion not in {"upheld", "modified", "revoked"}:
            raise ValidationFailed("复核结论必须是 upheld、modified 或 revoked")
        reason_text = required_text(reason_text, "reason_text", 2000)
        adjustments = adjustments or []
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            if row["status"] not in {"accepted", "in_review"}:
                raise InvalidState("申诉不在可复核状态")
            self._gate_overdue(row, "review")
            assignment = self.connection.execute(
                "SELECT assignment_id FROM reviewer_assignments WHERE appeal_id=? AND reviewer_id=? AND active=1",
                (appeal_id, actor_id),
            ).fetchone()
            if assignment is None:
                raise Forbidden("复核人员未被指派或已回避")
            decision_row = self.connection.execute(
                "SELECT * FROM penalty_decisions WHERE decision_id=?", (row["decision_id"],)
            ).fetchone()
            if decision_row["decided_by_staff"] == actor_id:
                raise Forbidden("原决定承办人不能作出复核决定")

            parsed_adjustments = self._parse_adjustments(appeal_id, conclusion, adjustments)
            evidence_snapshot = [
                dict(item) for item in self.connection.execute(
                    "SELECT evidence_id,evidence_version,title,content_sha256 FROM appeal_evidence_refs "
                    "WHERE appeal_id=? ORDER BY ref_id", (appeal_id,)
                ).fetchall()
            ]
            cursor = self.connection.execute(
                "INSERT INTO review_decisions(appeal_id,conclusion,reason_text,evidence_snapshot_json,"
                "decided_by,decided_at) VALUES(?,?,?,?,?,?)",
                (appeal_id, conclusion, reason_text, canonical_json(evidence_snapshot),
                 actor_id, self._now_text()),
            )
            review_decision_id = cursor.lastrowid
            for item in parsed_adjustments:
                self.connection.execute(
                    "INSERT INTO review_decision_items(review_decision_id,term_id,action_id,adjustment,"
                    "amount_text,due_at,note) VALUES(?,?,?,?,?,?,?)",
                    (
                        review_decision_id, item.get("term_id"), item["action_id"], item["adjustment"],
                        item.get("amount_text"), item.get("due_at"), item.get("note"),
                    ),
                )
            self._apply_conclusion(appeal_id, conclusion, parsed_adjustments, actor_id)
            self.connection.execute(
                "UPDATE appeals SET status='decided',decided_at=?,closed_at=?,revision=revision+1,updated_at=? "
                "WHERE appeal_id=?",
                (self._now_text(), self._now_text(), self._now_text(), appeal_id),
            )
            self._resolve_overdue(appeal_id, "review")
            self._audit("appeal", appeal_id, "review.decided", actor_id, {
                "conclusion": conclusion,
                "review_decision_id": review_decision_id,
                "evidence_versions": [(e["evidence_id"], e["evidence_version"]) for e in evidence_snapshot],
                "adjustments": len(parsed_adjustments),
            })
        return self.get_appeal(appeal_id)

    def _parse_adjustments(self, appeal_id: str, conclusion: str, adjustments: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if conclusion == "modified" and not adjustments:
            raise ValidationFailed("变更结论必须给出台账调整项")
        if conclusion != "modified" and adjustments:
            raise ValidationFailed("只有变更结论可以携带调整项")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(adjustments):
            action_id = required_text(item.get("action_id"), f"adjustments[{index}].action_id", 96)
            kind = required_text(item.get("adjustment"), f"adjustments[{index}].adjustment", 16)
            if kind not in {"resume", "terminate", "modify"}:
                raise ValidationFailed(f"adjustments[{index}].adjustment 不受支持")
            if action_id in seen:
                raise ValidationFailed(f"adjustments[{index}] 重复调整同一执行动作")
            seen.add(action_id)
            action = self.connection.execute(
                "SELECT * FROM enforcement_actions WHERE action_id=? AND decision_id="
                "(SELECT decision_id FROM appeals WHERE appeal_id=?)",
                (action_id, appeal_id),
            ).fetchone()
            if action is None:
                raise NotFound(f"执行动作不存在: {action_id}")
            amount_text = item.get("amount_text")
            due_at = item.get("due_at")
            if due_at is not None:
                due_at = isoformat(parse_utc(required_text(due_at, f"adjustments[{index}].due_at"), "due_at"))
            if kind == "modify" and amount_text is None and due_at is None:
                raise ValidationFailed(f"adjustments[{index}] 变更必须给出新金额或新期限")
            result.append({
                "action_id": action_id,
                "term_id": action["term_id"],
                "adjustment": kind,
                "amount_text": None if amount_text is None else str(amount_text),
                "due_at": due_at,
                "note": item.get("note"),
            })
        return result

    def _apply_conclusion(self, appeal_id: str, conclusion: str, adjustments: list[dict[str, Any]], actor_id: str) -> None:
        suspended = self.connection.execute(
            "SELECT * FROM enforcement_suspensions WHERE appeal_id=? AND state='active' ORDER BY action_id",
            (appeal_id,),
        ).fetchall()
        decision_id = self.connection.execute(
            "SELECT decision_id FROM appeals WHERE appeal_id=?", (appeal_id,)
        ).fetchone()["decision_id"]
        handled: set[str] = set()

        def resume(action_id: str, note: str) -> None:
            action = self.connection.execute(
                "SELECT * FROM enforcement_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE enforcement_actions SET state='active',suspended_appeal_id=NULL,"
                "revision=revision+1,updated_at=? WHERE action_id=?",
                (self._now_text(), action_id),
            )
            self._release_suspension(appeal_id, action_id, f"resume:{conclusion}")
            self._ledger(action_id, appeal_id, "resume", actor_id,
                         amount_before=action["current_amount_text"], amount_after=action["current_amount_text"],
                         due_before=action["current_due_at"], due_after=action["current_due_at"], note=note)

        def terminate(action_id: str, note: str) -> None:
            action = self.connection.execute(
                "SELECT * FROM enforcement_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE enforcement_actions SET state='terminated',suspended_appeal_id=NULL,"
                "revision=revision+1,updated_at=? WHERE action_id=?",
                (self._now_text(), action_id),
            )
            self._release_suspension(appeal_id, action_id, f"terminate:{conclusion}")
            self._ledger(action_id, appeal_id, "terminate", actor_id,
                         amount_before=action["current_amount_text"], amount_after=action["current_amount_text"],
                         due_before=action["current_due_at"], due_after=action["current_due_at"], note=note)

        if conclusion == "upheld":
            for item in suspended:
                resume(item["action_id"], "复核维持原决定，恢复执行")
            return

        if conclusion == "revoked":
            # 撤销使原处罚整体失效：无论是否曾被中止，全部执行动作终结
            all_actions = self.connection.execute(
                "SELECT action_id FROM enforcement_actions WHERE decision_id=? AND state<>'terminated' "
                "ORDER BY action_id",
                (decision_id,),
            ).fetchall()
            for item in all_actions:
                terminate(item["action_id"], "复核撤销原处罚，终结执行")
            return

        # modified：逐项恢复/终结/调整；未在调整项中列明的被中止动作默认恢复
        for item in adjustments:
            handled.add(item["action_id"])
            if item["adjustment"] == "resume":
                resume(item["action_id"], item["note"] or "复核变更后恢复执行")
            elif item["adjustment"] == "terminate":
                terminate(item["action_id"], item["note"] or "复核变更后终结该执行动作")
            else:
                action = self.connection.execute(
                    "SELECT * FROM enforcement_actions WHERE action_id=?", (item["action_id"],)
                ).fetchone()
                self.connection.execute(
                    "UPDATE enforcement_actions SET state='active',suspended_appeal_id=NULL,"
                    "current_amount_text=COALESCE(?,current_amount_text),"
                    "current_due_at=COALESCE(?,current_due_at),revision=revision+1,updated_at=? "
                    "WHERE action_id=?",
                    (item["amount_text"], item["due_at"], self._now_text(), item["action_id"]),
                )
                self._release_suspension(appeal_id, item["action_id"], "modify")
                self._ledger(item["action_id"], appeal_id, "modify", actor_id,
                             amount_before=action["current_amount_text"],
                             amount_after=item["amount_text"] or action["current_amount_text"],
                             due_before=action["current_due_at"],
                             due_after=item["due_at"] or action["current_due_at"],
                             note=item["note"] or "复核变更处罚内容")
        for item in suspended:
            if item["action_id"] not in handled:
                resume(item["action_id"], "复核变更未涉及该动作，恢复执行")

    def _release_suspension(self, appeal_id: str, action_id: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE enforcement_suspensions SET state='released',released_at=?,release_reason=? "
            "WHERE appeal_id=? AND action_id=? AND state='active'",
            (self._now_text(), reason, appeal_id, action_id),
        )

    def _ledger(self, action_id, appeal_id, entry_type, actor_id, *, amount_before, amount_after,
                due_before, due_after, note) -> None:
        self.connection.execute(
            "INSERT INTO enforcement_ledger(action_id,appeal_id,entry_type,amount_before,amount_after,"
            "due_before,due_after,note,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (action_id, appeal_id, entry_type, amount_before, amount_after, due_before, due_after,
             note, actor_id, self._now_text()),
        )

    # ----- 撤回 -----

    def withdraw_appeal(self, actor_id: str, appeal_id: str, requesting_party_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "appeal.register")
        reason = required_text(reason, "reason", 512)
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            if row["status"] not in {"registered", "materials_pending", "accepted", "in_review"}:
                raise InvalidState("已终结的申诉不能撤回")
            applicant = self.connection.execute(
                "SELECT * FROM appeal_applicants WHERE appeal_id=? AND party_id=?",
                (appeal_id, requesting_party_id),
            ).fetchone()
            if applicant is None:
                raise Forbidden("只有申诉当事人可以申请撤回")
            if applicant["capacity"] == "agent":
                subject_party_id = self.connection.execute(
                    "SELECT subject_party_id FROM penalty_decisions WHERE decision_id=?",
                    (row["decision_id"],),
                ).fetchone()["subject_party_id"]
                self._valid_authorization(
                    subject_party_id, requesting_party_id, row["decision_id"], "withdraw"
                )
            # 受理后撤回：已中止动作随撤回恢复执行
            suspended = self.connection.execute(
                "SELECT action_id FROM enforcement_suspensions WHERE appeal_id=? AND state='active'",
                (appeal_id,),
            ).fetchall()
            for item in suspended:
                action = self.connection.execute(
                    "SELECT * FROM enforcement_actions WHERE action_id=?", (item["action_id"],)
                ).fetchone()
                self.connection.execute(
                    "UPDATE enforcement_actions SET state='active',suspended_appeal_id=NULL,"
                    "revision=revision+1,updated_at=? WHERE action_id=?",
                    (self._now_text(), item["action_id"]),
                )
                self._release_suspension(appeal_id, item["action_id"], "withdraw")
                self._ledger(item["action_id"], appeal_id, "resume", actor_id,
                             amount_before=action["current_amount_text"], amount_after=action["current_amount_text"],
                             due_before=action["current_due_at"], due_after=action["current_due_at"],
                             note="申诉撤回，恢复执行")
            self._close_appeal(appeal_id, "withdrawn", actor_id, "appeal.withdrawn",
                               {"requesting_party_id": requesting_party_id, "reason": reason})
        return self.get_appeal(appeal_id)

    # ----- 送达与逾期 -----

    def record_service(
        self, actor_id: str, appeal_id: str, stage: str, method: str,
        recipient_party_id: str, document_sha256: str
    ) -> dict[str, Any]:
        self._require(actor_id, "service.record")
        if stage not in {"registration", "correction_notice", "acceptance_notice", "final_decision"}:
            raise ValidationFailed("送达阶段不受支持")
        if method not in SERVICE_METHODS:
            raise ValidationFailed("送达方式不受支持")
        document_sha256 = sha256_text(document_sha256, "document_sha256")
        recipient = identifier(recipient_party_id, "recipient_party_id")
        self._party(recipient)
        with transaction(self.connection, immediate=True):
            row = self._locked_appeal(appeal_id)
            duplicate = self.connection.execute(
                "SELECT 1 FROM service_records WHERE appeal_id=? AND stage=?", (appeal_id, stage)
            ).fetchone()
            if duplicate is not None:
                raise Conflict("该阶段送达记录已存在")
            if stage == "final_decision" and row["status"] != "decided":
                raise InvalidState("只有已作出复核决定的申诉可以送达最终决定")
            self.connection.execute(
                "INSERT INTO service_records(appeal_id,stage,method,recipient_party_id,document_sha256,"
                "served_at,served_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (appeal_id, stage, method, recipient, document_sha256,
                 self._now_text(), actor_id, self._now_text()),
            )
            if stage == "final_decision":
                self.connection.execute(
                    "UPDATE appeals SET status='served',revision=revision+1,updated_at=? WHERE appeal_id=?",
                    (self._now_text(), appeal_id),
                )
            self._audit("appeal", appeal_id, "service.recorded", actor_id,
                        {"stage": stage, "method": method})
        return self.get_appeal(appeal_id)

    def detect_overdue(self, actor_id: str) -> dict[str, Any]:
        """按可控时钟扫描各阶段到期未结事项，登记未解释逾期记录。"""
        self._require(actor_id, "report.read")
        found: list[dict[str, Any]] = []
        now_text = self._now_text()
        with transaction(self.connection, immediate=True):
            for stage, column in _STAGE_DEADLINE.items():
                placeholders = ",".join("?" for _ in _STAGE_ACTIVE_STATUS[stage])
                candidates = self.connection.execute(
                    f"SELECT appeal_id,{column} AS deadline FROM appeals WHERE status IN "
                    f"({placeholders}) AND {column} IS NOT NULL AND {column}<?",
                    (*_STAGE_ACTIVE_STATUS[stage], now_text),
                ).fetchall()
                for candidate in candidates:
                    exists = self.connection.execute(
                        "SELECT 1 FROM overdue_records WHERE appeal_id=? AND stage=? AND deadline=?",
                        (candidate["appeal_id"], stage, candidate["deadline"]),
                    ).fetchone()
                    if exists is not None:
                        continue
                    self.connection.execute(
                        "INSERT INTO overdue_records(appeal_id,stage,deadline,detected_at,reason_code,"
                        "created_at) VALUES(?,?,?,?, 'unexplained',?)",
                        (candidate["appeal_id"], stage, candidate["deadline"], now_text, now_text),
                    )
                    found.append({
                        "appeal_id": candidate["appeal_id"],
                        "stage": stage,
                        "deadline": candidate["deadline"],
                    })
                    self._audit("appeal", candidate["appeal_id"], "overdue.detected", actor_id,
                                {"stage": stage, "deadline": candidate["deadline"]})
        return {"detected": found}

    def explain_overdue(
        self, actor_id: str, appeal_id: str, stage: str, reason_code: str, reason_detail: str
    ) -> dict[str, Any]:
        self._require(actor_id, "overdue.explain")
        if stage not in _STAGE_DEADLINE:
            raise ValidationFailed("逾期阶段不受支持")
        reason_code = required_text(reason_code, "reason_code", 64)
        reason_detail = required_text(reason_detail, "reason_detail", 512)
        with transaction(self.connection, immediate=True):
            appeal = self._locked_appeal(appeal_id)
            deadline = appeal[_STAGE_DEADLINE[stage]]
            if deadline is None:
                raise InvalidState("该申诉在该阶段没有期限")
            row = self.connection.execute(
                "SELECT overdue_id FROM overdue_records WHERE appeal_id=? AND stage=? ORDER BY overdue_id DESC LIMIT 1",
                (appeal_id, stage),
            ).fetchone()
            if row is not None:
                self.connection.execute(
                    "UPDATE overdue_records SET reason_code=?,reason_detail=?,recorded_by=? WHERE overdue_id=?",
                    (reason_code, reason_detail, actor_id, row["overdue_id"]),
                )
                overdue_id = row["overdue_id"]
            else:
                cursor = self.connection.execute(
                    "INSERT INTO overdue_records(appeal_id,stage,deadline,detected_at,reason_code,reason_detail,"
                    "recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (appeal_id, stage, deadline, self._now_text(), reason_code, reason_detail,
                     actor_id, self._now_text()),
                )
                overdue_id = cursor.lastrowid
            self._audit("appeal", appeal_id, "overdue.explained", actor_id,
                        {"overdue_id": overdue_id, "stage": stage, "reason_code": reason_code})
        return {"overdue_id": overdue_id, "stage": stage, "reason_code": reason_code}

    def _gate_overdue(self, appeal_row: sqlite3.Row, stage: str) -> None:
        column = _STAGE_DEADLINE[stage]
        deadline = appeal_row[column]
        if deadline is None or self._now_text() <= deadline:
            return
        explained = self.connection.execute(
            "SELECT 1 FROM overdue_records WHERE appeal_id=? AND stage=? AND reason_code<>'unexplained'",
            (appeal_row["appeal_id"], stage),
        ).fetchone()
        if explained is None:
            raise InvalidState(f"{stage} 阶段已超过期限 {deadline}，须先登记逾期原因")

    def _resolve_overdue(self, appeal_id: str, stage: str) -> None:
        self.connection.execute(
            "UPDATE overdue_records SET resolved_at=? WHERE appeal_id=? AND stage=? AND resolved_at IS NULL",
            (self._now_text(), appeal_id, stage),
        )

    # ----- 查询 -----

    def _locked_appeal(self, appeal_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)
        ).fetchone()
        if row is None:
            raise NotFound("申诉不存在")
        return row

    def get_appeal(self, appeal_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
        if row is None:
            raise NotFound("申诉不存在")
        applicants = self.connection.execute(
            "SELECT * FROM appeal_applicants WHERE appeal_id=? ORDER BY rowid", (appeal_id,)
        ).fetchall()
        materials = self.connection.execute(
            "SELECT * FROM appeal_materials WHERE appeal_id=? ORDER BY submitted_at,material_id", (appeal_id,)
        ).fetchall()
        evidence_refs = self.connection.execute(
            "SELECT * FROM appeal_evidence_refs WHERE appeal_id=? ORDER BY ref_id", (appeal_id,)
        ).fetchall()
        return {
            "appeal": dict(row),
            "applicants": [dict(item) for item in applicants],
            "materials": [dict(item) for item in materials],
            "evidence_refs": [dict(item) for item in evidence_refs],
        }

    def appeal_history(self, actor_id: str, appeal_id: str) -> dict[str, Any]:
        """一次查齐逾期原因、回避、证据版本、送达与执行台账流水。"""
        self._require(actor_id, "report.read")
        if self.connection.execute("SELECT 1 FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone() is None:
            raise NotFound("申诉不存在")

        def all_rows(query: str) -> list[dict[str, Any]]:
            return [dict(item) for item in self.connection.execute(query, (appeal_id,)).fetchall()]

        corrections = [
            dict(item) | {"required_items": json.loads(item["required_items_json"])}
            for item in self.connection.execute(
                "SELECT * FROM material_corrections WHERE appeal_id=? ORDER BY correction_id", (appeal_id,)
            ).fetchall()
        ]
        decision_row = self.connection.execute(
            "SELECT * FROM review_decisions WHERE appeal_id=?", (appeal_id,)
        ).fetchone()
        decision = None
        if decision_row is not None:
            items = self.connection.execute(
                "SELECT * FROM review_decision_items WHERE review_decision_id=? ORDER BY item_id",
                (decision_row["review_decision_id"],),
            ).fetchall()
            decision = dict(decision_row)
            decision["evidence_snapshot"] = json.loads(decision_row["evidence_snapshot_json"])
            decision["items"] = [dict(item) for item in items]
        events = [
            dict(item) | {"payload": json.loads(item["payload_json"])}
            for item in self.connection.execute(
                "SELECT event_id,event_type,actor_id,payload_json,created_at FROM appeal_audit_events "
                "WHERE entity_type='appeal' AND entity_id=? ORDER BY event_id", (appeal_id,)
            ).fetchall()
        ]
        return {
            "appeal_id": appeal_id,
            "corrections": corrections,
            "acceptance_reviews": all_rows(
                "SELECT * FROM acceptance_reviews WHERE appeal_id=? ORDER BY review_id"
            ),
            "assignments": all_rows(
                "SELECT assignment_id,reviewer_id,active,assigned_by,created_at "
                "FROM reviewer_assignments WHERE appeal_id=? ORDER BY assignment_id"
            ),
            "recusals": all_rows("SELECT * FROM recusals WHERE appeal_id=? ORDER BY recusal_id"),
            "review_decision": decision,
            "suspensions": all_rows(
                "SELECT * FROM enforcement_suspensions WHERE appeal_id=? ORDER BY suspension_id"
            ),
            "ledger": all_rows(
                "SELECT * FROM enforcement_ledger WHERE appeal_id=? ORDER BY ledger_id"
            ),
            "services": all_rows("SELECT * FROM service_records WHERE appeal_id=? ORDER BY service_id"),
            "overdue": all_rows("SELECT * FROM overdue_records WHERE appeal_id=? ORDER BY overdue_id"),
            "events": events,
        }

    def _close_appeal(self, appeal_id: str, status: str, actor_id: str, event_type: str, payload) -> None:
        self.connection.execute(
            "UPDATE appeals SET status=?,closed_at=?,revision=revision+1,updated_at=? WHERE appeal_id=?",
            (status, self._now_text(), self._now_text(), appeal_id),
        )
        # 案件终结后各阶段期限一并消灭，遗留逾期登记标记为已解决
        self.connection.execute(
            "UPDATE overdue_records SET resolved_at=? WHERE appeal_id=? AND resolved_at IS NULL",
            (self._now_text(), appeal_id),
        )
        self._audit("appeal", appeal_id, event_type, actor_id, payload)

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM appeal_audit_events ORDER BY event_id").fetchall()
        import hashlib
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
