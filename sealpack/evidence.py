"""证据单元构建、承诺分级与同条件设计比较。

两条证据流严格分开，各自有独立时间尺度，应用不提供换算：
- 试验室流（lab）：时间单位暴露小时，单元键 (设计, 协议)；
- 现场流（field）：时间单位服役日，单元键 (设计, 环境)。

客户承诺以“给定时长内首次腐蚀风险”表达，只能由现场流承载；
试验室流用于刻画设计在已知剖面下的表现、识别证据缺口与规划下一轮试验。

分级语义（逐单元、给定时长与风险上限）：
- supported   可识别且自助上分位数不超过上限，样本量达到最低要求；
- limited     有可识别证据但不确定度越界，或样本量不足——支持范围有限；
- adverse     点估计本身已超过上限——观测到较差表现，与 limited 明确区分；
- unknown     该工况没有可识别证据（无样本或超出最大随访），保留未知。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .survival import Observation, fit_survival, risk_with_ci

LAB_STREAM = "lab"
FIELD_STREAM = "field"

SUPPORTED = "supported"
LIMITED = "limited"
ADVERSE = "adverse"
UNKNOWN = "unknown"

STATUS_LABELS = {
    SUPPORTED: "支持",
    LIMITED: "有限支持",
    ADVERSE: "观测到较差表现",
    UNKNOWN: "尚不支持（证据未知）",
}

MIN_SUBJECTS = 6


# ---------------------------------------------------------------- 观测构建

def build_lab_observations(store):
    """把每个试样转成一个区间观测；三种停试原因保留为不同删失结局。"""
    inspections = {}
    for ins in store.inspections:
        inspections.setdefault(ins.specimen_id, []).append(ins)
    profile = {}
    for exp in store.exposures:
        profile.setdefault(exp.specimen_id, set()).add(
            (exp.salt_g_l, exp.humidity_pct)
        )

    observations = []
    for sp in store.specimens:
        records = sorted(inspections.get(sp.specimen_id, []), key=lambda x: x.hour)
        first_corr = next((r for r in records if r.finding == "corrosion"), None)
        trace = {
            "design_id": sp.design_id,
            "protocol": sp.protocol,
            "batch_id": sp.batch_id,
            "stop_reason": sp.stop_reason,
            "physical_state": sp.physical_state,
            "severity": sorted(profile.get(sp.specimen_id, [])),
        }
        if first_corr is not None:
            prior_clear = [r.hour for r in records
                           if r.finding == "clear" and r.hour < first_corr.hour]
            left = max(prior_clear, default=0)
            observations.append(Observation(
                subject_id=sp.specimen_id, left=float(left),
                right=float(first_corr.hour), cluster_id=sp.batch_id,
                outcome="corrosion",
                trace={**trace, "inspection_ids": [r.inspection_id for r in records
                       if left <= r.hour <= first_corr.hour]},
            ))
        else:
            # safety/machine/planned 各自成类，调用方不得合并成同一种阴性
            observations.append(Observation(
                subject_id=sp.specimen_id, left=float(sp.end_hour), right=None,
                cluster_id=sp.batch_id, outcome=sp.stop_reason, trace=trace,
            ))
    return observations


def build_field_observations(store):
    """现场经历转区间观测。

    每个 spell 用自身安装日起算的局部时钟：换密封后的新设计经历不能沿用上一设计
    的寿命原点。腐蚀为 (末次清晰, 末次可见] 区间；seal_change/censored 为右删失，
    但剔除理由分别保留。
    """
    observations = []
    for fs in store.field_spells:
        entry_origin_day = fs.last_seen_day - (fs.end - fs.start).days
        local_clear = fs.last_clear_day - entry_origin_day
        local_seen = fs.last_seen_day - entry_origin_day
        trace = {
            "design_id": fs.design_id,
            "environment": fs.environment,
            "unit_id": fs.unit_id,
            "spell_id": fs.spell_id,
            "origin_days": (fs.last_clear_day, fs.last_seen_day),
            "entry_origin_day": entry_origin_day,
            "delayed_entry": entry_origin_day > 0,
        }
        if fs.end_reason == "corrosion":
            observations.append(Observation(
                subject_id=fs.spell_id, left=float(local_clear),
                right=float(local_seen), cluster_id=fs.unit_id,
                outcome="corrosion", trace=trace,
            ))
        else:
            observations.append(Observation(
                subject_id=fs.spell_id, left=float(local_seen), right=None,
                cluster_id=fs.unit_id, outcome=fs.end_reason, trace=trace,
            ))
    return observations


# ---------------------------------------------------------------- 证据单元

@dataclass
class EvidenceCell:
    stream: str
    design_id: str
    key: tuple
    result: object
    observations: list = field(default_factory=list)

    @property
    def n_subjects(self):
        return self.result.n_subjects if self.result else 0

    @property
    def n_events(self):
        return self.result.n_events if self.result else 0

    @property
    def max_followup(self):
        return self.result.max_followup if self.result else 0.0


def build_cells(store):
    """组装全部证据单元：试验室 (设计,协议) 与现场 (设计,环境)。"""
    cells = {}
    for obs in build_lab_observations(store):
        key = (obs.trace["design_id"], obs.trace["protocol"])
        cells.setdefault((LAB_STREAM, key), []).append(obs)
    for obs in build_field_observations(store):
        key = (obs.trace["design_id"], obs.trace["environment"])
        cells.setdefault((FIELD_STREAM, key), []).append(obs)
    return {
        ck: EvidenceCell(
            stream=ck[0], design_id=ck[1][0], key=ck[1],
            result=fit_survival(obs_list), observations=obs_list,
        )
        for ck, obs_list in cells.items()
    }


def severity_map(store):
    """每个设计在试验室实际承受的盐雾/湿度剖面（描述暴露，不给等效年限）。"""
    out = {}
    for exp in store.exposures:
        sp = next(s for s in store.specimens if s.specimen_id == exp.specimen_id)
        out.setdefault(sp.design_id, set()).add((exp.salt_g_l, exp.humidity_pct))
    return {d: sorted(v) for d, v in out.items()}


def classify_cell(cell, horizon, risk_limit, min_subjects=MIN_SUBJECTS,
                  replicates=500, seed=7123):
    """单元级承诺分级，返回含点估计、上分位数、分级与溯源句柄的结构。"""
    if cell is None or cell.result is None:
        return {"status": UNKNOWN, "risk": None, "reason": "no_evidence",
                "n_subjects": 0, "n_events": 0, "min_subjects": min_subjects,
                "traces": []}
    estimate = risk_with_ci(cell.result, float(horizon),
                            replicates=replicates, seed=seed)
    base = {
        "n_subjects": cell.n_subjects,
        "n_events": cell.n_events,
        "min_subjects": min_subjects,
        "max_followup": cell.max_followup,
        "censor_counts": dict(cell.result.censor_counts),
        "risk": estimate,
        "event_traces": cell.result.event_intervals(),
        "censor_traces": cell.result.censor_intervals(),
    }
    if estimate["status"] == "unidentifiable":
        return {**base, "status": UNKNOWN,
                "reason": estimate["reason"], "risk": estimate}
    if estimate["point"] > risk_limit + 1e-12:
        return {**base, "status": ADVERSE, "reason": "point_above_limit"}
    if cell.n_subjects < min_subjects:
        return {**base, "status": LIMITED, "reason": "sample_below_minimum"}
    if estimate["upper"] > risk_limit + 1e-12:
        return {**base, "status": LIMITED, "reason": "uncertainty_above_limit"}
    return {**base, "status": SUPPORTED, "reason": "upper_within_limit"}


# ---------------------------------------------------------------- 同条件比较

def compare_field(store, cells, environment, horizon, risk_limit,
                  replicates=500, seed=7123):
    """同一现场环境下有证据的设计两两比较；无重叠的环境明确标注不可比。"""
    rows = []
    for design in sorted({d.design_id for d in store.designs}):
        cell = cells.get((FIELD_STREAM, (design, environment)))
        if cell is None:
            rows.append({"design_id": design, "comparable": False,
                         "status": UNKNOWN, "reason": "no_evidence_in_environment"})
            continue
        verdict = classify_cell(cell, horizon, risk_limit,
                                replicates=replicates, seed=seed)
        if verdict["status"] == UNKNOWN:
            rows.append({"design_id": design, "comparable": False,
                         "status": UNKNOWN, "reason": verdict.get("reason"),
                         "n_subjects": verdict.get("n_subjects", 0),
                         "n_events": verdict.get("n_events", 0)})
            continue
        rows.append({
            "design_id": design,
            "comparable": True,
            "status": verdict["status"],
            "point": (verdict.get("risk") or {}).get("point"),
            "upper": (verdict.get("risk") or {}).get("upper"),
            "n_subjects": verdict.get("n_subjects", 0),
            "n_events": verdict.get("n_events", 0),
        })
    comparable = [r for r in rows if r["comparable"]]
    ranked = sorted(
        comparable,
        key=lambda r: (r["point"] is None, r["point"] if r["point"] is not None else 0.0,
                       r["upper"] if r["upper"] is not None else 0.0),
    )
    return {
        "environment": environment,
        "horizon_days": horizon,
        "risk_limit": risk_limit,
        "rows": rows,
        "ranked": [r["design_id"] for r in ranked],
        "comparable_pairs": len(comparable) * (len(comparable) - 1) // 2,
        "note": "仅同环境现场证据可比较；无该环境证据的设计保留未知，不参与排名",
    }


def lab_diagnostic(store, cells, common_hour, replicates=500, seed=7123):
    """试验室跨设计诊断排名。

    设计与盐雾剖面在本资料中完全混杂（D1/D2/D3 各自只承受一种烈度），
    因此不存在同剖面跨设计对比，任何跨设计试验室排名都不可识别为设计优劣。
    这里同时给出朴素通过率与共同小时风险两套口径，并检查朴素口径是否会
    因高暴露/早停试样而把设计错误排前，作为防偏诊断而非排名结论。
    """
    profiles = severity_map(store)
    rows = []
    for design in sorted({d.design_id for d in store.designs}):
        protocol_rows = []
        for protocol in sorted({s.protocol for s in store.specimens
                                if s.design_id == design}):
            cell = cells[(LAB_STREAM, (design, protocol))]
            crude_pass = (
                cell.n_subjects - cell.n_events
            ) / cell.n_subjects
            estimate = risk_with_ci(cell.result, float(common_hour),
                                    replicates=replicates, seed=seed)
            protocol_rows.append({
                "protocol": protocol,
                "n_subjects": cell.n_subjects,
                "n_events": cell.n_events,
                "crude_pass_fraction": crude_pass,
                "stop_breakdown": dict(cell.result.censor_counts),
                "risk_at_common_hour": estimate,
            })
        rows.append({"design_id": design, "severity_profile": profiles[design],
                     "protocols": protocol_rows})

    naive_order = sorted(
        rows,
        key=lambda r: -sum(p["crude_pass_fraction"] for p in r["protocols"])
        / len(r["protocols"]),
    )
    proper = []
    for r in rows:
        points = [p["risk_at_common_hour"]["point"]
                  for p in r["protocols"]
                  if p["risk_at_common_hour"]["status"] == "estimated"]
        proper.append((r["design_id"], sum(points) / len(points) if points else None))
    proper_order = [d for d, _ in sorted(
        (x for x in proper if x[1] is not None), key=lambda x: x[1])]
    bias_flag = [d for d in naive_order[0:1] if d not in proper_order[0:1]]
    return {
        "common_hour": common_hour,
        "rows": rows,
        "naive_ranking": [r["design_id"] for r in naive_order],
        "interval_aware_ranking": proper_order,
        "severity_confounded": True,
        "bias_warning": (
            "朴素通过率排名与区间删失排名不一致：高暴露早停试样导致口径偏差，"
            "禁止使用朴素排名承诺可靠性"
            if bias_flag else
            "本资料中设计与暴露烈度完全混杂，两套口径都不构成同条件设计优劣证据"
        ),
    }
