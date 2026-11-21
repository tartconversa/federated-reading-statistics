"""聚合引擎测试：单位截断、跨机构合并与确定性噪声。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fedread.bootstrap import bootstrap
from fedread.engine import compute, normalize_shape
from fedread.service import FedReadService
from fedread.storage import connect

MATERIALS = Path(__file__).resolve().parents[1] / "materials"


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "engine.db"
        bootstrap(self.db, MATERIALS)
        self.svc = FedReadService(connect(self.db))
        self.conn = self.svc.conn

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _plan(self, plan_id="PLAN2"):
        return self.conn.execute("SELECT * FROM plans WHERE id = ?",
                                 (plan_id,)).fetchone()

    def _shape(self, **overrides):
        params = dict(plan_id="PLAN2", metric="count", group_by=[],
                      filters={}, version="2026-04-v1")
        params.update(overrides)
        return normalize_shape(**params)

    def test_noise_identical_across_connections(self):
        plan = self._plan()
        shape = self._shape()
        seed = plan["noise_key"].encode()
        run1 = compute(self.conn, shape=shape, plan=plan,
                       institution_ids=["I1", "I2", "I3"],
                       epsilon_total=0.5, noise_seed_material=seed)
        other_conn = connect(self.db)
        try:
            run2 = compute(other_conn, shape=shape, plan=plan,
                           institution_ids=["I1", "I2", "I3"],
                           epsilon_total=0.5, noise_seed_material=seed)
        finally:
            other_conn.close()
        self.assertEqual(run1["cells"], run2["cells"])
        self.assertEqual(run1["data_digest"], run2["data_digest"])

    def test_different_plan_keys_produce_different_noise(self):
        plan = self._plan()
        shape = self._shape()
        run1 = compute(self.conn, shape=shape, plan=plan,
                       institution_ids=["I1", "I2", "I3"],
                       epsilon_total=0.5,
                       noise_seed_material=plan["noise_key"].encode())
        run2 = compute(self.conn, shape=shape, plan=plan,
                       institution_ids=["I1", "I2", "I3"],
                       epsilon_total=0.5,
                       noise_seed_material=b"a-different-plan-key")
        self.assertNotEqual(run1["cells"], run2["cells"])

    def test_cross_institution_units_are_deduplicated(self):
        # 每个 person 在 3 家机构各有记录；PLAN2 单位=person、上限=1。
        # 三家合并后的单位计数应约为 150 人，而不是 600 行或 450 截断行。
        plan = self._plan()
        shape = self._shape()
        out = compute(self.conn, shape=shape, plan=plan,
                      institution_ids=["I1", "I2", "I3"],
                      epsilon_total=10.0,  # 大 epsilon => 噪声很小，便于核对量级
                      noise_seed_material=plan["noise_key"].encode())
        noisy = out["cells"][0]["unit_count_noisy"]
        self.assertAlmostEqual(noisy, 150, delta=2)
        self.assertEqual(out["contribution_limit_applied"], 1)

    def test_subset_of_institutions_changes_digest(self):
        plan = self._plan()
        shape = self._shape()
        full = compute(self.conn, shape=shape, plan=plan,
                       institution_ids=["I1", "I2", "I3"],
                       epsilon_total=0.5,
                       noise_seed_material=plan["noise_key"].encode())
        partial = compute(self.conn, shape=shape, plan=plan,
                          institution_ids=["I1"], epsilon_total=0.5,
                          noise_seed_material=plan["noise_key"].encode())
        self.assertNotEqual(full["data_digest"], partial["data_digest"])

    def test_household_limit_truncates_records_deterministically(self):
        plan = self._plan("PLAN1")
        shape = normalize_shape(plan_id="PLAN1", metric="count", group_by=[],
                                filters={}, version="2026-04-v1")
        out = compute(self.conn, shape=shape, plan=plan,
                      institution_ids=["I1", "I2", "I3"],
                      epsilon_total=10.0,
                      noise_seed_material=plan["noise_key"].encode())
        # 75 个家庭；每家庭上限 2 条，单位计数与行数无关，量级应在 75 附近。
        self.assertAlmostEqual(out["cells"][0]["unit_count_noisy"], 75, delta=2)
        self.assertEqual(out["adjacency_unit"], "household")


if __name__ == "__main__":
    unittest.main()
