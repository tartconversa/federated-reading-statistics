"""服务层集成测试：授权依赖、共享额度、复用、撤回与角色隔离。

每个用例在临时目录自建库；全部数据为合成资料。
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from fedread.bootstrap import bootstrap
from fedread.errors import (
    AccessDenied,
    AuthorizationError,
    BudgetError,
    StateError,
    ValidationError,
)
from fedread.service import FedReadService
from fedread.storage import connect

MATERIALS = Path(__file__).resolve().parents[1] / "materials"


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        bootstrap(self.db, MATERIALS)
        self.svc = FedReadService(connect(self.db))
        self.u1 = self.svc.operator("U1")   # researcher PLAN1
        self.u2 = self.svc.operator("U2")   # reviewer
        self.u3 = self.svc.operator("U3")   # institution
        self.u4 = self.svc.operator("U4")   # administrator
        self.u5 = self.svc.operator("U5")   # researcher PLAN2
        self.u6 = self.svc.operator("U6")   # institution PLAN2

    def tearDown(self):
        self.svc.conn.close()
        self.tmp.cleanup()

    def _publish_plan2_count(self, rid, epsilon=0.3, groups=None, filters=None):
        self.svc.submit_request(
            self.u5, request_id=rid, plan_id="PLAN2", metric="count",
            group_by=groups or [], filters=filters or {},
            version="2026-04-v1", epsilon_total=epsilon, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, rid, approve=True)
        return self.svc.publish(self.u2, rid)

    # -- 权限与脱敏 ---------------------------------------------------------

    def test_researcher_cannot_review_or_publish(self):
        self.svc.submit_request(
            self.u5, request_id="P1", plan_id="PLAN2", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")
        with self.assertRaises(AccessDenied):
            self.svc.review(self.u5, "P1", approve=True)
        with self.assertRaises(AccessDenied):
            self.svc.publish(self.u5, "P1")

    def test_researcher_cannot_read_audit_or_raw_records(self):
        with self.assertRaises(AccessDenied):
            self.svc.audit_trail(self.u5)
        with self.assertRaises(AccessDenied):
            self.svc.disclosure_report(self.u5)
        # 引擎外不存在任何返回 records_raw 的服务方法。
        self.assertFalse(hasattr(self.svc, "export_records"))

    def test_plan_isolation_between_researchers(self):
        with self.assertRaises(AccessDenied):
            self.svc.submit_request(
                self.u1, request_id="X1", plan_id="PLAN2", metric="count",
                group_by=[], filters={}, version="2026-04-v1",
                epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")

    # -- 校验与计划口径 -----------------------------------------------------

    def test_metric_and_group_whitelist(self):
        with self.assertRaises(ValidationError):
            self.svc.submit_request(
                self.u5, request_id="V1", plan_id="PLAN2",
                metric="mean_reading_minutes",  # PLAN2 不允许
                group_by=[], filters={}, version="2026-04-v1",
                epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")
        with self.assertRaises(ValidationError):
            self.svc.submit_request(
                self.u5, request_id="V2", plan_id="PLAN2", metric="count",
                group_by=["grade"],  # PLAN2 仅允许 region
                filters={}, version="2026-04-v1",
                epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")

    def test_single_epsilon_over_total_budget_rejected_upfront(self):
        with self.assertRaises(BudgetError):
            self.svc.submit_request(
                self.u5, request_id="V3", plan_id="PLAN2", metric="count",
                group_by=[], filters={}, version="2026-04-v1",
                epsilon_total=5.0, delta_total=0.0, as_of="2026-05-15")  # 总预算仅 1.0

    def test_submission_outside_plan_term_window_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.submit_request(
                self.u5, request_id="V4", plan_id="PLAN2", metric="count",
                group_by=[], filters={}, version="2026-04-v1",
                epsilon_total=0.2, delta_total=0.0,
                as_of="2026-08-01")  # 止日不含，窗口外
        # 窗口边界内可正常提交。
        self.svc.submit_request(
            self.u5, request_id="V5", plan_id="PLAN2", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.2, delta_total=0.0,
            as_of="2026-07-31")

    def test_preview_contains_no_real_statistics(self):
        preview = self.svc.preview(
            self.u5, plan_id="PLAN2", metric="count", group_by=["region"],
            filters={}, version="2026-04-v1", epsilon_total=0.3)
        self.assertTrue(preview["preview"])
        self.assertNotIn("cells", preview)
        self.assertIn("mechanisms", preview)
        self.assertEqual(preview["declared_cells"], ["rural", "urban"])
        self.assertTrue(preview["feasible"])

    def test_preview_flags_missing_grant_and_hints(self):
        preview = self.svc.preview(
            self.u1, plan_id="PLAN1", metric="count", group_by=["region"],
            filters={}, version="2026-04-v2", epsilon_total=0.3)
        self.assertFalse(preview["feasible"])
        self.assertIn("I2", preview["authorization_dependencies"]["missing_institutions"])
        self.assertTrue(preview["adjustments"])

    # -- 单位贡献裁剪（统计单位不是行数） -----------------------------------

    def test_contribution_limit_is_enforced_per_unit(self):
        # PLAN2：person 邻接、每单位最多 1 条。即使每人跨 3 家机构有 3 条，
        # 计数敏感度仍为 1，发布的去重单位计数远小于记录行数 600。
        out = self._publish_plan2_count("L1", epsilon=0.5)
        cell = out["release"]["cells"][0]
        self.assertLess(cell["unit_count_noisy"], 200)  # 150 人 + 小噪声
        self.assertEqual(out["release"]["contribution_limit_applied"], 1)
        self.assertEqual(out["release"]["adjacency_unit"], "person")

    def test_household_plan_uses_household_adjacency(self):
        # PLAN1 的 v1 授权因 I2 撤回不完整，先恢复 G2 再发布。
        self.svc.reinstate_grant(self.u3, "G2")
        self.svc.submit_request(
            self.u1, request_id="H1", plan_id="PLAN1", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.5, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, "H1", approve=True)
        out = self.svc.publish(self.u2, "H1")
        self.assertEqual(out["release"]["adjacency_unit"], "household")
        self.assertEqual(out["release"]["contribution_limit_applied"], 2)
        # 75 个家庭，每家庭截断到 2 条；单位计数约 75 而非 600 行。
        self.assertLess(out["release"]["cells"][0]["unit_count_noisy"], 120)

    # -- 共享额度竞争 -------------------------------------------------------

    def test_parallel_requests_compete_for_shared_budget(self):
        self._publish_plan2_count("B1", epsilon=0.6, groups=["region"])
        self.svc.submit_request(
            self.u5, request_id="B2", plan_id="PLAN2", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.6, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, "B2", approve=True)
        with self.assertRaises(BudgetError):
            self.svc.publish(self.u2, "B2")
        position = self.svc.ledger_position("PLAN2")
        self.assertAlmostEqual(position["epsilon_used"], 0.6)
        self.assertEqual(self.svc._request("B2")["state"], "budget_denied")

    def test_budget_denied_can_retry_with_smaller_epsilon(self):
        self._publish_plan2_count("D1", epsilon=0.8)
        # 不同分组 => 不同指纹，不会被复用吸收，才会真正竞争额度。
        self.svc.submit_request(
            self.u5, request_id="D2", plan_id="PLAN2", metric="count",
            group_by=["region"], filters={}, version="2026-04-v1",
            epsilon_total=0.5, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, "D2", approve=True)
        with self.assertRaises(BudgetError):
            self.svc.publish(self.u2, "D2")
        # 额度未扣；研究员按提示改小 epsilon 重新申请即可成功。
        self._publish_plan2_count("D3", epsilon=0.2, groups=["region"])
        self.assertAlmostEqual(
            self.svc.ledger_position("PLAN2")["epsilon_used"], 1.0, places=9)

    def test_max_releases_cap_enforced(self):
        # PLAN2 最多 4 次披露；用不同分组/人群制造 5 个不同指纹。
        shapes = [
            ([], {}),
            (["region"], {}),
            ([], {"region": "urban"}),
            ([], {"region": "rural"}),
            (["region"], {"access_barrier": True}),
        ]
        for i, (groups, filters) in enumerate(shapes):
            rid = f"M{i}"
            self.svc.submit_request(
                self.u5, request_id=rid, plan_id="PLAN2", metric="count",
                group_by=groups, filters=filters, version="2026-04-v1",
                epsilon_total=0.1, delta_total=0.0, as_of="2026-05-15")
            self.svc.review(self.u2, rid, approve=True)
            if i < 4:
                self.svc.publish(self.u2, rid)
            else:
                with self.assertRaises(BudgetError):
                    self.svc.publish(self.u2, rid)
        self.assertEqual(self.svc.ledger_position("PLAN2")["release_count"], 4)

    def test_concurrent_publish_only_one_wins(self):
        """两个进程/线程并行发布、各自余额看似够用（0.6+0.6>1.0）：
        SQLite 写事务串行化，恰好一个成功，另一个 budget_denied 且零扣账。"""
        shapes = {"T1": ["region"], "T2": []}
        for rid, groups in shapes.items():
            self.svc.submit_request(
                self.u5, request_id=rid, plan_id="PLAN2", metric="count",
                group_by=groups, filters={}, version="2026-04-v1",
                epsilon_total=0.6, delta_total=0.0, as_of="2026-05-15")
            self.svc.review(self.u2, rid, approve=True)

        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def worker(rid: str) -> None:
            local = FedReadService(connect(self.db))
            reviewer = local.operator("U2")
            barrier.wait()
            try:
                local.publish(reviewer, rid)
                results[rid] = "published"
            except BudgetError:
                results[rid] = "denied"
            finally:
                local.conn.close()

        threads = [threading.Thread(target=worker, args=(rid,))
                   for rid in shapes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(sorted(results.values()), ["denied", "published"])
        position = self.svc.ledger_position("PLAN2")
        self.assertAlmostEqual(position["epsilon_used"], 0.6, places=9)
        states = {r["id"]: r["state"]
                  for r in self.svc.list_requests(self.u4, "PLAN2")
                  if r["id"] in shapes}
        self.assertEqual(sorted(states.values()),
                         ["budget_denied", "published"])

    # -- 结果复用与确定性噪声 -----------------------------------------------

    def test_identical_request_reuses_with_zero_spend(self):
        first = self._publish_plan2_count("U1", epsilon=0.3,
                                          groups=["region"], filters={"region": "urban"})
        spent = self.svc.ledger_position("PLAN2")["epsilon_used"]
        second = self._publish_plan2_count("U2", epsilon=0.3,
                                           groups=["region"], filters={"region": "urban"})
        self.assertTrue(second["reused"])
        self.assertEqual(second["release"]["cells"], first["release"]["cells"])
        self.assertEqual(self.svc.ledger_position("PLAN2")["epsilon_used"], spent)

    def test_changed_fingerprint_consumes_budget_again(self):
        self._publish_plan2_count("F1", epsilon=0.2, filters={"region": "urban"})
        spent = self.svc.ledger_position("PLAN2")["epsilon_used"]
        self._publish_plan2_count("F2", epsilon=0.1, filters={"region": "rural"})
        self.assertAlmostEqual(
            self.svc.ledger_position("PLAN2")["epsilon_used"] - spent, 0.1, places=9)

    def test_retry_does_not_redraw_noise(self):
        out = self._publish_plan2_count("N1", epsilon=0.3)
        retry = self.svc.publish(self.u2, "N1")
        self.assertTrue(retry["reused"])
        self.assertEqual(retry["release"]["cells"], out["release"]["cells"])

    # -- 授权撤回 -----------------------------------------------------------

    def test_withdrawal_stops_pending_and_approved_requests(self):
        self.svc.submit_request(
            self.u5, request_id="W1", plan_id="PLAN2", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")  # 待审
        self.svc.submit_request(
            self.u5, request_id="W2", plan_id="PLAN2", metric="count",
            group_by=["region"], filters={}, version="2026-04-v1",
            epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, "W2", approve=True)  # 已批未发
        self.svc.withdraw_grant(self.u6, "G5", reason="测试撤回")
        self.assertEqual(self.svc._request("W1")["state"], "stopped")
        self.assertEqual(self.svc._request("W2")["state"], "stopped")
        with self.assertRaises((StateError, AuthorizationError)):
            self.svc.publish(self.u2, "W2")

    def test_published_result_survives_withdrawal_and_budget_not_refunded(self):
        self._publish_plan2_count("K1", epsilon=0.3)
        spent = self.svc.ledger_position("PLAN2")["epsilon_used"]
        self.svc.withdraw_grant(self.u6, "G5")
        result = self.svc.get_release(self.u5, "K1")
        self.assertEqual(result["state"], "published")
        self.assertEqual(self.svc.ledger_position("PLAN2")["epsilon_used"], spent)

    def test_withdrawal_does_not_touch_other_plans(self):
        self.svc.withdraw_grant(self.u6, "G5")  # PLAN2 的授权
        plan1 = self.svc.list_requests(self.u4, "PLAN1")
        self.assertTrue(plan1)
        self.assertNotIn("stopped", {r["state"] for r in plan1})

    def test_reinstate_does_not_resurrect_stopped_requests(self):
        self.svc.submit_request(
            self.u5, request_id="Z1", plan_id="PLAN2", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, "Z1", approve=True)
        self.svc.withdraw_grant(self.u6, "G5")
        self.svc.reinstate_grant(self.u6, "G5")
        self.assertEqual(self.svc._request("Z1")["state"], "stopped")

    # -- 中断恢复 -----------------------------------------------------------

    def _make_approved(self, rid):
        self.svc.submit_request(
            self.u5, request_id=rid, plan_id="PLAN2", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.3, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, rid, approve=True)

    def test_crash_before_commit_rolls_back_and_can_retry(self):
        self._make_approved("CB1")
        self.svc.arm_crash(self.u4, point="before_commit")
        with self.assertRaises(RuntimeError):
            self.svc.publish(self.u2, "CB1")
        # 提交前崩溃：事务整体回滚，无发布、无扣账，请求仍为 approved。
        self.assertEqual(self.svc._request("CB1")["state"], "approved")
        self.assertEqual(self.svc.ledger_position("PLAN2")["epsilon_used"], 0)
        self.assertEqual(
            self.svc.conn.execute("SELECT COUNT(*) AS n FROM releases").fetchone()["n"], 0)
        # 重连（模拟新进程）后直接重试即可成功，恢复例程报告无待修复项。
        svc2 = FedReadService(connect(self.db))
        self.assertEqual(svc2.recover(self.u4)["repaired_requests"], [])
        out = svc2.publish(self.u2, "CB1")
        self.assertTrue(out["published"])
        self.assertAlmostEqual(
            svc2.ledger_position("PLAN2")["epsilon_used"], 0.3, places=9)

    def test_crash_after_commit_recovers_without_double_charge(self):
        self._make_approved("CR1")
        self.svc.arm_crash(self.u4, point="after_publish")
        with self.assertRaises(RuntimeError):
            self.svc.publish(self.u2, "CR1")
        # 提交后崩溃：release/ledger 已持久，仅请求状态待收敛。
        self.assertEqual(self.svc.ledger_position("PLAN2")["epsilon_used"], 0.3)
        svc2 = FedReadService(connect(self.db))
        repaired = svc2.recover(self.u4)
        self.assertEqual(repaired["repaired_requests"], ["CR1"])
        result = svc2.get_release(self.u4, "CR1")
        retry = svc2.publish(self.u2, "CR1")
        self.assertEqual(retry["release"]["cells"], result["payload"]["cells"])
        self.assertAlmostEqual(
            svc2.ledger_position("PLAN2")["epsilon_used"], 0.3, places=9)
        # 再次恢复应幂等，无重复修复。
        self.assertEqual(svc2.recover(self.u4)["repaired_requests"], [])

    # -- 管理员审计 ---------------------------------------------------------

    def test_admin_disclosure_report_maps_grants_and_mechanism(self):
        self._publish_plan2_count("A1", epsilon=0.25)
        report = self.svc.disclosure_report(self.u4)
        self.assertEqual(len(report), 1)
        entry = report[0]
        self.assertEqual(entry["source_grant_ids"], ["G4", "G5", "G6"])
        self.assertEqual(entry["adjacency_unit"], "person")
        self.assertEqual(entry["version"], "2026-04-v1")
        self.assertIn("count", entry["mechanism"])

    def test_audit_trail_is_append_only_and_complete(self):
        self._publish_plan2_count("AU1", epsilon=0.2)
        trail = self.svc.audit_trail(self.u4)
        actions = [e["action"] for e in trail]
        self.assertIn("request_submitted", actions)
        self.assertIn("request_published", actions)
        # 审计表无更新/删除接口：只暴露追加方法。
        self.assertFalse(hasattr(self.svc, "delete_audit"))

    def test_error_messages_and_releases_never_leak_record_detail(self):
        # 先取得一次发布载荷，再制造撤回停止，收集研究员可见错误文本。
        out = self._publish_plan2_count("LE2", epsilon=0.2)
        self.svc.submit_request(
            self.u5, request_id="LE1", plan_id="PLAN2", metric="count",
            group_by=[], filters={}, version="2026-04-v1",
            epsilon_total=0.2, delta_total=0.0, as_of="2026-05-15")
        self.svc.review(self.u2, "LE1", approve=True)
        self.svc.withdraw_grant(self.u6, "G5")
        try:
            self.svc.publish(self.u2, "LE1")
            self.fail("应当停止")
        except (AuthorizationError, StateError) as exc:
            message = str(exc)
        # 错误文本不得含人员/家庭/观测编号或原始字段值。
        for token in ("P0", "H0", "R1", "reading_minutes", "records_raw"):
            self.assertNotIn(token, message)

        # 发布载荷只含加噪单元结果与机制解释，不含任何单位编号。
        payload_text = repr(out["release"])
        for token in ("P001", "H000", "R1", "institution_id"):
            self.assertNotIn(token, payload_text)
        # 原始记录仍只存在于受控表，服务层不暴露导出。
        self.assertFalse(hasattr(self.svc, "export_records"))


if __name__ == "__main__":
    unittest.main()
