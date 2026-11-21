"""领域错误类型。

所有面向研究员的错误信息只描述请求本身的问题，绝不回显受限明细
（不出现 person/household/record 编号或原始值）。
"""


class FedReadError(Exception):
    """应用层错误基类，消息可安全展示给操作者。"""


class ValidationError(FedReadError):
    """请求结构或研究计划口径不合法。"""


class AuthorizationError(FedReadError):
    """机构用途授权不满足请求的真实依赖。"""


class BudgetError(FedReadError):
    """共享隐私额度、披露次数或单位贡献界限无法满足。"""


class StateError(FedReadError):
    """请求当前状态不允许该操作。"""


class NotFound(FedReadError):
    """对象不存在或对该操作者不可见（不区分二者，避免枚举探测）。"""


class AccessDenied(FedReadError):
    """本地角色无权执行该操作。"""
