"""把合成资料包装载进本地 SQLite（机构侧受控导入）。

输入是 materials/data.json 与 contract.json；不产生任何分析结论。
原始 records 进入 records_raw 表，仅聚合引擎可读取，不提供研究员导出入口。
"""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from .storage import append_audit, connect, dumps, initialize, transaction


def _read_materials(materials_dir: Path) -> dict:
    import json

    raw = (materials_dir / "data.json").read_bytes()
    data = json.loads(raw.decode("utf-8"))
    contract = json.loads((materials_dir / "contract.json").read_text("utf-8"))
    if set(data) != set(contract["tables"]):
        raise ValueError("资料表与契约不一致")
    data["_digest"] = hashlib.sha256(raw).hexdigest()
    return data


def bootstrap(db_path: str | Path, materials_dir: str | Path) -> dict:
    """初始化本地库并导入全部合成资料。重复运行在空库上才允许。"""
    materials_dir = Path(materials_dir)
    data = _read_materials(materials_dir)
    conn = connect(db_path)
    initialize(conn)

    with transaction(conn) as tx:
        existing = tx.execute("SELECT COUNT(*) AS n FROM institutions").fetchone()["n"]
        if existing:
            raise ValueError("数据库已初始化，避免覆盖持久状态；请使用新库路径")

        tx.executemany(
            "INSERT INTO institutions(id, label) VALUES (?, ?)",
            [(r["id"], r["label"]) for r in data["institutions"]],
        )
        tx.executemany(
            "INSERT INTO versions(id, received_on, description) VALUES (?, ?, ?)",
            [(r["id"], r["received_on"], r["description"]) for r in data["versions"]],
        )
        tx.executemany(
            """INSERT INTO records_raw(id, institution_id, person, household, grade,
                   region, on_date, reading_minutes, access_barrier, version)
               VALUES (:id, :institution_id, :person, :household, :grade,
                   :region, :on, :reading_minutes, :access_barrier, :version)""",
            [
                {
                    **r,
                    "access_barrier": 1 if r["access_barrier"] else 0,
                }
                for r in data["records"]
            ],
        )
        for plan in data["plans"]:
            tx.execute(
                """INSERT INTO plans(id, purpose, unit, contribution_limit, lower,
                       upper, epsilon, delta, max_releases, from_on, to_on,
                       allowed_metrics, allowed_groups, noise_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan["id"],
                    plan["purpose"],
                    plan["unit"],
                    plan["contribution_limit"],
                    plan["lower"],
                    plan["upper"],
                    plan["epsilon"],
                    plan["delta"],
                    plan["max_releases"],
                    plan["from_on"],
                    plan["to_on"],
                    dumps(plan["allowed_metrics"]),
                    dumps(plan["allowed_groups"]),
                    secrets.token_bytes(32).hex(),
                ),
            )
        tx.executemany(
            """INSERT INTO grants(id, institution_id, plan_id, version,
                   approved_on, withdrawn_on)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    r["id"],
                    r["institution_id"],
                    r["plan_id"],
                    r["version"],
                    r["approved_on"],
                    r["withdrawn_on"],
                )
                for r in data["grants"]
            ],
        )
        # 资料包中的历史请求作为已提交事实导入（仍需走本应用审核/发布流程）。
        tx.executemany(
            """INSERT INTO requests(id, plan_id, metric, group_by, filters, version,
                   epsilon, delta, state, submitted_on, approved_on, decided_on,
                   created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            [
                (
                    r["id"],
                    r["plan_id"],
                    r["metric"],
                    dumps(r["group_by"]),
                    dumps(r["filters"]),
                    r["version"],
                    r["epsilon"],
                    r["delta"],
                    "approved" if r["approved_on"] else "draft",
                    r["submitted_on"],
                    r["approved_on"],
                    "import",
                )
                for r in data["requests"]
            ],
        )
        for name in ("crash_before_commit", "crash_after_publish",
                     "skip_request_state_on_publish"):
            tx.execute(
                "INSERT INTO fault_config(name, enabled) VALUES (?, 0)", (name,)
            )
        # 本地合成操作者：库内以 operators 表为权限依据。
        local_operators = [
            ("U1", "researcher", ["PLAN1"]),
            ("U2", "reviewer", ["PLAN1", "PLAN2"]),
            ("U3", "institution", ["PLAN1", "PLAN2"]),
            ("U4", "administrator", ["PLAN1", "PLAN2"]),
            ("U5", "researcher", ["PLAN2"]),
            ("U6", "institution", ["PLAN2"]),
        ]
        tx.executemany(
            "INSERT INTO operators(id, role, plan_ids) VALUES (?, ?, ?)",
            [(oid, role, dumps(plans)) for oid, role, plans in local_operators],
        )
        append_audit(
            tx,
            at="1970-01-01T00:00:00Z",
            actor_id="bootstrap",
            actor_role="administrator",
            action="bootstrap",
            details={
                "data_digest": data["_digest"],
                "tables": {name: len(rows) for name, rows in data.items()
                           if isinstance(rows, list)},
            },
        )
    conn.close()
    return {"db_path": str(db_path), "data_digest": data["_digest"]}
