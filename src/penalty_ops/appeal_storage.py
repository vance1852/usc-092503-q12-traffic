"""申诉复核子系统的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS appeal_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('officer','party','agent','handler','reviewer','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 处罚决定:申诉的争议对象
CREATE TABLE IF NOT EXISTS penalty_decisions (
    decision_id TEXT PRIMARY KEY,
    case_record_id TEXT NOT NULL,
    party_id TEXT NOT NULL REFERENCES appeal_users(user_id),
    violation_summary TEXT NOT NULL,
    legal_basis TEXT NOT NULL,
    fine_amount_cny TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','modified','revoked')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    issued_by TEXT NOT NULL REFERENCES appeal_users(user_id),
    issued_at TEXT NOT NULL
);

-- 处罚决定作出时引用的证据版本
CREATE TABLE IF NOT EXISTS decision_evidence (
    decision_id TEXT NOT NULL REFERENCES penalty_decisions(decision_id),
    evidence_id TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    PRIMARY KEY (decision_id, evidence_id)
);

-- 当事人对代理人的授权
CREATE TABLE IF NOT EXISTS agent_authorizations (
    authorization_id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL REFERENCES appeal_users(user_id),
    agent_id TEXT NOT NULL REFERENCES appeal_users(user_id),
    scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
    created_by TEXT NOT NULL REFERENCES appeal_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES penalty_decisions(decision_id),
    legal_ground TEXT NOT NULL,
    appellant_id TEXT NOT NULL REFERENCES appeal_users(user_id),
    appellant_kind TEXT NOT NULL CHECK(appellant_kind IN ('party','agent')),
    authorization_id TEXT REFERENCES agent_authorizations(authorization_id),
    statement TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('registered','correcting','accepted','decided','rejected','withdrawn')),
    merge_count INTEGER NOT NULL DEFAULT 0 CHECK(merge_count >= 0),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

-- 同一争议决定、同一法定事由只允许一个在办案件,重复提交合并进入该案件
CREATE UNIQUE INDEX IF NOT EXISTS one_open_appeal_per_ground
ON appeals(decision_id, legal_ground)
WHERE state IN ('registered','correcting','accepted');

CREATE INDEX IF NOT EXISTS idx_appeals_decision ON appeals(decision_id, state);

CREATE TABLE IF NOT EXISTS appeal_materials (
    material_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('initial','supplement','merged')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appeal_materials ON appeal_materials(appeal_id, material_id);

-- 材料补正通知
CREATE TABLE IF NOT EXISTS material_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    required_items TEXT NOT NULL,
    reason TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','fulfilled','expired','cancelled')),
    created_by TEXT NOT NULL REFERENCES appeal_users(user_id),
    created_at TEXT NOT NULL,
    fulfilled_at TEXT
);

-- 受理审查与复核决定
CREATE TABLE IF NOT EXISTS appeal_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    stage TEXT NOT NULL CHECK(stage IN ('acceptance','reconsideration')),
    conclusion TEXT NOT NULL,
    reason TEXT NOT NULL,
    new_fine_amount_cny TEXT,
    reviewer_id TEXT NOT NULL REFERENCES appeal_users(user_id),
    reviewed_at TEXT NOT NULL,
    overdue_reason TEXT
);

-- 复核决定引用的证据版本
CREATE TABLE IF NOT EXISTS review_evidence_citations (
    review_id INTEGER NOT NULL REFERENCES appeal_reviews(review_id),
    evidence_id TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    PRIMARY KEY (review_id, evidence_id)
);

-- 审查人员回避登记
CREATE TABLE IF NOT EXISTS appeal_recusals (
    recusal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    reviewer_id TEXT NOT NULL REFERENCES appeal_users(user_id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(appeal_id, reviewer_id)
);

-- 各环节期限:补正、受理审查、复核决定
CREATE TABLE IF NOT EXISTS appeal_deadlines (
    deadline_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    kind TEXT NOT NULL CHECK(kind IN ('correction','acceptance','reconsideration')),
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','met','void','overdue')),
    completed_at TEXT,
    overdue_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_appeal_deadlines ON appeal_deadlines(appeal_id, status);

-- 执行动作(催缴通知、滞纳金计收),受理后按规则中止
CREATE TABLE IF NOT EXISTS enforcement_actions (
    action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES penalty_decisions(decision_id),
    kind TEXT NOT NULL CHECK(kind IN ('dunning_notice','late_fee_accrual')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','cancelled')),
    suspended_by_appeal_id TEXT REFERENCES appeals(appeal_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_enforcement_actions ON enforcement_actions(decision_id, status);

-- 处罚台账:罚款与滞纳金,结论作出时以事务方式恢复或调整
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL REFERENCES penalty_decisions(decision_id),
    kind TEXT NOT NULL CHECK(kind IN ('fine','late_fee')),
    amount_cny TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('outstanding','suspended','adjusted','waived','collected')),
    supersedes_entry_id INTEGER REFERENCES ledger_entries(entry_id),
    source TEXT NOT NULL,
    appeal_id TEXT REFERENCES appeals(appeal_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_entries ON ledger_entries(decision_id, status);

-- 最终送达,每件申诉只登记一次
CREATE TABLE IF NOT EXISTS appeal_deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id TEXT NOT NULL UNIQUE REFERENCES appeals(appeal_id),
    method TEXT NOT NULL CHECK(method IN ('electronic','postal','in_person')),
    recipient TEXT NOT NULL,
    delivered_by TEXT NOT NULL REFERENCES appeal_users(user_id),
    delivered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeal_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appeal_audit_entity
ON appeal_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


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


def rows(connection: sqlite3.Connection, query: str, args: tuple = ()) -> list[dict]:
    return [dict(row) for row in connection.execute(query, args).fetchall()]
