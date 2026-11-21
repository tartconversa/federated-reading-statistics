"""差分隐私机制原语测试：确定性、敏感度与经验邻接保证。"""
from __future__ import annotations

import math
import unittest
from collections import Counter

from fedread import dp


class DeterminismTests(unittest.TestCase):
    def test_same_label_same_noise(self):
        rng1 = dp.DeterministicLaplace(b"plan-key")
        rng2 = dp.DeterministicLaplace(b"plan-key")
        self.assertEqual(rng1.laplace("cell|count", 3.0),
                         rng2.laplace("cell|count", 3.0))

    def test_domain_separation(self):
        rng = dp.DeterministicLaplace(b"plan-key")
        # 不同标签派生不同抽样；不同密钥也相互独立。
        self.assertNotEqual(rng.laplace("a", 1.0), rng.laplace("b", 1.0))
        other = dp.DeterministicLaplace(b"other-key")
        self.assertNotEqual(rng.laplace("a", 1.0), other.laplace("a", 1.0))

    def test_distribution_shape(self):
        rng = dp.DeterministicLaplace(b"stat-key")
        samples = [rng.laplace(f"x{i}", 2.0) for i in range(4000)]
        mean = sum(samples) / len(samples)
        self.assertAlmostEqual(mean, 0.0, delta=0.15)
        within_b = sum(1 for x in samples if abs(x) <= 2.0) / len(samples)
        # Lap(b) 落在 [-b,b] 的概率为 1 - exp(-1) ≈ 0.632
        self.assertAlmostEqual(within_b, 1 - math.exp(-1), delta=0.04)


class SensitivityTests(unittest.TestCase):
    def test_count_sensitivity_is_unit_limit_not_row_count(self):
        self.assertEqual(dp.count_sensitivity(1), 1)
        self.assertEqual(dp.count_sensitivity(2), 2)

    def test_sum_sensitivity_uses_clamp_and_limit(self):
        # 每单位最多 2 条、每条裁剪到 [0,120]：最坏单位影响 240。
        self.assertEqual(dp.sum_sensitivity(2, 0, 120), 240)
        # 负下界按最大绝对值计。
        self.assertEqual(dp.sum_sensitivity(3, -50, 20), 150)
        with self.assertRaises(ValueError):
            dp.sum_sensitivity(1, 10, 0)

    def test_clamp(self):
        self.assertEqual(dp.clamp_value(200, 0, 120), 120)
        self.assertEqual(dp.clamp_value(-5, 0, 120), 0)
        self.assertEqual(dp.clamp_value(41, 0, 120), 41)


class LaplaceMechanismTests(unittest.TestCase):
    def test_moe95_formula(self):
        spec = dp.build_mechanism(sensitivity=2, epsilon_cell=0.5)
        self.assertAlmostEqual(spec.scale, 4.0)
        self.assertAlmostEqual(spec.moe95, 4.0 * math.log(20), places=9)
        self.assertEqual(spec.delta_cell, 0.0)

    def test_empirical_epsilon_guarantee(self):
        """经验验证 (ε,0)-DP 不等式：对单值 0/1 邻接输入，
        任意输出集合上的似然比不超过 exp(ε)（拉普拉斯机制定理）。

        这里用分箱频率在大样本上检验密度比上界，而不是只看噪声大小。
        """
        epsilon, sensitivity = 0.8, 1
        scale = dp.laplace_scale(sensitivity, epsilon)
        rng0 = dp.DeterministicLaplace(b"empirical")
        n = 6000
        x0 = [rng0.laplace(f"d0-{i}", scale) for i in range(n)]
        x1 = [v + sensitivity for v in x0]  # 邻接数据集输出平移 Δ
        bins = [(-10, -2), (-2, 0), (0, 2), (2, 10)]
        for lo, hi in bins:
            p0 = sum(lo <= v < hi for v in x0) / n
            p1 = sum(lo <= v < hi for v in x1) / n
            if p0 > 0.01 and p1 > 0.01:
                ratio = max(p1 / p0, p0 / p1)
                # 理论上界 exp(ε)=exp(0.8)≈2.226，留样本波动余量。
                self.assertLess(ratio, math.exp(epsilon) * 1.4)


if __name__ == "__main__":
    unittest.main()
