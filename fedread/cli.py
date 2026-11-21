"""本地命令行界面。

角色（无密码，本地演练）：
  researcher   预览结构/精度、提交请求、查询获准结果
  reviewer     审核批准/驳回、执行发布
  institution  管理授权范围（撤回/恢复）
  administrator 额度核对、披露审计、中断恢复、故障演练

所有输出为 JSON；错误信息只描述请求层面的问题，不含受限明细。
数据库路径通过 --db 或环境变量 FEDREAD_DB 指定，默认 ./state/fedread.db。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from .bootstrap import bootstrap
from .errors import FedReadError
from .service import FedReadService
from .storage import connect

DEFAULT_DB = os.environ.get("FEDREAD_DB", "state/fedread.db")


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _service(args) -> FedReadService:
    return FedReadService(connect(args.db))


def _json_arg(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 参数解析失败：{exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fedread",
        description="本地阅读联合研究：差分隐私受限统计与共享额度管理（合成资料）",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="本地 SQLite 库路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="导入合成资料并初始化本地库")
    p.add_argument("--materials", default="materials", help="资料目录")

    p = sub.add_parser("catalog", help="列出计划、版本、机构与授权范围")
    p.add_argument("--actor", required=True)
    p.add_argument("--plan", help="可选：限定计划")

    p = sub.add_parser("preview", help="预览可申请结构与预计精度（不返回真实统计）")
    p.add_argument("--actor", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--group-by", default="[]", help="JSON 数组，如 [\"region\"]")
    p.add_argument("--filters", default="{}", help="JSON 对象")
    p.add_argument("--version", required=True)
    p.add_argument("--epsilon", type=float, required=True)

    p = sub.add_parser("submit", help="研究员提交请求")
    p.add_argument("--actor", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--metric", required=True)
    p.add_argument("--group-by", default="[]")
    p.add_argument("--filters", default="{}")
    p.add_argument("--version", required=True)
    p.add_argument("--epsilon", type=float, required=True)
    p.add_argument("--delta", type=float, default=0.0)
    p.add_argument("--as-of", default=None,
                   help="提出日 YYYY-MM-DD，默认今天；须落在计划期限窗口内")

    p = sub.add_parser("list", help="列出计划下的请求与状态")
    p.add_argument("--actor", required=True)
    p.add_argument("--plan", required=True)

    p = sub.add_parser("review", help="审核请求")
    p.add_argument("--actor", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--decision", choices=("approve", "reject"), required=True)
    p.add_argument("--note", default="")

    p = sub.add_parser("publish", help="对已批准请求执行差分隐私发布")
    p.add_argument("--actor", required=True)
    p.add_argument("--id", required=True)

    p = sub.add_parser("result", help="查询已成立发布的结果与解释")
    p.add_argument("--actor", required=True)
    p.add_argument("--id", required=True)

    p = sub.add_parser("budget", help="查看计划共享额度账")
    p.add_argument("--actor", required=True)
    p.add_argument("--plan", required=True)

    p = sub.add_parser("withdraw", help="资料方撤回机构用途授权")
    p.add_argument("--actor", required=True)
    p.add_argument("--grant", required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("reinstate", help="资料方恢复机构授权")
    p.add_argument("--actor", required=True)
    p.add_argument("--grant", required=True)

    p = sub.add_parser("audit", help="管理员查看只追加审计记录")
    p.add_argument("--actor", required=True)
    p.add_argument("--plan", default=None)

    p = sub.add_parser("disclosures", help="管理员核对全部实际披露")
    p.add_argument("--actor", required=True)

    p = sub.add_parser("recover", help="管理员中断续办：按持久事实收敛状态")
    p.add_argument("--actor", required=True)

    p = sub.add_parser("arm-crash", help="演练：在发布的指定位置模拟崩溃")
    p.add_argument("--actor", required=True)
    p.add_argument("--point", choices=("before_commit", "after_publish"),
                   default="after_publish")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "init":
            _print(bootstrap(args.db, Path(args.materials)))
            return 0

        svc = _service(args)
        actor = svc.operator(args.actor) if args.cmd != "init" else None

        if args.cmd == "catalog":
            _print(_catalog(svc, args.plan))
        elif args.cmd == "preview":
            _print(svc.preview(
                actor, plan_id=args.plan, metric=args.metric,
                group_by=_json_arg(args.group_by),
                filters=_json_arg(args.filters),
                version=args.version, epsilon_total=args.epsilon))
        elif args.cmd == "submit":
            _print(svc.submit_request(
                actor, request_id=args.id, plan_id=args.plan,
                metric=args.metric, group_by=_json_arg(args.group_by),
                filters=_json_arg(args.filters), version=args.version,
                epsilon_total=args.epsilon, delta_total=args.delta,
                as_of=args.as_of))
        elif args.cmd == "list":
            _print(svc.list_requests(actor, args.plan))
        elif args.cmd == "review":
            _print(svc.review(actor, args.id,
                             approve=(args.decision == "approve"), note=args.note))
        elif args.cmd == "publish":
            _print(svc.publish(actor, args.id))
        elif args.cmd == "result":
            _print(svc.get_release(actor, args.id))
        elif args.cmd == "budget":
            _print(svc.ledger_position(args.plan))
        elif args.cmd == "withdraw":
            _print(svc.withdraw_grant(actor, args.grant, reason=args.reason))
        elif args.cmd == "reinstate":
            _print(svc.reinstate_grant(actor, args.grant))
        elif args.cmd == "audit":
            _print(svc.audit_trail(actor, plan_id=args.plan))
        elif args.cmd == "disclosures":
            _print(svc.disclosure_report(actor))
        elif args.cmd == "recover":
            _print(svc.recover(actor))
        elif args.cmd == "arm-crash":
            svc.arm_crash(actor, point=args.point)
            _print({"armed": "crash_before_commit"
                    if args.point == "before_commit"
                    else "crash_after_publish"})
        else:  # pragma: no cover
            raise SystemExit(2)
        return 0
    except FedReadError as exc:
        # 统一脱敏错误出口：仅回显业务级原因，不含明细。
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # 演练注入的崩溃点：明确告知是模拟故障，持久状态以库内事实为准。
        if str(exc).startswith("SIMULATED CRASH"):
            print(json.dumps(
                {"error": "SimulatedCrash", "message": str(exc),
                 "hint": "请管理员执行 recover 后按请求状态查询，勿凭进程输出猜测"},
                ensure_ascii=False), file=sys.stderr)
            return 3
        traceback.print_exc()
        print(json.dumps({"error": "InternalError",
                          "message": "内部错误，未返回任何受限数据"},
                         ensure_ascii=False), file=sys.stderr)
        return 2


def _catalog(svc: FedReadService, plan_id: str | None) -> dict:
    conn = svc.conn
    out: dict = {"institutions": [dict(r) for r in conn.execute(
        "SELECT id, label FROM institutions ORDER BY id")],
        "versions": [dict(r) for r in conn.execute(
        "SELECT id, received_on, description FROM versions ORDER BY id")]}
    plan_sql = "SELECT * FROM plans"
    params: tuple = ()
    if plan_id:
        plan_sql += " WHERE id = ?"
        params = (plan_id,)
    plans = []
    for row in conn.execute(plan_sql + " ORDER BY id", params):
        from .storage import loads
        item = {k: row[k] for k in row.keys() if k != "noise_key"}
        item["allowed_metrics"] = loads(row["allowed_metrics"])
        item["allowed_groups"] = loads(row["allowed_groups"])
        item["grants"] = [dict(g) for g in conn.execute(
            "SELECT id, institution_id, plan_id, version, approved_on, withdrawn_on "
            "FROM grants WHERE plan_id = ? ORDER BY id", (row["id"],))]
        plans.append(item)
    out["plans"] = plans
    return out


if __name__ == "__main__":
    raise SystemExit(main())
