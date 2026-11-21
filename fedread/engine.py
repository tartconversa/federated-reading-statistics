"""受控联合聚合引擎。

唯一允许读取 records_raw 的模块。输出只含按保护单位聚合、加噪后的统计，
绝不返回记录行数、人员/家庭编号或任何明细。

关键口径：
* 统计单位是计划声明的保护单位（person/household），不是记录行数；
* 跨机构同人/同家庭先合并，再按 contribution_limit 做确定性截断；
* 邻接定义为加入/移除一个完整单位，敏感度按截断后的最坏单位影响推导；
* 噪声由计划密钥 + 发布指纹 + 单元/量标签确定性派生，重试不重抽。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from . import dp
from .config import FILTER_FIELDS, GROUP_FIELDS, INDICATOR_METRICS, VALUE_METRICS
from .errors import ValidationError
from .storage import dumps, loads


@dataclass(frozen=True)
class RequestShape:
    """请求的规范化形状，用于指纹与结构预览。"""

    plan_id: str
    metric: str
    group_by: tuple[str, ...]
    filters: dict
    version: str

    def fingerprint(self) -> str:
        canonical = dumps(
            {
                "plan_id": self.plan_id,
                "metric": self.metric,
                "group_by": list(self.group_by),
                "filters": self.filters,
                "version": self.version,
            }
        )
        return hashlib.sha256(b"fedread-fp-v1|" + canonical.encode("utf-8")).hexdigest()


def normalize_shape(
    *, plan_id: str, metric: str, group_by: list[str], filters: dict, version: str
) -> RequestShape:
    if not isinstance(group_by, list) or any(g not in GROUP_FIELDS for g in group_by):
        raise ValidationError("分组字段不受支持")
    if len(group_by) != len(set(group_by)):
        raise ValidationError("分组字段重复")
    if not isinstance(filters, dict):
        raise ValidationError("人群条件必须是对象")
    unknown = set(filters) - set(FILTER_FIELDS)
    if unknown:
        raise ValidationError("人群条件含不支持字段：" + ",".join(sorted(unknown)))
    for key, value in filters.items():
        expected = FILTER_FIELDS[key]
        if not isinstance(value, expected) or isinstance(value, bool) and expected is int:
            raise ValidationError(f"人群条件 {key} 类型不正确")
    if "grade_min" in filters and "grade_max" in filters:
        if filters["grade_min"] > filters["grade_max"]:
            raise ValidationError("年级下界大于上界，人群为空口径")
    if "on_from" in filters and "on_to" in filters:
        if filters["on_from"] >= filters["on_to"]:
            raise ValidationError("观测日区间为空口径")
    return RequestShape(
        plan_id=plan_id,
        metric=metric,
        group_by=tuple(sorted(group_by)),
        filters=dict(sorted(filters.items())),
        version=version,
    )


def _where_sql(filters: dict, institution_ids: list[str]) -> tuple[str, list]:
    # version 由调用方通过机构授权集合限定，这里仍显式限定。
    clauses = ["r.institution_id IN (%s)" % ",".join("?" * len(institution_ids))]
    params: list = list(institution_ids)
    clauses.append("r.version = ?")
    params.append(filters["__version__"])
    if "grade_min" in filters:
        clauses.append("r.grade >= ?")
        params.append(filters["grade_min"])
    if "grade_max" in filters:
        clauses.append("r.grade <= ?")
        params.append(filters["grade_max"])
    if "region" in filters:
        clauses.append("r.region = ?")
        params.append(filters["region"])
    if "access_barrier" in filters:
        clauses.append("r.access_barrier = ?")
        params.append(1 if filters["access_barrier"] else 0)
    if "on_from" in filters:
        clauses.append("r.on_date >= ?")
        params.append(filters["on_from"])
    if "on_to" in filters:
        clauses.append("r.on_date < ?")
        params.append(filters["on_to"])
    return " AND ".join(clauses), params


def _group_key(row: sqlite3.Row, group_by: tuple[str, ...]) -> str:
    if not group_by:
        return "ALL"
    return "|".join(str(row[g]) for g in group_by)


def _unit_column(unit: str) -> str:
    return "person" if unit == "person" else "household"


def compute(
    conn: sqlite3.Connection,
    *,
    shape: RequestShape,
    plan: sqlite3.Row,
    institution_ids: list[str],
    epsilon_total: float,
    noise_seed_material: bytes,
) -> dict:
    """执行一次受控联合聚合，返回可发布载荷与机制解释。

    institution_ids 必须已由服务层校验为“当前有效且覆盖该计划/版本”的授权。
    """
    unit_col = _unit_column(plan["unit"])
    limit = int(plan["contribution_limit"])
    lower, upper = float(plan["lower"]), float(plan["upper"])
    metric = shape.metric
    group_by = shape.group_by

    query_filters = dict(shape.filters)
    query_filters["__version__"] = shape.version
    where, params = _where_sql(query_filters, institution_ids)

    group_select = ", ".join(f"r.{g}" for g in group_by)
    group_prefix = (group_select + ", ") if group_select else ""

    # 单位贡献截断：按 (单元, 单位, 机构, 观测日, 记录id) 确定性排序，
    # 每单位只保留前 limit 条。跨机构同单位记录在同一排序流中竞争，
    # 保证一个邻接单位的 L1 影响不超过 limit。
    ranked_sql = f"""
        SELECT * FROM (
            SELECT r.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY {group_prefix} r.{unit_col}
                       ORDER BY r.institution_id, r.on_date, r.id
                   ) AS rn
            FROM records_raw r
            WHERE {where}
        ) WHERE rn <= ?
    """
    params_ranked = params + [limit]
    rows = conn.execute(ranked_sql, params_ranked).fetchall()

    cells: dict[str, dict] = {}
    for row in rows:
        key = _group_key(row, group_by)
        cell = cells.setdefault(key, {"count_units": set(), "sum": 0.0,
                                      "indicator": 0, "n_records": 0})
        cell["count_units"].add(row[unit_col])
        cell["n_records"] += 1
        if metric in VALUE_METRICS:
            cell["sum"] += dp.clamp_value(float(row["reading_minutes"]), lower, upper)
        if metric in INDICATOR_METRICS:
            cell["indicator"] += int(row["access_barrier"])

    # 资料指纹：实际参与计算的受限数据范围的稳定摘要（不含答案本身）。
    digest_input = dumps(
        {
            "version": shape.version,
            "institutions": sorted(institution_ids),
            "filters": shape.filters,
            "unit": plan["unit"],
            "group_by": list(group_by),
            "metric": metric,
            "contribution_limit": limit,
        }
    )
    data_digest = hashlib.sha256(
        b"fedread-data-v1|" + digest_input.encode("utf-8")
    ).hexdigest()

    rng = dp.DeterministicLaplace(noise_seed_material + b"|" + shape.fingerprint().encode())

    # 敏感度（L1，单位邻接）。
    s_count = dp.count_sensitivity(limit)
    result_cells = []
    mechanisms = []

    if metric == "count":
        spec = dp.build_mechanism(s_count, epsilon_total)
        for key in sorted(cells):
            raw = float(len(cells[key]["count_units"]))
            noisy = max(0.0, raw + rng.laplace(f"count|{key}", spec.scale))
            result_cells.append({
                "group": key,
                "unit_count_noisy": round(noisy, 6),
                "raw_unit_count_released": False,
            })
        mechanisms.append(("count", spec.explain()))

    elif metric == "barrier_fraction":
        # 发布两个量：受障碍单位计数（指示和）与单位计数，比例由二者派生。
        eps_each = epsilon_total / 2.0
        spec_n = dp.build_mechanism(s_count, eps_each)
        spec_b = dp.build_mechanism(dp.indicator_sensitivity(limit), eps_each)
        for key in sorted(cells):
            cell = cells[key]
            n_noisy = max(1.0, len(cell["count_units"])
                          + rng.laplace(f"barrier_n|{key}", spec_n.scale))
            b_noisy = max(0.0, cell["indicator"]
                          + rng.laplace(f"barrier_b|{key}", spec_b.scale))
            fraction = min(1.0, max(0.0, b_noisy / n_noisy))
            result_cells.append({
                "group": key,
                "barrier_fraction_noisy": round(fraction, 6),
                "denominator_unit_count_noisy": round(n_noisy, 6),
                "post_processed": "比例裁剪到[0,1]，计数裁剪到非负",
            })
        mechanisms.append(("unit_count", spec_n.explain()))
        mechanisms.append(("barrier_indicator_sum", spec_b.explain()))

    elif metric == "mean_reading_minutes":
        # 联合发布 (裁剪分钟和, 单位计数)，均值 = 和/计数，总 ε 对半分。
        eps_each = epsilon_total / 2.0
        s_sum = dp.sum_sensitivity(limit, lower, upper)
        spec_n = dp.build_mechanism(s_count, eps_each)
        spec_s = dp.build_mechanism(s_sum, eps_each)
        for key in sorted(cells):
            cell = cells[key]
            n_noisy = max(1.0, len(cell["count_units"])
                          + rng.laplace(f"mean_n|{key}", spec_n.scale))
            s_noisy = cell["sum"] + rng.laplace(f"mean_s|{key}", spec_s.scale)
            mean = s_noisy / n_noisy
            result_cells.append({
                "group": key,
                "mean_minutes_noisy": round(mean, 6),
                "clipped_sum_noisy": round(s_noisy, 6),
                "denominator_unit_count_noisy": round(n_noisy, 6),
                "clamp_bounds": [lower, upper],
                "post_processed": "均值由两个独立加噪量相除，未对均值本身再裁剪",
            })
        mechanisms.append(("unit_count", spec_n.explain()))
        mechanisms.append(("clipped_value_sum", spec_s.explain()))
    else:  # pragma: no cover - 服务层已拦截
        raise ValidationError("指标不受支持")

    return {
        "cells": result_cells,
        "mechanisms": dict(mechanisms),
        "data_digest": data_digest,
        "adjacency_unit": plan["unit"],
        "contribution_limit_applied": limit,
        "institution_count": len(institution_ids),
        "note": "结果为差分隐私加噪统计；空单元不展示以避免单元存在性推断，"
                "但单元结构来自计划白名单而非数据。",
    }
