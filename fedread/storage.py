"""SQLite 持久层。

库内逻辑分区：
* records_* 是机构侧受限合成原始资料，仅聚合引擎在受控计算中读取，
  任何研究员接口都不导出这些表；
* plans/grants/requests/releases/ledger 是联合研究的办理与额度状态；
* audit_log 是只追加的持久审计记录。

所有状态迁移走短事务，发布走单条 SERIALIZABLE 等效事务
（SQLite deferred 事务 + busy_timeout，写串行化），保证“送达即扣账、
扣账才送达”不会出现中间态。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS institutions (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    received_on TEXT NOT NULL,
    description TEXT NOT NULL
);

-- 受限原始资料：仅受控聚合读取，不对研究员导出。
CREATE TABLE IF NOT EXISTS records_raw (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    person TEXT NOT NULL,
    household TEXT NOT NULL,
    grade INTEGER NOT NULL,
    region TEXT NOT NULL,
    on_date TEXT NOT NULL,
    reading_minutes REAL NOT NULL,
    access_barrier INTEGER NOT NULL CHECK (access_barrier IN (0,1)),
    version TEXT NOT NULL REFERENCES versions(id)
);
CREATE INDEX IF NOT EXISTS idx_records_lookup
    ON records_raw(version, institution_id);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    unit TEXT NOT NULL CHECK (unit IN ('person','household')),
    contribution_limit INTEGER NOT NULL CHECK (contribution_limit >= 1),
    lower REAL NOT NULL,
    upper REAL NOT NULL CHECK (upper >= lower),
    epsilon REAL NOT NULL CHECK (epsilon > 0),
    delta REAL NOT NULL CHECK (delta >= 0),
    max_releases INTEGER NOT NULL CHECK (max_releases >= 0),
    from_on TEXT NOT NULL,
    to_on TEXT NOT NULL,
    allowed_metrics TEXT NOT NULL,   -- JSON 数组
    allowed_groups TEXT NOT NULL,    -- JSON 数组
    noise_key TEXT NOT NULL          -- hex，计划级确定性噪声密钥
);

CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    institution_id TEXT NOT NULL REFERENCES institutions(id),
    plan_id TEXT NOT NULL REFERENCES plans(id),
    version TEXT NOT NULL REFERENCES versions(id),
    approved_on TEXT NOT NULL,
    withdrawn_on TEXT,
    UNIQUE (institution_id, plan_id, version)
);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    metric TEXT NOT NULL,
    group_by TEXT NOT NULL,          -- JSON 数组
    filters TEXT NOT NULL,           -- JSON 对象
    version TEXT NOT NULL REFERENCES versions(id),
    epsilon REAL NOT NULL CHECK (epsilon > 0),
    delta REAL NOT NULL CHECK (delta >= 0),
    state TEXT NOT NULL,
    submitted_on TEXT NOT NULL,
    approved_on TEXT,
    decided_on TEXT,
    fingerprint TEXT,                -- 规范化发布指纹
    stopping_reason TEXT,
    reuse_of TEXT,                   -- 复用的已发布请求 id
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_plan ON requests(plan_id);
CREATE INDEX IF NOT EXISTS idx_requests_fp ON requests(fingerprint);

CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES requests(id),
    plan_id TEXT NOT NULL REFERENCES plans(id),
    fingerprint TEXT NOT NULL,
    version TEXT NOT NULL,
    epsilon_spent REAL NOT NULL,
    delta_spent REAL NOT NULL,
    published_on TEXT NOT NULL,
    payload TEXT NOT NULL,           -- JSON：含机制解释与单元结果，不含明细
    mechanism_explained TEXT NOT NULL,
    source_grant_ids TEXT NOT NULL,  -- JSON：实际依赖的机构授权
    data_digest TEXT NOT NULL        -- 参与计算的资料指纹
);
CREATE INDEX IF NOT EXISTS idx_releases_fp ON releases(plan_id, fingerprint);

-- 只追加额度账：每条已成立披露一条借账；复用不追加。
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    release_id TEXT NOT NULL UNIQUE REFERENCES releases(id),
    epsilon_spent REAL NOT NULL,
    delta_spent REAL NOT NULL,
    recorded_on TEXT NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    plan_id TEXT,
    request_id TEXT,
    details TEXT NOT NULL            -- JSON，研究员视图需脱敏
);

-- 本地合成操作者（资料包 operators 仅为输入事实，库内以本表为权限依据）。
CREATE TABLE IF NOT EXISTS operators (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    plan_ids TEXT NOT NULL
);

-- 故障注入点配置，供中断续办演练，生产演练后可清空。
CREATE TABLE IF NOT EXISTS fault_config (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式 IMMEDIATE 事务上下文；异常回滚，正常提交。

    BEGIN IMMEDIATE 在事务入口即申请保留写锁：并行写者按 busy_timeout
    排队，避免 WAL 下“先读后升级写锁”触发不可重试的 BUSY_SNAPSHOT，
    从而保证发布事务的串行化与额度竞争判定的正确性。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(text: str | None) -> Any:
    return json.loads(text) if text is not None else None


def append_audit(
    conn: sqlite3.Connection,
    *,
    at: str,
    actor_id: str,
    actor_role: str,
    action: str,
    details: dict,
    plan_id: str | None = None,
    request_id: str | None = None,
) -> None:
    """审计只追加，不提供更新/删除接口。"""
    conn.execute(
        """INSERT INTO audit_log(at, actor_id, actor_role, action, plan_id,
                                 request_id, details)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            at,
            actor_id,
            actor_role,
            action,
            plan_id,
            request_id,
            dumps(details),
        ),
    )
