"""差分隐私机制原语（仅标准库）。

保证口径（与研究计划声明一致，详见 docs/隐私保证.md）：

* 单位邻接：邻接数据集定义为加入或移除一个*完整保护单位*
  （person 或 household，由计划 unit 字段声明）。同一单位跨机构、跨多条
  记录的全部观测一起变化，因此统计单位是保护单位，绝不是记录行数。
* 每单位贡献上限：先按 contribution_limit 对每单位参与记录做确定性截断，
  数值再裁剪到 [lower, upper]。由此得到每条发布向量的 L1 敏感度上界。
* 机制：L1 敏感度 Δ 上的拉普拉斯机制，每维独立加 Lap(Δ/ε_cell)。
  这是 (ε_cell, 0)-DP；本应用不使用高斯机制，delta 仅作计划级簿记并要求
  组合后不超过计划 delta 上限（本机制实际 delta=0）。
* 组合：同一计划下全部已发布披露按顺序组合（sequential composition），
  保守地把 ε 线性相加、delta 相加；分组各单元也按顺序组合计入。
  完全相同且同资料版本的请求复用既有发布，不新增任何消耗。
* 确定性噪声：噪声由 (计划密钥, 发布指纹, 单元标签, 量标签) 经
  SHA-256 派生流密码式确定性采样。重试/中断续办得到同一噪声，
  研究员无法通过重复申请挑选有利答案；不同单元/量/计划的域分隔标签不同，
  不会复用同一抽样。
"""
from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass

from .config import MOE95_LAPLACE


class DeterministicLaplace:
    """由种子材料确定性派生的拉普拉斯采样器。

    内部用 SHA-256 生成均匀样本（53 位精度），再经逆 CDF 变换：
        u ~ Uniform(0, 1) ；x = -b * sign(u-0.5) * ln(1 - 2|u-0.5|)
    相同 (seed, label) 永远得到相同噪声；不同 label 以域分隔避免关联。
    """

    def __init__(self, seed_material: bytes):
        self._key = hashlib.sha256(b"fedread-laplace-v1|" + seed_material).digest()

    def _uniform(self, label: str) -> float:
        # 计数器式派生：同一 label 取第 0 块；需要多个均匀数时扩展计数。
        digest = hashlib.sha256(self._key + label.encode("utf-8")).digest()
        # 取 53 位尾数，严格落在 (0, 1)，避开逆 CDF 的 0 点。
        bits = struct.unpack(">Q", digest[:8])[0] >> 11
        u = (bits + 1) / (1 << 53)
        return min(u, 1.0 - 1e-15)

    def laplace(self, label: str, scale: float) -> float:
        if scale <= 0:
            raise ValueError("拉普拉斯尺度必须为正")
        u = self._uniform(label)
        centered = u - 0.5
        return -scale * math.copysign(math.log(1.0 - 2.0 * abs(centered)), centered)


def laplace_scale(sensitivity: float, epsilon: float) -> float:
    """Lap(Δ/ε) 的尺度参数。"""
    if epsilon <= 0:
        raise ValueError("epsilon 必须为正数")
    if sensitivity <= 0:
        raise ValueError("敏感度必须为正数")
    return sensitivity / epsilon


def moe95(scale: float) -> float:
    """拉普拉斯分布的 95% 边际误差（对称双侧）。"""
    return MOE95_LAPLACE * scale


@dataclass(frozen=True)
class MechanismSpec:
    """一条数值发布向量的机制说明，可向研究员解释。"""

    sensitivity: float          # L1 敏感度上界 Δ
    epsilon_cell: float         # 每个单元/量的 epsilon
    scale: float                # Laplace 尺度 b = Δ/ε
    moe95: float                # 95% 边际误差
    mechanism: str = "Laplace"
    delta_cell: float = 0.0     # 拉普拉斯机制为纯 DP

    def explain(self) -> dict:
        return {
            "mechanism": self.mechanism,
            "adjacency": "加入或移除一个完整保护单位（计划声明的 person/household）",
            "l1_sensitivity_bound": round(self.sensitivity, 6),
            "epsilon_per_cell": self.epsilon_cell,
            "delta_per_cell": self.delta_cell,
            "laplace_scale": round(self.scale, 6),
            "moe95": round(self.moe95, 6),
            "note": "95% 边际误差 = ln(20)*Δ/ε；ε、δ 为顺序组合的实际消耗",
        }


def build_mechanism(sensitivity: float, epsilon_cell: float) -> MechanismSpec:
    scale = laplace_scale(sensitivity, epsilon_cell)
    return MechanismSpec(
        sensitivity=sensitivity,
        epsilon_cell=epsilon_cell,
        scale=scale,
        moe95=moe95(scale),
    )


# --- 敏感度推导 -----------------------------------------------------------

def count_sensitivity(contribution_limit: int) -> int:
    """单位计数的 L1 敏感度：一个单位最多计入 contribution_limit 条记录，
    截断后一个邻接单位对（单元, 总量）计数向量的 L1 影响不超过该上限。"""
    if contribution_limit < 1:
        raise ValueError("每单位贡献上限至少为 1")
    return contribution_limit


def sum_sensitivity(contribution_limit: int, lower: float, upper: float) -> float:
    """裁剪后数值和的 L1 敏感度。

    每条保留记录的取值落在 [lower, upper]，移除一个单位最多带走
    contribution_limit 条记录；按最大绝对贡献计：
        Δ_sum = contribution_limit * max(|lower|, |upper|)
    """
    if upper < lower:
        raise ValueError("数值界限上下限颠倒")
    return contribution_limit * max(abs(lower), abs(upper))


def indicator_sensitivity(contribution_limit: int) -> int:
    """0/1 指示值之和的 L1 敏感度，等同计数敏感度。"""
    return count_sensitivity(contribution_limit)


def clamp_value(value: float, lower: float, upper: float) -> float:
    """数值裁剪到计划声明界限。"""
    return max(lower, min(upper, value))
