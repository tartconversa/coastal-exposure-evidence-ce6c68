"""Turnbull 区间删失估计与整群自助的统计正确性测试。

用手工可验证的小样本构造，不依赖合成业务资料，保证估计量本身正确。
"""
import unittest

from sealpack.survival import (
    Observation, fit_survival, risk_with_ci, turnbull,
)


def obs(subject, left, right, cluster=None, outcome="corrosion"):
    return Observation(
        subject_id=subject, left=float(left),
        right=None if right is None else float(right),
        cluster_id=cluster or subject, outcome=outcome,
    )


class TurnbullTests(unittest.TestCase):
    def test_interval_events_plus_right_censoring(self):
        # 2 个失效均落在 (150,180]，14 个在 320 右删失：
        # 180 处质量应为 2/16，末端桶 14/16，320 有限桶质量为 0
        observations = [obs(f"E{i}", 150, 180) for i in range(2)]
        observations += [
            obs(f"C{i}", 320, None, outcome="planned") for i in range(14)
        ]
        result = fit_survival(observations)
        mass_at_180 = dict(zip(result.grid, result.mass))[180.0]
        self.assertAlmostEqual(mass_at_180, 2 / 16, places=6)
        self.assertAlmostEqual(result.tail_mass, 14 / 16, places=6)
        self.assertAlmostEqual(sum(result.mass) + result.tail_mass, 1.0, places=9)
        # 240 天在 180 之后、320 之前，风险等于 180 处累计质量
        self.assertAlmostEqual(result.risk(240), 2 / 16, places=6)
        self.assertAlmostEqual(result.risk(180), 2 / 16, places=6)

    def test_censor_time_does_not_absorb_failure_mass(self):
        # 回归：右删失时点不得承接失效质量，否则 F 被错误抬到 1
        observations = [obs("E1", 10, 20), obs("E2", 10, 20)]
        observations += [obs(f"C{i}", 100, None, outcome="machine")
                         for i in range(8)]
        result = fit_survival(observations)
        self.assertLess(result.risk(50), 0.3)
        self.assertGreater(result.tail_mass, 0.7)

    def test_horizon_beyond_followup_unidentifiable(self):
        observations = [obs("C1", 100, None, outcome="planned")]
        result = fit_survival(observations)
        self.assertIsNone(result.risk(200))
        self.assertIsNone(result.survival(200))

    def test_zero_event_cell(self):
        observations = [obs(f"C{i}", 240, None, outcome="planned")
                         for i in range(10)]
        result = fit_survival(observations)
        self.assertEqual(result.n_events, 0)
        self.assertEqual(result.risk(240), 0.0)
        self.assertAlmostEqual(result.tail_mass, 1.0, places=9)
        ci = risk_with_ci(result, 240, replicates=100, seed=1)
        # 零失效不等于零风险：上分位走保守上界
        self.assertGreater(ci["upper"], 0.0)
        self.assertEqual(ci["upper_source"], "zero_event_conservative")

    def test_distinct_censor_reasons_kept_separate(self):
        observations = [
            obs("S", 100, None, outcome="safety"),
            obs("M", 100, None, outcome="machine"),
            obs("P", 100, None, outcome="planned"),
        ]
        result = fit_survival(observations)
        self.assertEqual(
            result.censor_counts, {"safety": 1, "machine": 1, "planned": 1}
        )

    def test_cluster_bootstrap_respects_groups(self):
        # 同一整群 4 个个体，自助以群为单位；2 个群、各 4 个体
        observations = []
        for cluster in ("B1", "B2"):
            observations += [
                obs(f"{cluster}-{i}", 50, 100, cluster=cluster) for i in range(4)
            ]
        result = fit_survival(observations)
        self.assertEqual(result.n_clusters, 2)
        ci = risk_with_ci(result, 100, replicates=200, seed=42)
        self.assertEqual(ci["status"], "estimated")
        self.assertGreaterEqual(ci["upper"], ci["point"])

    def test_reproducible_with_same_seed(self):
        observations = [obs(f"E{i}", 10, 20) for i in range(4)]
        observations += [obs(f"C{i}", 90, None, outcome="planned")
                         for i in range(6)]
        result = fit_survival(observations)
        first = risk_with_ci(result, 50, replicates=120, seed=99)
        second = risk_with_ci(result, 50, replicates=120, seed=99)
        self.assertEqual(first["upper"], second["upper"])
        self.assertEqual(first["point"], second["point"])

    def test_empty_observations(self):
        self.assertIsNone(fit_survival([]))
        grid, mass, tail = turnbull([])
        self.assertEqual(grid, [])
        self.assertEqual(mass, [])
        self.assertEqual(tail, 1.0)


if __name__ == "__main__":
    unittest.main()
