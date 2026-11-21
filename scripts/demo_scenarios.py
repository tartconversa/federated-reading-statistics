"""端到端场景演练（可重复运行，使用独立临时库）。

覆盖四个承诺交会：
1. 共享额度竞争：两个已批准请求并行发布，各自余额看似够用，
   合起来超额时只有先提交者成功，后者 budget_denied 且零扣账；
2. 中断续办：发布事务提交后模拟崩溃，恢复后状态与额度收敛，
   重试不重复扣账、不重抽噪声；
3. 结果复用：同计划同版本完全相同形状的请求复用既有发布，
   零新增消耗、答案逐位相同；指标/人群/分组/版本任一变化即重新计费；
4. 用途撤回：机构撤回后待审/已批未发请求按真实依赖停止，
   已发布结果保留可查，且不波及其他计划。

运行：python3 -m scripts.demo_scenarios
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fedread.bootstrap import bootstrap
from fedread.errors import AuthorizationError, BudgetError, StateError
from fedread.service import FedReadService
from fedread.storage import connect

MATERIALS = Path(__file__).resolve().parents[1] / "materials"


def heading(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, obj) -> None:
    print(f"\n-- {label}")
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def fresh_service(tmp: Path) -> FedReadService:
    db = tmp / "demo.db"
    bootstrap(db, MATERIALS)
    return FedReadService(connect(db))


def scenario_budget_contention(svc: FedReadService) -> None:
    heading("场景 1：共享额度竞争（并行发布，合起来超额不能都交付）")
    u2, u4 = svc.operator("U2"), svc.operator("U4")
    # PLAN2 总额度 epsilon=1.0、最多 4 次披露；构造 0.6 + 0.6 的两个请求。
    # 分组不同 => 指纹不同 => 不能复用，真正竞争同一共享额度。
    shapes = {"C1": ["region"], "C2": []}
    for rid, groups in shapes.items():
        svc.submit_request(
            svc.operator("U5"),
            request_id=rid, plan_id="PLAN2", metric="count",
            group_by=groups, filters={}, version="2026-04-v1",
            epsilon_total=0.6, delta_total=0.0, as_of="2026-05-15")
    svc.review(u2, "C1", approve=True)
    svc.review(u2, "C2", approve=True)
    show("发布前额度", svc.ledger_position("PLAN2"))

    first = svc.publish(u2, "C1")
    show("C1 发布成立", {"published": first["published"], "reused": first["reused"]})
    try:
        svc.publish(u2, "C2")
        raise AssertionError("C2 应当因额度竞争失败")
    except BudgetError as exc:
        show("C2 被拒（未扣额度）", {"error": type(exc).__name__, "message": str(exc)})
    position = svc.ledger_position("PLAN2")
    show("发布后额度账", position)
    assert position["epsilon_used"] == 0.6, "失败请求不得扣账"
    states = {r["id"]: r["state"] for r in svc.list_requests(u4, "PLAN2")}
    show("请求状态", states)
    assert states["C1"] == "published" and states["C2"] == "budget_denied"
    print("✔ 并行超额时只交付先提交者，失败方零扣账、状态可查")


def scenario_crash_recovery(svc: FedReadService) -> None:
    heading("场景 2：中断续办（提交后崩溃：不重复扣账、不重抽噪声）")
    u2, u4 = svc.operator("U2"), svc.operator("U4")
    svc.submit_request(
        svc.operator("U5"), request_id="K1", plan_id="PLAN2",
        metric="count", group_by=[], filters={},
        version="2026-04-v1", epsilon_total=0.3, delta_total=0.0, as_of="2026-05-15")
    svc.review(u2, "K1", approve=True)
    svc.arm_crash(u4)
    try:
        svc.publish(u2, "K1")
        raise AssertionError("应当模拟崩溃")
    except RuntimeError as exc:
        show("模拟进程崩溃", {"error": str(exc)})

    # 新进程视角：用同一库路径重新连接，先查持久事实。
    db_path = svc.conn.execute("PRAGMA database_list").fetchone()[2]
    svc2 = FedReadService(connect(db_path))
    before = svc2.ledger_position("PLAN2")
    show("崩溃后额度账（提交已生效）", before)
    recovered = svc2.recover(u4)
    show("恢复结果", recovered)
    result = svc2.get_release(u4, "K1")
    answer_first = result["payload"]["cells"]
    show("恢复后可查发布", {"state": result["state"], "cells": answer_first})

    # 重试发布：幂等返回同一结果，额度不重复扣。
    retry = svc2.publish(u2, "K1")
    after = svc2.ledger_position("PLAN2")
    show("重试后额度账", after)
    assert after["epsilon_used"] == before["epsilon_used"], "重试不得重复扣账"
    assert retry["release"]["cells"] == answer_first, "重试不得重抽噪声"
    print("✔ 发布与扣账同生共死；恢复后重试幂等，噪声逐位一致")


def scenario_reuse(svc: FedReadService) -> None:
    heading("场景 3：结果复用（同形同版本零消耗；任一要素变化重新计费）")
    u2, u4 = svc.operator("U2"), svc.operator("U4")
    u5 = svc.operator("U5")
    common = dict(plan_id="PLAN2", metric="count", group_by=["region"],
                  filters={"region": "urban"}, version="2026-04-v1",
                  epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")
    svc.submit_request(u5, request_id="R1", **common)
    svc.review(u2, "R1", approve=True)
    r1 = svc.publish(u2, "R1")
    spent_after_r1 = svc.ledger_position("PLAN2")["epsilon_used"]

    # 完全相同的新请求：必须复用，不抽新噪声、不新增额度。
    svc.submit_request(u5, request_id="R2", **common)
    svc.review(u2, "R2", approve=True)
    r2 = svc.publish(u2, "R2")
    spent_after_r2 = svc.ledger_position("PLAN2")["epsilon_used"]
    show("复用判定", {"R2_reused": r2["reused"], "reuse_of": r2.get("reuse_of"),
                     "same_cells": r1["release"]["cells"] == r2["release"]["cells"],
                     "epsilon_delta": spent_after_r2 - spent_after_r1})
    assert r2["reused"] is True and spent_after_r2 == spent_after_r1

    # 改人群（filters）即不同指纹，必须重新判定并计费。
    changed = {**common, "filters": {"region": "rural"}, "epsilon_total": 0.1}
    svc.submit_request(u5, request_id="R3", **changed)
    svc.review(u2, "R3", approve=True)
    r3 = svc.publish(u2, "R3")
    spent_after_r3 = svc.ledger_position("PLAN2")["epsilon_used"]
    show("变更人群后", {"R3_reused": r3["reused"],
                       "epsilon_delta": round(spent_after_r3 - spent_after_r2, 6)})
    assert r3["reused"] is False and abs(
        (spent_after_r3 - spent_after_r2) - 0.1) < 1e-9

    # 改资料版本同样是新指纹（即便版本间数据恰好相同也不复用）。
    v2 = {**common, "version": "2026-04-v2"}
    svc.submit_request(u5, request_id="R4", **v2)
    svc.review(u2, "R4", approve=True)
    try:
        svc.publish(u2, "R4")
        raise AssertionError("v2 上 PLAN2 无授权，发布应按依赖停止")
    except AuthorizationError as exc:
        show("版本变化且授权缺失", {"error": type(exc).__name__,
                                  "message": str(exc)})
    assert svc._request("R4")["state"] == "stopped"
    print("✔ 同形同版本复用且零消耗；人群/版本变化按新请求重新计费与判定")


def scenario_withdrawal(svc: FedReadService) -> None:
    heading("场景 4：用途撤回（按真实依赖停止；已发布保留；不牵连其他计划）")
    u2, u3, u4 = svc.operator("U2"), svc.operator("U3"), svc.operator("U4")
    u5 = svc.operator("U5")
    # PLAN2 上先成立一个发布。
    svc.submit_request(u5, request_id="W1", plan_id="PLAN2", metric="count",
                       group_by=[], filters={}, version="2026-04-v1",
                       epsilon_total=0.1, delta_total=0.0, as_of="2026-05-15")
    svc.review(u2, "W1", approve=True)
    svc.publish(u2, "W1")

    # 再布置一个待审、一个已批未发。
    svc.submit_request(u5, request_id="W2", plan_id="PLAN2", metric="count",
                       group_by=["region"], filters={}, version="2026-04-v1",
                       epsilon_total=0.1, delta_total=0.0, as_of="2026-05-15")
    svc.submit_request(u5, request_id="W3", plan_id="PLAN2", metric="count",
                       group_by=[], filters={}, version="2026-04-v1",
                       epsilon_total=0.1, delta_total=0.0, as_of="2026-05-15")
    svc.review(u2, "W3", approve=True)

    outcome = svc.withdraw_grant(u3, "G5", reason="演练撤回 I2 对 PLAN2/v1 的授权")
    show("撤回结果", outcome)
    states = {r["id"]: r["state"] for r in svc.list_requests(u4, "PLAN2")}
    show("撤回后请求状态", states)
    assert states["W2"] == "stopped" and states["W3"] == "stopped"
    assert states["W1"] == "published", "已发布结果不能收回"

    # 已发布结果仍可查询当时范围与限制。
    kept = svc.get_release(u5, "W1")
    show("已发布结果仍可查", {"request_id": kept["request_id"],
                            "source_grant_ids": kept["source_grant_ids"],
                            "data_digest": kept["data_digest"]})

    # 已批未发请求再发布必须被拒。
    try:
        svc.publish(u2, "W3")
        raise AssertionError("已停止请求不能发布")
    except (StateError, AuthorizationError) as exc:
        show("停止后发布被拒", {"error": type(exc).__name__, "message": str(exc)})

    # 撤回不牵连其他计划：PLAN1 的请求与发布不受 G5 影响。
    plan1_open = svc.list_requests(u4, "PLAN1")
    show("PLAN1 请求未受波及", plan1_open[:3])
    assert all(r["state"] != "stopped" for r in plan1_open), "撤回不得跨计划株连"

    # 额度不退还：已扣 epsilon 保持不变。
    show("撤回后 PLAN2 额度账（已消耗不退还）", svc.ledger_position("PLAN2"))
    print("✔ 撤回按 (计划, 版本) 真实依赖停止未发请求；已发布保留、额度不退、不跨计划株连")


def _db_path(svc: FedReadService) -> str:
    row = svc.conn.execute("PRAGMA database_list").fetchone()
    return row[2]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        scenario_budget_contention(fresh_service(tmp_path / "s1"))
        scenario_crash_recovery(fresh_service(tmp_path / "s2"))
        scenario_reuse(fresh_service(tmp_path / "s3"))
        scenario_withdrawal(fresh_service(tmp_path / "s4"))
        heading("全部场景通过")


if __name__ == "__main__":
    main()
