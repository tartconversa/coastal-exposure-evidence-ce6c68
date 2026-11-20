"""基于合成业务资料的端到端业务检查。

覆盖需求中的关键判断：停试三因不合并、密封换型分段、区间腐蚀定位、
承诺四分级、未覆盖环境不被平均掩盖、朴素通过率防偏、排舱硬约束、
资源竞争、情景不预支证据、台账只追加与显式换版。
"""
import json
import tempfile
import unittest
from pathlib import Path

from sealpack.commitments import (
    evaluate_conditions, evaluate_request, list_commitments,
    reassess_commitments, save_commitment, scenario_narrow,
    scenario_reduce_duration, supersede_commitment,
)
from sealpack.evidence import (
    ADVERSE, FIELD_STREAM, LAB_STREAM, LIMITED, SUPPORTED, UNKNOWN,
    build_cells, build_field_observations, build_lab_observations,
    compare_field, lab_diagnostic,
)
from sealpack.planning import (
    coverage_gaps, plan_scenario, recheck_after_occupancy, validate_multi,
    validate_plan,
)
from sealpack.records import load_store

ROOT = Path(__file__).resolve().parents[1]


class BusinessTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = load_store(ROOT)
        cls.cells = build_cells(cls.store)


class RecordsTests(BusinessTestBase):
    def test_seal_change_splits_spell_and_resets_clock(self):
        # U9 有两段：D1 以 seal_change 终止，D3 另起经历
        units = {}
        for fs in self.store.field_spells:
            units.setdefault(fs.unit_id, []).append(fs)
        u9 = sorted(units["U9"], key=lambda x: x.start)
        self.assertEqual(len(u9), 2)
        self.assertEqual(u9[0].end_reason, "seal_change")
        self.assertEqual(u9[0].design_id, "D1")
        self.assertEqual(u9[1].design_id, "D3")
        self.assertEqual(u9[1].start, u9[0].end)

    def test_field_failure_is_interval_not_point(self):
        for o in build_field_observations(self.store):
            if o.right is not None:
                self.assertLess(o.left, o.right)
                self.assertEqual(o.outcome, "corrosion")

    def test_lab_censor_reasons_never_merged(self):
        for o in build_lab_observations(self.store):
            if o.right is None:
                self.assertIn(o.outcome, ("safety", "machine", "planned"))
        result = self.cells[(LAB_STREAM, ("D1", "P1"))].result
        self.assertEqual(set(result.censor_counts), {"machine", "planned"})

    def test_destructed_specimens_marked(self):
        destructed = {s.specimen_id for s in self.store.specimens
                      if s.physical_state == "destructed"}
        self.assertEqual(len(destructed), 13)

    def test_severity_confounded_with_design(self):
        # D1/D2/D3 各自只承受一种盐雾剖面，跨设计试验室排名不可识别为优劣
        diag = lab_diagnostic(self.store, self.cells, 240, replicates=40)
        self.assertTrue(diag["severity_confounded"])
        # 朴素通过率把零失效但早停的 D1 排第一，与区间删失口径不一致时必须告警
        self.assertEqual(diag["naive_ranking"][0], "D1")
        self.assertIn("禁止", diag["bias_warning"])


class CommitmentTests(BusinessTestBase):
    def test_four_way_classification_on_real_cells(self):
        # D3 humid：2/16 区间失效，点风险 12.5% > 5% -> adverse（不是 limited）
        cell = self.cells[(FIELD_STREAM, ("D3", "humid"))]
        from sealpack.evidence import classify_cell
        verdict = classify_cell(cell, 240, 0.05, replicates=100)
        self.assertEqual(verdict["status"], ADVERSE)
        self.assertAlmostEqual(verdict["risk"]["point"], 0.125, places=6)

    def test_limited_distinct_from_adverse(self):
        # 放宽风险上限到 30%：点估计在内但自助上分 25%... 构造有限支持场景：
        # 用 D1 coastal（点 6.25%，上分 12.5%），上限取 10% -> limited
        cell = self.cells[(FIELD_STREAM, ("D1", "coastal"))]
        from sealpack.evidence import classify_cell
        verdict = classify_cell(cell, 180, 0.10, replicates=200)
        self.assertEqual(verdict["status"], LIMITED)
        self.assertEqual(verdict["reason"], "uncertainty_above_limit")

    def test_unknown_when_horizon_beyond_followup(self):
        evaluation = evaluate_request(self.store, self.cells, "R3",
                                      replicates=100)
        d1 = next(d for d in evaluation["designs"] if d["design_id"] == "D1")
        # 360 天 > 最大随访 320 天，coastal 必须 unknown，整体 unknown
        coastal = next(e for e in d1["per_environment"]
                       if e["environment"] == "coastal")
        self.assertEqual(coastal["status"], UNKNOWN)
        self.assertEqual(d1["overall_status"], UNKNOWN)
        self.assertIsNone(d1["weighted_point_risk"])

    def test_uncovered_environment_not_masked_by_average(self):
        evaluation = evaluate_request(self.store, self.cells, "R1",
                                      replicates=100)
        for d in evaluation["designs"]:
            self.assertTrue(d["uncovered_environments"])
            self.assertEqual(d["overall_status"], UNKNOWN)
            self.assertEqual(d["servable_cabinets"], 0)

    def test_supported_when_upper_within_limit(self):
        conditions = [{
            "environment": "coastal", "share": 1.0,
            "duration_days": 180, "risk_limit": 0.20,
        }]
        evaluation = evaluate_conditions(
            self.store, self.cells, conditions, 20, replicates=200
        )
        d1 = next(d for d in evaluation["designs"] if d["design_id"] == "D1")
        self.assertEqual(d1["overall_status"], SUPPORTED)
        self.assertEqual(d1["servable_cabinets"], 20)

    def test_narrow_scenario_drops_only_named_environments(self):
        base = evaluate_request(self.store, self.cells, "R1", replicates=100)
        narrow = scenario_narrow(base, ["humid", "inland"])
        d1 = next(d for d in narrow["designs"] if d["design_id"] == "D1")
        # 仅留 coastal：D1 是 adverse（6.25%>5%），不能再被剔除环境带成 unknown
        self.assertEqual(d1["overall_status"], ADVERSE)
        self.assertAlmostEqual(narrow["kept_share_of_original"], 0.6, places=9)

    def test_reduce_duration_recomputes_cells(self):
        base = evaluate_request(self.store, self.cells, "R3", replicates=100)
        reduced = scenario_reduce_duration(
            self.store, self.cells, base, 180
        )
        self.assertEqual(reduced["conditions"][0]["duration_days"], 180)
        d1 = next(d for d in reduced["designs"] if d["design_id"] == "D1")
        # 180 天进入可识别窗口：coastal 从 unknown 变为 adverse（6.25%>5%）
        coastal = next(e for e in d1["per_environment"]
                       if e["environment"] == "coastal")
        self.assertEqual(coastal["status"], ADVERSE)
        # 但 humid/inland 仍无证据，整体不得翻成支持
        self.assertEqual(d1["overall_status"], UNKNOWN)
        self.assertIsNone(d1["weighted_point_risk"])

    def test_ad_hoc_conditions_must_have_unit_share(self):
        bad = [{
            "environment": "coastal", "share": 0.5,
            "duration_days": 100, "risk_limit": 0.05,
        }]
        with self.assertRaises(ValueError):
            evaluate_conditions(self.store, self.cells, bad, 1, replicates=40)

    def test_no_acceleration_conversion(self):
        # 试验室小时与现场日分属不同单元，应用不提供跨流映射
        lab_keys = {k for st, k in self.cells if st == LAB_STREAM}
        field_keys = {k for st, k in self.cells if st == FIELD_STREAM}
        self.assertTrue(all(len(k) == 2 for k in lab_keys))
        self.assertFalse(lab_keys & field_keys)


class ComparisonTests(BusinessTestBase):
    def test_only_same_environment_comparable(self):
        cmp_ = compare_field(self.store, self.cells, "coastal", 180, 0.20,
                             replicates=100)
        statuses = {r["design_id"]: r for r in cmp_["rows"]}
        self.assertTrue(statuses["D1"]["comparable"])
        self.assertFalse(statuses["D2"]["comparable"])  # 无 coastal 证据
        self.assertEqual(cmp_["ranked"], ["D1"])


class PlanningTests(BusinessTestBase):
    def _plan(self, placements, plan_id="T"):
        return {"plan_id": plan_id, "placements": placements}

    def test_feasible_plan(self):
        plan = self._plan([
            {"slot_id": "C2", "action_id": "A1", "specimen_id": "X002"},
            {"slot_id": "C2", "action_id": "A1", "specimen_id": "X008"},
            {"slot_id": "C1", "action_id": "A2", "specimen_id": "X003"},
        ])
        report = validate_plan(self.store, plan)
        self.assertTrue(report["feasible"], report["errors"])
        self.assertLessEqual(report["total_cost"], report["budget"])

    def test_destructed_specimen_rejected(self):
        plan = self._plan([
            {"slot_id": "C2", "action_id": "A3", "specimen_id": "X060"},
        ])
        report = validate_plan(self.store, plan)
        self.assertFalse(report["feasible"])
        self.assertTrue(any("破坏" in e for e in report["errors"]))

    def test_protocol_mixing_rejected(self):
        # X004 是 P1 试样，不能挂 P2 动作/舱段
        plan = self._plan([
            {"slot_id": "C1", "action_id": "A2", "specimen_id": "X004"},
        ])
        report = validate_plan(self.store, plan)
        self.assertFalse(report["feasible"])
        self.assertTrue(any("协议" in e for e in report["errors"]))

    def test_capacity_overflow_rejected(self):
        # C2 容量 8、reserved 0，排 9 个 P1 可用试样
        p1_available = [
            s.specimen_id for s in self.store.specimens
            if s.protocol == "P1" and s.physical_state == "available"
        ][:9]
        plan = self._plan([
            {"slot_id": "C2", "action_id": "A1", "specimen_id": sid}
            for sid in p1_available
        ])
        report = validate_plan(self.store, plan)
        self.assertFalse(report["feasible"])
        self.assertTrue(any("容量" in e for e in report["errors"]))

    def test_multi_plan_double_booking_rejected(self):
        shared = [{"slot_id": "C2", "action_id": "A1",
                   "specimen_id": "X002"}]
        plan_a = self._plan(shared, "A")
        plan_b = self._plan(
            shared + [{"slot_id": "C4", "action_id": "A1",
                       "specimen_id": "X014"}], "B")
        reports = validate_multi(self.store, [plan_a, plan_b])
        # 第一份先占用者单独可行，第二份重复占用违规
        self.assertTrue(reports[0]["feasible"])
        self.assertFalse(reports[1]["feasible"])
        self.assertTrue(
            any("重复占用" in e for r in reports for e in r["errors"])
        )

    def test_destructive_action_consumes_specimen(self):
        plan = self._plan([
            {"slot_id": "C2", "action_id": "A3", "specimen_id": "X002"},
            {"slot_id": "C4", "action_id": "A1", "specimen_id": "X002"},
        ])
        report = validate_plan(self.store, plan)
        self.assertFalse(report["feasible"])
        self.assertTrue(any("破坏性" in e for e in report["errors"]))
        self.assertIn("X002", report["specimens_consumed"])

    def test_occupancy_recheck_blocks_and_keeps_commitments(self):
        plan = self._plan([
            {"slot_id": "C2", "action_id": "A1", "specimen_id": "X002"},
            {"slot_id": "C2", "action_id": "A1", "specimen_id": "X008"},
        ])
        evaluation = evaluate_request(self.store, self.cells, "R1",
                                      replicates=60)
        report = recheck_after_occupancy(
            self.store, self.cells, plan, evaluation,
            extra_reservations={"C2": 7}, blocked_specimens=["X008"],
        )
        self.assertFalse(report["feasible"])
        self.assertIn("open_field_gaps", report)
        self.assertIn("补证延后", report["occupancy_effect"])

    def test_scenario_does_not_pre_count_evidence(self):
        plan = self._plan([
            {"slot_id": "C2", "action_id": "A1", "specimen_id": "X002"},
        ])
        evaluation = evaluate_request(self.store, self.cells, "R1",
                                      replicates=60)
        scenario = plan_scenario(self.store, self.cells, plan, evaluation)
        affected = scenario["lab_cells_affected"][0]
        for case in affected["hypothetical_outcomes"]:
            self.assertFalse(case["evidence_now"])
        # 现场缺口不允许由试验室安排关闭
        self.assertTrue(all(
            g["closable_by_lab"] is False
            for g in coverage_gaps(self.store, self.cells, evaluation)
            ["field_gaps"]
        ))


class LedgerTests(BusinessTestBase):
    def _conditions(self, limit):
        return [{
            "environment": "coastal", "share": 1.0,
            "duration_days": 180, "risk_limit": limit,
        }]

    def test_append_only_and_traceability(self):
        with tempfile.TemporaryDirectory() as ledger:
            evaluation = evaluate_conditions(
                self.store, self.cells, self._conditions(0.20), 20,
                replicates=100,
            )
            cid = save_commitment(ledger, evaluation, self.store, "D1",
                                  note="测试")
            snapshot = next(s for s in list_commitments(ledger)
                            if s["commitment_id"] == cid)
            self.assertEqual(snapshot["status"], "issued")
            self.assertIn("F33-1", snapshot["evidence_basis"][0]["event_ids"])
            self.assertIn("seal_change",
                          snapshot["evidence_basis"][0]["censor_reasons"])
            self.assertTrue(snapshot["limits"]["no_acceleration_conversion"])
            self.assertEqual(len(snapshot["data_fingerprint"]["digests"]), 9)

    def test_unknown_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as ledger:
            evaluation = evaluate_request(self.store, self.cells, "R3",
                                          replicates=60)
            with self.assertRaises(ValueError):
                save_commitment(ledger, evaluation, self.store, "D1")

    def test_reassessment_preserves_original(self):
        with tempfile.TemporaryDirectory() as ledger:
            evaluation = evaluate_conditions(
                self.store, self.cells, self._conditions(0.05), 20,
                replicates=100,
            )
            cid = save_commitment(ledger, evaluation, self.store, "D1")
            impacts = reassess_commitments(ledger, self.store, self.cells)
            self.assertEqual(impacts[0]["commitment_id"], cid)
            # 同资料重评：指纹不变、结论不变
            self.assertFalse(impacts[0]["fingerprint_changed"])
            original = next(s for s in list_commitments(ledger)
                            if s["commitment_id"] == cid)
            self.assertEqual(original["status"], "issued")

    def test_supersession_keeps_chain(self):
        with tempfile.TemporaryDirectory() as ledger:
            strict = evaluate_conditions(
                self.store, self.cells, self._conditions(0.05), 20,
                replicates=100,
            )
            old_id = save_commitment(ledger, strict, self.store, "D1")
            relaxed = evaluate_conditions(
                self.store, self.cells, self._conditions(0.20), 20,
                replicates=100,
            )
            new_id = supersede_commitment(
                ledger, relaxed, self.store, old_id, "D1", note="换版"
            )
            records = {s["commitment_id"]: s for s in list_commitments(ledger)}
            self.assertEqual(records[old_id]["status"], "superseded")
            self.assertEqual(records[old_id]["superseded_by"], new_id)
            self.assertEqual(records[new_id]["supersedes"], old_id)
            self.assertEqual(records[new_id]["status"], "issued")


if __name__ == "__main__":
    unittest.main()
