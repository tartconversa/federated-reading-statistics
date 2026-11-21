"""联合研究服务层：角色操作、授权依赖、共享额度与发布协调。

承诺对应的关键设计：
* 审核批准 != 已发布；发布在单条写事务内完成“查额度→复用判定→
  聚合加噪→写 release→写 ledger→置 published”，SQLite 写串行化，
  两个并行请求即使各自余额够，也只有先提交者能成功，后者得到 budget_denied；
* 中断续办：commit 后崩溃由恢复例程按 release/ledger 事实收敛状态，
  未 commit 的事务整体回滚，不会重复扣账也不会出现“答案已送达额度未扣”；
* 复用：同计划、同资料版本、完全相同形状的已发布结果直接复用，
  噪声确定性派生，重试不重新抽样；
* 撤回：按 (计划, 版本) 的真实授权依赖停止待审/已批未发请求，
  已发布结果保留可查，且不波及其他计划。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import dp
from .config import (
    METRICS,
    ROLE_ADMINISTRATOR,
    ROLE_INSTITUTION,
    ROLE_RESEARCHER,
    ROLE_REVIEWER,
    STATE_APPROVED,
    STATE_BUDGET_DENIED,
    STATE_DRAFT,
    STATE_PUBLISHED,
    STATE_REJECTED,
    STATE_STOPPED,
    TERMINAL_STATES,
)
from .engine import RequestShape, compute, normalize_shape
from .errors import (
    AccessDenied,
    AuthorizationError,
    BudgetError,
    FedReadError,
    NotFound,
    StateError,
    ValidationError,
)
from .storage import append_audit, dumps, loads, transaction


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Operator:
    id: str
    role: str
    plan_ids: list[str]


class FedReadService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- 基础读取与权限 -----------------------------------------------------

    def operator(self, operator_id: str) -> Operator:
        row = self.conn.execute(
            "SELECT id, role, plan_ids FROM operators WHERE id = ?",
            (operator_id,),
        ).fetchone() if self._has_operators() else None
        if row is None:
            # 资料包未提供 operators 表内容时，用内置合成角色兜底。
            seeded = self._seeded_operators().get(operator_id)
            if seeded is None:
                raise NotFound("操作者不存在")
            return Operator(operator_id, seeded[0], seeded[1])
        return Operator(row["id"], row["role"], loads(row["plan_ids"]))

    def _has_operators(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operators'"
        ).fetchone() is not None

    def _seeded_operators(self) -> dict:
        return {
            "U1": (ROLE_RESEARCHER, ["PLAN1"]),
            "U2": (ROLE_REVIEWER, ["PLAN1", "PLAN2"]),
            "U3": (ROLE_INSTITUTION, ["PLAN1", "PLAN2"]),
            "U4": (ROLE_ADMINISTRATOR, ["PLAN1", "PLAN2"]),
            "U5": (ROLE_RESEARCHER, ["PLAN2"]),
            "U6": (ROLE_INSTITUTION, ["PLAN2"]),
        }

    def _require_role(self, op: Operator, role: str) -> None:
        if op.role != role:
            raise AccessDenied(f"该操作仅 {role} 可执行")

    def _require_plan_access(self, op: Operator, plan_id: str) -> None:
        if op.role != ROLE_ADMINISTRATOR and plan_id not in op.plan_ids:
            raise AccessDenied("无权操作该研究计划")

    def _plan(self, plan_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise NotFound("研究计划不存在")
        return row

    def _request(self, request_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFound("请求不存在")
        return row

    # -- 授权依赖 -----------------------------------------------------------

    def _active_grants(self, plan_id: str, version: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM grants
               WHERE plan_id = ? AND version = ? AND withdrawn_on IS NULL
               ORDER BY institution_id""",
            (plan_id, version),
        ).fetchall()

    def check_dependencies(self, plan_id: str, version: str) -> dict:
        """返回请求对机构授权的真实依赖；任一机构撤回即不可办理。

        联合统计依赖所有参与机构，授权不能相加：缺少任意一家都只能停止，
        而不是用其余机构替代发布。
        """
        plan = self._plan(plan_id)
        active = self._active_grants(plan_id, version)
        all_institutions = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM institutions ORDER BY id"
            ).fetchall()
        ]
        covered = {g["institution_id"] for g in active}
        missing = [i for i in all_institutions if i not in covered]
        return {
            "satisfied": not missing,
            "active_grant_ids": [g["id"] for g in active],
            "active_institutions": sorted(covered),
            "missing_institutions": missing,
        }

    # -- 结构预览（不触碰真实统计） -----------------------------------------

    def preview(self, op: Operator, *, plan_id: str, metric: str,
                group_by: list[str], filters: dict, version: str,
                epsilon_total: float) -> dict:
        self._require_role(op, ROLE_RESEARCHER)
        self._require_plan_access(op, plan_id)
        plan = self._plan(plan_id)
        shape = self._validate_request_shape(plan, metric, group_by, filters, version)
        self._validate_budget_shape(plan, epsilon_total, 0.0)

        limit = int(plan["contribution_limit"])
        lower, upper = float(plan["lower"]), float(plan["upper"])
        cells = self._declared_cells(plan, group_by)
        mechanisms: list[dict] = []
        if metric == "count":
            mechanisms.append(dp.build_mechanism(dp.count_sensitivity(limit),
                                                 epsilon_total).explain())
        else:
            eps_each = epsilon_total / 2.0
            mechanisms.append(dp.build_mechanism(dp.count_sensitivity(limit),
                                                 eps_each).explain())
            if metric == "barrier_fraction":
                mechanisms.append(
                    dp.build_mechanism(dp.indicator_sensitivity(limit),
                                       eps_each).explain())
            else:
                mechanisms.append(
                    dp.build_mechanism(dp.sum_sensitivity(limit, lower, upper),
                                       eps_each).explain())
        ledger = self.ledger_position(plan_id)
        deps = self.check_dependencies(plan_id, version)
        feasible, reasons = self._feasibility(plan, epsilon_total, 0.0, deps)
        return {
            "preview": True,
            "disclaimer": "预览仅基于计划声明参数推导，不含任何受限数据的真实统计",
            "plan_id": plan_id,
            "unit_adjacency": plan["unit"],
            "contribution_limit": limit,
            "clamp_bounds": [lower, upper],
            "declared_cells": cells,
            "cell_count": len(cells),
            "epsilon_planned": epsilon_total,
            "composition": self._composition_explain(plan, epsilon_total),
            "mechanisms": mechanisms,
            "budget_position": ledger,
            "authorization_dependencies": deps,
            "feasible": feasible,
            "adjustments": self._adjustment_hints(plan, epsilon_total, deps, reasons),
        }

    def _declared_cells(self, plan: sqlite3.Row, group_by: list[str]) -> list[str]:
        """单元结构只来自计划白名单的声明取值，绝不从数据中枚举。"""
        if not group_by:
            return ["ALL"]
        domains = {"region": ["rural", "urban"], "grade": [3, 4, 5, 6]}
        allowed = set(loads(plan["allowed_groups"]))
        levels = []
        for g in group_by:
            if g not in allowed:
                raise ValidationError(f"分组 {g} 不在计划允许范围")
            levels.append([(g, v) for v in domains[g]])
        out: list[str] = []
        def recurse(idx: int, parts: list[str]) -> None:
            if idx == len(levels):
                out.append("|".join(parts))
                return
            for _, value in levels[idx]:
                recurse(idx + 1, parts + [str(value)])
        recurse(0, [])
        return out

    def _feasibility(self, plan: sqlite3.Row, epsilon_total: float,
                     delta_total: float, deps: dict) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        ledger = self.ledger_position(plan["id"])
        if epsilon_total + ledger["epsilon_used"] > plan["epsilon"] + 1e-12:
            reasons.append("epsilon 超出计划共享额度剩余")
        if delta_total + ledger["delta_used"] > plan["delta"] + 1e-15:
            reasons.append("delta 超出计划共享额度剩余")
        if ledger["release_count"] >= plan["max_releases"]:
            reasons.append("正式披露次数已达上限")
        if not deps["satisfied"]:
            reasons.append("机构授权依赖未满足：" + ",".join(deps["missing_institutions"]))
        return not reasons, reasons

    def _adjustment_hints(self, plan: sqlite3.Row, epsilon_total: float,
                          deps: dict, reasons: list[str]) -> list[str]:
        hints: list[str] = []
        ledger = self.ledger_position(plan["id"])
        if any("epsilon" in r for r in reasons):
            remaining = max(0.0, plan["epsilon"] - ledger["epsilon_used"])
            hints.append(f"可将本次 epsilon 下调至不超过 {remaining:.6g}，"
                         "或减少分组单元/改申请可复用的相同形状")
        if any("delta" in r for r in reasons):
            hints.append("拉普拉斯机制实际 delta=0，可将本次 delta 声明为 0")
        if any("披露次数" in r for r in reasons):
            hints.append("复用同版本完全相同的已发布结果不占用披露次数")
        if not deps["satisfied"]:
            hints.append("等待机构恢复授权，或改用授权仍完整的资料版本/计划")
        return hints

    # -- 提交与审核 ---------------------------------------------------------

    def submit_request(self, op: Operator, *, request_id: str, plan_id: str,
                       metric: str, group_by: list[str], filters: dict,
                       version: str, epsilon_total: float,
                       delta_total: float, as_of: str | None = None) -> dict:
        self._require_role(op, ROLE_RESEARCHER)
        self._require_plan_access(op, plan_id)
        plan = self._plan(plan_id)
        as_of = as_of or _now()[:10]
        if not (plan["from_on"] <= as_of < plan["to_on"]):
            raise ValidationError(
                f"提出日 {as_of} 不在计划期限 "
                f"[{plan['from_on']}, {plan['to_on']}) 内；"
                "期限外的新申请不能使用该计划额度")
        shape = self._validate_request_shape(plan, metric, group_by, filters, version)
        self._validate_budget_shape(plan, epsilon_total, delta_total)
        if self.conn.execute("SELECT 1 FROM requests WHERE id = ?",
                             (request_id,)).fetchone():
            raise ValidationError("请求编号已存在")
        with transaction(self.conn) as tx:
            tx.execute(
                """INSERT INTO requests(id, plan_id, metric, group_by, filters,
                       version, epsilon, delta, state, submitted_on, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, plan_id, metric, dumps(list(shape.group_by)),
                 dumps(shape.filters), version, epsilon_total, delta_total,
                 STATE_DRAFT, _now(), op.id),
            )
            append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                         action="request_submitted", plan_id=plan_id,
                         request_id=request_id,
                         details={"fingerprint": shape.fingerprint(),
                                  "epsilon": epsilon_total})
        return {"request_id": request_id, "state": STATE_DRAFT,
                "fingerprint": shape.fingerprint()}

    def _validate_request_shape(self, plan: sqlite3.Row, metric: str,
                                group_by: list[str], filters: dict,
                                version: str) -> RequestShape:
        if metric not in METRICS or metric not in loads(plan["allowed_metrics"]):
            raise ValidationError("指标不在计划允许的可用指标白名单内")
        allowed_groups = set(loads(plan["allowed_groups"]))
        if any(g not in allowed_groups for g in group_by):
            raise ValidationError("分组超出计划允许的分组字段")
        shape = normalize_shape(plan_id=plan["id"], metric=metric,
                                group_by=group_by, filters=filters, version=version)
        if self.conn.execute("SELECT 1 FROM versions WHERE id = ?",
                             (version,)).fetchone() is None:
            raise ValidationError("资料版本不存在")
        return shape

    def _validate_budget_shape(self, plan: sqlite3.Row, epsilon_total: float,
                               delta_total: float) -> None:
        if epsilon_total <= 0:
            raise ValidationError("epsilon 必须为正数")
        if delta_total < 0:
            raise ValidationError("delta 不能为负")
        if epsilon_total > plan["epsilon"] + 1e-12:
            raise BudgetError("单次 epsilon 已超过计划总预算，任何组合下都无法发布")

    def review(self, op: Operator, request_id: str, *, approve: bool,
               note: str = "") -> dict:
        self._require_role(op, ROLE_REVIEWER)
        req = self._request(request_id)
        self._require_plan_access(op, req["plan_id"])
        if req["state"] != STATE_DRAFT:
            raise StateError(f"请求处于 {req['state']}，不可审核")
        new_state = STATE_APPROVED if approve else STATE_REJECTED
        with transaction(self.conn) as tx:
            tx.execute(
                "UPDATE requests SET state = ?, approved_on = ?, decided_on = ? WHERE id = ?",
                (new_state, _now() if approve else None, _now(), request_id),
            )
            append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                         action="request_approved" if approve else "request_rejected",
                         plan_id=req["plan_id"], request_id=request_id,
                         details={"note": note})
        return {"request_id": request_id, "state": new_state}

    # -- 额度账 -------------------------------------------------------------

    def ledger_position(self, plan_id: str) -> dict:
        rows = self.conn.execute(
            """SELECT COALESCE(SUM(epsilon_spent),0) AS eps,
                      COALESCE(SUM(delta_spent),0) AS dl,
                      COUNT(*) AS n
               FROM ledger WHERE plan_id = ?""",
            (plan_id,),
        ).fetchone()
        plan = self._plan(plan_id)
        return {
            "plan_id": plan_id,
            "epsilon_budget": plan["epsilon"],
            "epsilon_used": rows["eps"],
            "epsilon_remaining": plan["epsilon"] - rows["eps"],
            "delta_budget": plan["delta"],
            "delta_used": rows["dl"],
            "delta_remaining": plan["delta"] - rows["dl"],
            "release_count": rows["n"],
            "max_releases": plan["max_releases"],
        }

    def _composition_explain(self, plan: sqlite3.Row, epsilon_total: float) -> dict:
        ledger = self.ledger_position(plan["id"])
        return {
            "rule": "同计划全部已发布披露按顺序组合，epsilon/delta 线性相加",
            "epsilon_after_publish": ledger["epsilon_used"] + epsilon_total,
            "epsilon_budget": plan["epsilon"],
            "delta_after_publish": ledger["delta_used"],
            "delta_budget": plan["delta"],
            "reuse_policy": "同计划同版本完全相同形状复用既有发布，零新增消耗",
        }

    # -- 发布（核心事务） ---------------------------------------------------

    def publish(self, op: Operator, request_id: str) -> dict:
        if op.role not in (ROLE_REVIEWER, ROLE_ADMINISTRATOR):
            raise AccessDenied("发布仅审核人或管理员可执行")
        req = self._request(request_id)
        self._require_plan_access(op, req["plan_id"])
        if req["state"] in TERMINAL_STATES and req["state"] != STATE_BUDGET_DENIED:
            if req["state"] == STATE_PUBLISHED:
                existing = self.get_release(op, request_id)
                return {"request_id": request_id, "published": True, "reused": True,
                        "reuse_of": req["reuse_of"],
                        "release": existing["payload"]}
            raise StateError(f"请求处于终态 {req['state']}，不可发布")
        if req["state"] not in (STATE_APPROVED, STATE_BUDGET_DENIED):
            raise StateError("请求尚未审核批准，不能发布")

        plan = self._plan(req["plan_id"])
        shape = normalize_shape(
            plan_id=req["plan_id"], metric=req["metric"],
            group_by=loads(req["group_by"]), filters=loads(req["filters"]),
            version=req["version"],
        )
        deps = self.check_dependencies(req["plan_id"], req["version"])

        # 事务内只做状态落库；业务异常在事务提交后抛出，避免回滚掉
        # budget_denied/stopped 等需要持久保留的办理状态。
        outcome: dict | None = None
        pending_error: FedReadError | None = None
        pre_commit_crash = False

        try:
            with transaction(self.conn) as tx:
                # 事务内重新读取最新状态与账，防止并行竞争下的过期判断。
                fresh = tx.execute("SELECT * FROM requests WHERE id = ?",
                                   (request_id,)).fetchone()
                if fresh["state"] == STATE_PUBLISHED:
                    outcome = {"request_id": request_id, "published": True,
                               "reused": True, "release": self._load_release(tx, request_id)}
                elif fresh["state"] == STATE_STOPPED:
                    pending_error = StateError("授权已撤回，请求已停止，不能发布")
                elif fresh["state"] not in (STATE_APPROVED, STATE_BUDGET_DENIED):
                    pending_error = StateError(f"请求状态 {fresh['state']} 不可发布")
                else:
                    # 复用判定：同计划、同资料版本、同指纹的已发布结果。
                    existing = tx.execute(
                        """SELECT r.* FROM releases r JOIN requests q ON q.id = r.request_id
                           WHERE r.plan_id = ? AND r.fingerprint = ?
                             AND r.version = ? AND q.state = 'published'""",
                        (plan["id"], shape.fingerprint(), shape.version),
                    ).fetchone()
                    if existing is not None:
                        tx.execute(
                            """UPDATE requests SET state = 'published', decided_on = ?,
                                   fingerprint = ?, reuse_of = ? WHERE id = ?""",
                            (_now(), shape.fingerprint(),
                             existing["request_id"], request_id),
                        )
                        append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                                     action="request_reused", plan_id=plan["id"],
                                     request_id=request_id,
                                     details={"reused_release": existing["id"],
                                              "epsilon_new": 0.0})
                        outcome = {"request_id": request_id, "published": True,
                                   "reused": True,
                                   "reuse_of": existing["request_id"],
                                   "release": loads(existing["payload"])}
                    elif not deps["satisfied"]:
                        tx.execute(
                            "UPDATE requests SET state = ?, stopping_reason = ?, decided_on = ? WHERE id = ?",
                            (STATE_STOPPED,
                             "授权依赖缺失：" + ",".join(deps["missing_institutions"]),
                             _now(), request_id),
                        )
                        append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                                     action="request_stopped", plan_id=plan["id"],
                                     request_id=request_id, details=deps)
                        pending_error = AuthorizationError(
                            "机构授权依赖未满足，请求已按真实依赖停止；"
                            "错误信息不包含任何受限明细")
                    else:
                        ledger = tx.execute(
                            """SELECT COALESCE(SUM(epsilon_spent),0) AS eps,
                                      COALESCE(SUM(delta_spent),0) AS dl, COUNT(*) AS n
                               FROM ledger WHERE plan_id = ?""",
                            (plan["id"],),
                        ).fetchone()
                        over_budget = (
                            ledger["eps"] + req["epsilon"] > plan["epsilon"] + 1e-12
                            or ledger["dl"] + req["delta"] > plan["delta"] + 1e-15
                            or ledger["n"] >= plan["max_releases"]
                        )
                        if over_budget:
                            tx.execute(
                                "UPDATE requests SET state = ?, decided_on = ? WHERE id = ?",
                                (STATE_BUDGET_DENIED, _now(), request_id),
                            )
                            append_audit(tx, at=_now(), actor_id=op.id,
                                         actor_role=op.role, action="budget_denied",
                                         plan_id=plan["id"], request_id=request_id,
                                         details={"epsilon_used": ledger["eps"],
                                                  "epsilon_ask": req["epsilon"]})
                            pending_error = BudgetError(
                                "共享额度竞争失败：本次发布将突破计划 epsilon/delta "
                                "或披露次数上限，未扣除任何额度")
                        else:
                            payload = compute(
                                tx, shape=shape, plan=plan,
                                institution_ids=deps["active_institutions"],
                                epsilon_total=float(req["epsilon"]),
                                noise_seed_material=plan["noise_key"].encode(),
                            )
                            release_id = "REL-" + request_id
                            tx.execute(
                                """INSERT INTO releases(id, request_id, plan_id, fingerprint,
                                       version, epsilon_spent, delta_spent, published_on,
                                       payload, mechanism_explained, source_grant_ids,
                                       data_digest)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (release_id, request_id, plan["id"], shape.fingerprint(),
                                 shape.version, req["epsilon"], req["delta"], _now(),
                                 dumps(payload), dumps(payload["mechanisms"]),
                                 dumps(deps["active_grant_ids"]), payload["data_digest"]),
                            )
                            # 额度借账与发布同事务：要么都成立，要么都回滚。
                            tx.execute(
                                """INSERT INTO ledger(plan_id, release_id, epsilon_spent,
                                       delta_spent, recorded_on, note)
                                   VALUES (?, ?, ?, ?, ?, ?)""",
                                (plan["id"], release_id, req["epsilon"], req["delta"],
                                 _now(), f"发布 {release_id} 的顺序组合消耗"),
                            )
                            # 演练钩子：模拟“应答丢失/状态更新未落地”——release 与
                            # ledger 已在本事务持久，但请求保留 approved，
                            # 由 recover 按持久事实收敛（不重复扣账、不重抽噪声）。
                            skip_state = self._fault_enabled(
                                tx, "skip_request_state_on_publish")
                            if not skip_state:
                                tx.execute(
                                    "UPDATE requests SET state = 'published', "
                                    "decided_on = ?, fingerprint = ? WHERE id = ?",
                                    (_now(), shape.fingerprint(), request_id),
                                )
                            else:
                                tx.execute(
                                    "UPDATE fault_config SET enabled=0 "
                                    "WHERE name='skip_request_state_on_publish'")
                                tx.execute(
                                    "UPDATE requests SET fingerprint = ? WHERE id = ?",
                                    (shape.fingerprint(), request_id),
                                )
                            append_audit(tx, at=_now(), actor_id=op.id,
                                         actor_role=op.role, action="request_published",
                                         plan_id=plan["id"], request_id=request_id,
                                         details={"release_id": release_id,
                                                  "epsilon_spent": req["epsilon"],
                                                  "delta_spent": req["delta"],
                                                  "grants": deps["active_grant_ids"],
                                                  "state_update_skipped": skip_state})
                            # 演练钩子：提交前崩溃 => 整个事务回滚，
                            # 不留 release/ledger/状态任何痕迹，可安全重试。
                            if self._fault_enabled(tx, "crash_before_commit"):
                                pre_commit_crash = True
                                raise RuntimeError("SIMULATED CRASH BEFORE COMMIT")
                            outcome = {"request_id": request_id, "published": True,
                                       "reused": False, "release": payload}

        except RuntimeError as exc:
            if str(exc) != 'SIMULATED CRASH BEFORE COMMIT':
                raise
            pre_commit_crash = True
        if pending_error is not None:
            raise pending_error

        if pre_commit_crash:
            # 事务已回滚；在独立事务中清除故障标志并留痕，便于重试。
            with transaction(self.conn) as tx:
                tx.execute(
                    "UPDATE fault_config SET enabled=0 "
                    "WHERE name='crash_before_commit'")
                append_audit(tx, at=_now(), actor_id="fault-injection",
                             actor_role=ROLE_ADMINISTRATOR,
                             action="simulated_crash_before_commit",
                             details={"request_id": request_id})
            raise RuntimeError("SIMULATED CRASH BEFORE COMMIT")

        # 事务已提交。若管理员置位演练标志，模拟进程在提交后、应答前崩溃：
        # 持久事实已经成立，恢复时只能收敛状态，不能重复扣账或重抽噪声。
        if self._consume_crash_flag():
            raise RuntimeError("SIMULATED CRASH AFTER COMMIT")
        return outcome

    def _fault_enabled(self, tx: sqlite3.Connection, name: str) -> bool:
        row = tx.execute("SELECT enabled FROM fault_config WHERE name = ?",
                         (name,)).fetchone()
        return bool(row and row["enabled"])

    def _consume_crash_flag(self) -> bool:
        row = self.conn.execute(
            "SELECT enabled FROM fault_config WHERE name='crash_after_publish'"
        ).fetchone()
        if row is None or not row["enabled"]:
            return False
        with transaction(self.conn) as tx:
            tx.execute(
                "UPDATE fault_config SET enabled=0 WHERE name='crash_after_publish'"
            )
            append_audit(tx, at=_now(), actor_id="fault-injection",
                         actor_role=ROLE_ADMINISTRATOR,
                         action="simulated_crash_after_commit", details={})
        return True

    def arm_crash(self, op: Operator, *, point: str = "after_publish") -> None:
        """管理员演练：在下一次发布的指定位置模拟进程崩溃。

        point='before_commit'：发布事务提交前崩溃，事务整体回滚，可安全重试；
        point='after_publish'：事务提交后、应答前崩溃，并模拟请求状态更新
        未落地（release/ledger 已持久），由 recover 收敛请求状态，
        不重复扣账、不重抽噪声。
        """
        self._require_role(op, ROLE_ADMINISTRATOR)
        with transaction(self.conn) as tx:
            if point == "before_commit":
                tx.execute(
                    "UPDATE fault_config SET enabled = 1 "
                    "WHERE name = 'crash_before_commit'")
            else:
                tx.execute(
                    "UPDATE fault_config SET enabled = 1 "
                    "WHERE name IN ('crash_after_publish',"
                    "'skip_request_state_on_publish')")
            append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                         action="fault_armed", details={"point": point})

    def recover(self, op: Operator) -> dict:
        """中断续办：以 release/ledger 事实为准收敛请求状态。

        已 commit 的发布：release 与 ledger 必然同时存在，把请求收敛为
        published（不重复扣账、不重抽噪声）；未 commit 的事务已由 SQLite
        回滚，请求保持 approved，可安全重试。
        """
        self._require_role(op, ROLE_ADMINISTRATOR)
        repaired: list[str] = []
        with transaction(self.conn) as tx:
            rows = tx.execute(
                """SELECT q.id AS qid, q.state, r.id AS rid, r.fingerprint
                   FROM requests q JOIN releases r ON r.request_id = q.id
                   WHERE q.state != 'published'"""
            ).fetchall()
            for row in rows:
                tx.execute(
                    "UPDATE requests SET state='published', decided_on=COALESCE(decided_on, ?), fingerprint=? WHERE id=?",
                    (_now(), row["fingerprint"], row["qid"]),
                )
                repaired.append(row["qid"])
            # 反向完整性：有 ledger 无 release 不可能发生（同事务），发现即报错。
            orphan = tx.execute(
                """SELECT l.id FROM ledger l LEFT JOIN releases r
                   ON r.id = l.release_id WHERE r.id IS NULL"""
            ).fetchall()
            if orphan:
                raise StateError("检测到孤立额度账，数据完整性被破坏")
            append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                         action="recovery_run",
                         details={"repaired": repaired})
        return {"repaired_requests": repaired}

    # -- 结果查询（研究员只拿到获准结果与解释） -----------------------------

    def get_release(self, op: Operator, request_id: str) -> dict:
        req = self._request(request_id)
        if op.role == ROLE_RESEARCHER:
            self._require_plan_access(op, req["plan_id"])
        elif op.role not in (ROLE_REVIEWER, ROLE_ADMINISTRATOR):
            raise AccessDenied("该角色不能查询发布结果")
        if req["state"] != STATE_PUBLISHED:
            raise StateError(f"发布未成立（当前状态 {req['state']}），"
                             "不能凭进程显示猜测结果")
        row = self.conn.execute(
            "SELECT * FROM releases WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:  # 复用型发布指向原始 release
            row = self.conn.execute(
                "SELECT * FROM releases WHERE request_id = ?",
                (req["reuse_of"],),
            ).fetchone()
        return {
            "request_id": request_id,
            "state": STATE_PUBLISHED,
            "published_on": row["published_on"],
            "reuse_of": req["reuse_of"],
            "epsilon_spent": row["epsilon_spent"],
            "delta_spent": row["delta_spent"],
            "source_grant_ids": loads(row["source_grant_ids"]),
            "data_digest": row["data_digest"],
            "payload": loads(row["payload"]),
        }

    def list_requests(self, op: Operator, plan_id: str) -> list[dict]:
        self._require_plan_access(op, plan_id)
        rows = self.conn.execute(
            "SELECT id, metric, state, version, epsilon, delta, fingerprint, reuse_of FROM requests WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
        if op.role == ROLE_RESEARCHER:
            return [dict(r) for r in rows]
        return [dict(r) for r in rows]

    # -- 机构撤回 -----------------------------------------------------------

    def withdraw_grant(self, op: Operator, grant_id: str, *, reason: str = "") -> dict:
        self._require_role(op, ROLE_INSTITUTION)
        grant = self.conn.execute("SELECT * FROM grants WHERE id = ?",
                                  (grant_id,)).fetchone()
        if grant is None:
            raise NotFound("授权不存在")
        self._require_plan_access(op, grant["plan_id"])
        if grant["withdrawn_on"] is not None:
            raise StateError("授权此前已撤回")
        stopped: list[str] = []
        with transaction(self.conn) as tx:
            tx.execute("UPDATE grants SET withdrawn_on = ? WHERE id = ?",
                       (_now(), grant_id))
            # 真实依赖：仅停止同计划、同版本、尚未发布的请求。
            pending = tx.execute(
                """SELECT id FROM requests
                   WHERE plan_id = ? AND version = ?
                     AND state IN (?, ?)""",
                (grant["plan_id"], grant["version"], STATE_DRAFT, STATE_APPROVED),
            ).fetchall()
            for row in pending:
                # 再次确认依赖确实已不满足（避免多机构授权冗余误停）。
                deps = self.check_dependencies(grant["plan_id"], grant["version"])
                if not deps["satisfied"]:
                    tx.execute(
                        "UPDATE requests SET state = ?, stopping_reason = ?, decided_on = ? WHERE id = ?",
                        (STATE_STOPPED,
                         f"机构 {grant['institution_id']} 撤回授权：{reason}",
                         _now(), row["id"]),
                    )
                    stopped.append(row["id"])
            append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                         action="grant_withdrawn", plan_id=grant["plan_id"],
                         details={"grant_id": grant_id,
                                  "institution": grant["institution_id"],
                                  "stopped_requests": stopped,
                                  "published_results_kept": True})
        return {"grant_id": grant_id, "withdrawn": True,
                "stopped_requests": stopped,
                "published_results": "已发布结果不可收回，保留当时范围与限制可查"}

    def reinstate_grant(self, op: Operator, grant_id: str) -> dict:
        """资料方恢复授权（演练用）；已停止请求需重新提交，不自动复活。"""
        self._require_role(op, ROLE_INSTITUTION)
        grant = self.conn.execute("SELECT * FROM grants WHERE id = ?",
                                  (grant_id,)).fetchone()
        if grant is None:
            raise NotFound("授权不存在")
        self._require_plan_access(op, grant["plan_id"])
        with transaction(self.conn) as tx:
            tx.execute("UPDATE grants SET withdrawn_on = NULL WHERE id = ?",
                       (grant_id,))
            append_audit(tx, at=_now(), actor_id=op.id, actor_role=op.role,
                         action="grant_reinstated", plan_id=grant["plan_id"],
                         details={"grant_id": grant_id})
        return {"grant_id": grant_id, "reinstated": True}

    # -- 管理员审计 ---------------------------------------------------------

    def audit_trail(self, op: Operator, *, plan_id: str | None = None) -> list[dict]:
        self._require_role(op, ROLE_ADMINISTRATOR)
        sql = "SELECT * FROM audit_log"
        params: tuple = ()
        if plan_id:
            sql += " WHERE plan_id = ?"
            params = (plan_id,)
        sql += " ORDER BY id"
        return [
            {**dict(r), "details": loads(r["details"])}
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def disclosure_report(self, op: Operator) -> list[dict]:
        """管理员核对：每条实际披露对应的授权、人群版本、贡献约定与额度变化。"""
        self._require_role(op, ROLE_ADMINISTRATOR)
        rows = self.conn.execute(
            """SELECT r.*, q.filters, q.group_by, q.metric, p.unit,
                      p.contribution_limit, p.lower, p.upper
               FROM releases r
               JOIN requests q ON q.id = r.request_id
               JOIN plans p ON p.id = r.plan_id
               ORDER BY r.id"""
        ).fetchall()
        return [
            {
                "release_id": r["id"],
                "request_id": r["request_id"],
                "plan_id": r["plan_id"],
                "version": r["version"],
                "metric": r["metric"],
                "group_by": loads(r["group_by"]),
                "filters": loads(r["filters"]),
                "adjacency_unit": r["unit"],
                "contribution_limit": r["contribution_limit"],
                "clamp_bounds": [r["lower"], r["upper"]],
                "epsilon_spent": r["epsilon_spent"],
                "delta_spent": r["delta_spent"],
                "source_grant_ids": loads(r["source_grant_ids"]),
                "data_digest": r["data_digest"],
                "published_on": r["published_on"],
                "mechanism": loads(r["mechanism_explained"]),
            }
            for r in rows
        ]

    def _load_release(self, tx: sqlite3.Connection, request_id: str) -> dict:
        row = tx.execute("SELECT * FROM releases WHERE request_id = ?",
                         (request_id,)).fetchone()
        return loads(row["payload"])
