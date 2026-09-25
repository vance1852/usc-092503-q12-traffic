"""处罚申诉服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS pa_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('clerk','reviewer','auditor','admin')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 当事人（本人或单位），区别于系统内部工作人员
CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    party_type TEXT NOT NULL CHECK(party_type IN ('individual','enterprise')),
    name TEXT NOT NULL,
    id_number TEXT,
    created_at TEXT NOT NULL
);

-- 授权委托：当事人授权代理人代为发起、撤回申诉；scope_decision_id 为空表示全部决定
CREATE TABLE IF NOT EXISTS party_authorizations (
    authorization_id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    agent_party_id TEXT NOT NULL REFERENCES parties(party_id),
    scope_decision_id TEXT,
    power TEXT NOT NULL CHECK(power IN ('register','withdraw','full')),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    document_sha256 TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
    created_at TEXT NOT NULL,
    CHECK(agent_party_id <> party_id)
);

-- 处罚决定台账（争议对象）
CREATE TABLE IF NOT EXISTS penalty_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_number TEXT NOT NULL UNIQUE,
    subject_party_id TEXT NOT NULL REFERENCES parties(party_id),
    case_record_id TEXT,
    title TEXT NOT NULL,
    decided_by_staff TEXT,
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS penalty_terms (
    term_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL REFERENCES penalty_decisions(decision_id),
    action_type TEXT NOT NULL,
    amount_text TEXT,
    due_at TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, action_type)
);

-- 执行动作台账：催缴等下游流程只读取该表的当前状态
CREATE TABLE IF NOT EXISTS enforcement_actions (
    action_id TEXT PRIMARY KEY,
    term_id INTEGER NOT NULL REFERENCES penalty_terms(term_id),
    decision_id TEXT NOT NULL REFERENCES penalty_decisions(decision_id),
    action_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','terminated')),
    current_amount_text TEXT,
    current_due_at TEXT,
    suspended_appeal_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 受理后中止执行的规则（可查询、可配置）
CREATE TABLE IF NOT EXISTS suspension_rules (
    action_type TEXT PRIMARY KEY,
    suspend_on_acceptance INTEGER NOT NULL CHECK(suspend_on_acceptance IN (0,1)),
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES penalty_decisions(decision_id),
    legal_ground_code TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'registered','materials_pending','accepted','in_review',
        'decided','rejected','withdrawn','served'
    )),
    application_due_at TEXT NOT NULL,
    beyond_window INTEGER NOT NULL DEFAULT 0 CHECK(beyond_window IN (0,1)),
    late_reason TEXT,
    registered_by_party_id TEXT NOT NULL REFERENCES parties(party_id),
    registered_by_staff TEXT REFERENCES pa_users(user_id),
    acceptance_due_at TEXT NOT NULL,
    cure_deadline TEXT,
    accepted_at TEXT,
    review_due_at TEXT,
    decided_at TEXT,
    closed_at TEXT,
    idempotency_key TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 同一决定、同一法定事由只要存在未终结申诉即合并，不另生成多案
CREATE UNIQUE INDEX IF NOT EXISTS one_open_appeal_per_ground
ON appeals(decision_id, legal_ground_code)
WHERE status IN ('registered','materials_pending','accepted','in_review');

CREATE TABLE IF NOT EXISTS appeal_applicants (
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    party_id TEXT NOT NULL REFERENCES parties(party_id),
    representative_party_id TEXT REFERENCES parties(party_id),
    capacity TEXT NOT NULL CHECK(capacity IN ('self','agent')),
    authorization_id TEXT REFERENCES party_authorizations(authorization_id),
    joined_at TEXT NOT NULL,
    PRIMARY KEY(appeal_id, party_id)
);

CREATE TABLE IF NOT EXISTS appeal_materials (
    material_id TEXT NOT NULL,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    submitted_by_party_id TEXT NOT NULL REFERENCES parties(party_id),
    submitted_at TEXT NOT NULL,
    PRIMARY KEY(appeal_id, material_id)
);

CREATE TABLE IF NOT EXISTS appeal_evidence_refs (
    ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    evidence_id TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    title TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    submitted_by_party_id TEXT NOT NULL REFERENCES parties(party_id),
    created_at TEXT NOT NULL,
    UNIQUE(appeal_id, evidence_id, evidence_version)
);

CREATE TABLE IF NOT EXISTS material_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    required_items_json TEXT NOT NULL,
    cure_deadline TEXT NOT NULL,
    note TEXT,
    issued_by TEXT NOT NULL REFERENCES pa_users(user_id),
    served_at TEXT,
    cured_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acceptance_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    reviewer_id TEXT NOT NULL REFERENCES pa_users(user_id),
    conclusion TEXT NOT NULL CHECK(conclusion IN ('accepted','rejected')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewer_assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    reviewer_id TEXT NOT NULL REFERENCES pa_users(user_id),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    assigned_by TEXT NOT NULL REFERENCES pa_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recusals (
    recusal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    reviewer_id TEXT NOT NULL REFERENCES pa_users(user_id),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    decided_by TEXT REFERENCES pa_users(user_id),
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_recusal_per_reviewer
ON recusals(appeal_id, reviewer_id)
WHERE status='pending';

CREATE TABLE IF NOT EXISTS review_decisions (
    review_decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL UNIQUE REFERENCES appeals(appeal_id),
    conclusion TEXT NOT NULL CHECK(conclusion IN ('upheld','modified','revoked')),
    reason_text TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES pa_users(user_id),
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_decision_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_decision_id INTEGER NOT NULL REFERENCES review_decisions(review_decision_id),
    term_id INTEGER REFERENCES penalty_terms(term_id),
    action_id TEXT REFERENCES enforcement_actions(action_id),
    adjustment TEXT NOT NULL CHECK(adjustment IN ('resume','terminate','modify')),
    amount_text TEXT,
    due_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS enforcement_suspensions (
    suspension_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    action_id TEXT NOT NULL REFERENCES enforcement_actions(action_id),
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','released')),
    suspended_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    UNIQUE(appeal_id, action_id)
);

-- 执行台账流水：中止、恢复、终结、调整全部在此留痕
CREATE TABLE IF NOT EXISTS enforcement_ledger (
    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES enforcement_actions(action_id),
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    entry_type TEXT NOT NULL CHECK(entry_type IN ('suspend','resume','terminate','modify')),
    amount_before TEXT,
    amount_after TEXT,
    due_before TEXT,
    due_after TEXT,
    note TEXT,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_records (
    service_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    stage TEXT NOT NULL CHECK(stage IN ('registration','correction_notice','acceptance_notice','final_decision')),
    method TEXT NOT NULL CHECK(method IN ('direct','mail','electronic','announcement')),
    recipient_party_id TEXT NOT NULL REFERENCES parties(party_id),
    document_sha256 TEXT NOT NULL,
    served_at TEXT NOT NULL,
    served_by TEXT REFERENCES pa_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(appeal_id, stage)
);

CREATE TABLE IF NOT EXISTS overdue_records (
    overdue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    stage TEXT NOT NULL CHECK(stage IN ('acceptance','correction','review')),
    deadline TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_detail TEXT,
    recorded_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(appeal_id, stage, deadline)
);

CREATE TABLE IF NOT EXISTS appeal_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS appeal_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appeal_audit_entity
ON appeal_audit_events(entity_type, entity_id, event_id);

CREATE INDEX IF NOT EXISTS idx_actions_state
ON enforcement_actions(decision_id, state);
"""

DEFAULT_SUSPENSION_RULES = (
    ("payment_demand", 1, "罚款催缴在申诉受理后中止，避免难以回转"),
    ("license_suspension", 1, "暂扣、吊销证照类执行在申诉受理后中止"),
    ("demerit_points", 0, "记分不自动中止，复核决定撤销时再调整台账"),
    ("detention", 0, "限制人身自由措施依法不适用自动中止"),
)


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程复用同一连接；写入统一走 BEGIN IMMEDIATE 串行化
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.executemany(
            "INSERT INTO suspension_rules(action_type,suspend_on_acceptance,note) VALUES(?,?,?) "
            "ON CONFLICT(action_type) DO UPDATE SET note=excluded.note",
            DEFAULT_SUSPENSION_RULES,
        )


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
