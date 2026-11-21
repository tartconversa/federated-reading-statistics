"""本地应用的固定配置与口径常量。

仅依赖标准库；所有机构与个人记录均为合成资料，应用不连接任何真实账户。
"""
from __future__ import annotations

# 支持的指标口径。
# count                : 单位计数（按保护单位去重并裁剪后计数）
# mean_reading_minutes : 阅读分钟均值，由受保护的(裁剪和, 计数)两个量联合发布
# barrier_fraction     : access_barrier 比例，本质是 0/1 值的均值
METRICS = ("count", "mean_reading_minutes", "barrier_fraction")

# 指标是否需要数值型贡献（用于 lower/upper 裁剪界限）。
VALUE_METRICS = frozenset({"mean_reading_minutes"})
# 指标以 0/1 指示值贡献（比例类）。
INDICATOR_METRICS = frozenset({"barrier_fraction"})

# 允许出现在 filters 中的人群字段及其类型。
FILTER_FIELDS = {
    "grade_min": int,
    "grade_max": int,
    "region": str,
    "access_barrier": bool,
    "on_from": str,
    "on_to": str,
}

# 可用于分组的字段（计划再做白名单收敛）。
GROUP_FIELDS = ("region", "grade")

# 请求状态机。
STATE_DRAFT = "draft"                # 已提交，待审核
STATE_APPROVED = "approved"          # 审核通过，尚未发布
STATE_PUBLISHED = "published"        # 已正式披露，额度已扣
STATE_REJECTED = "rejected"          # 审核驳回（不扣额度，不可复活）
STATE_STOPPED = "stopped"            # 因授权撤回按真实依赖停止
STATE_BUDGET_DENIED = "budget_denied"  # 发布时共享额度竞争失败，未扣额度

TERMINAL_STATES = frozenset(
    {STATE_PUBLISHED, STATE_REJECTED, STATE_STOPPED, STATE_BUDGET_DENIED}
)

# 日志中允许研究员看到的事件类型；其余仅管理员/审核可见。
RESEARCHER_VISIBLE_EVENTS = frozenset(
    {
        "request_submitted",
        "request_approved",
        "request_rejected",
        "request_stopped",
        "request_published",
        "request_reused",
        "budget_denied",
    }
)

ROLE_RESEARCHER = "researcher"
ROLE_REVIEWER = "reviewer"
ROLE_INSTITUTION = "institution"
ROLE_ADMINISTRATOR = "administrator"
ROLES = (ROLE_RESEARCHER, ROLE_REVIEWER, ROLE_INSTITUTION, ROLE_ADMINISTRATOR)

# 拉普拉斯机制 95% 边际误差系数：b * ln(20) ≈ 2.9957b。
MOE95_LAPLACE = 2.995732273553991
