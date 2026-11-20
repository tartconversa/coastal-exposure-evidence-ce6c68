"""资料装载、时点过滤与业务不变量校验。

只做读取与口径校验，不做任何估计或推荐。所有数值字段在此完成类型转换；
空字符串保留为 None，调用方不得把 None 当零使用。

关键口径（见 docs/资料口径.md）：
- available_at 表示事实最早可知时刻，历史时点的运行只能使用当时已知的事实；
- 日期区间左闭右开；
- 现场 seal_change 终止原设计经历，换型后的 spell 归属新设计，不能续算原设计寿命；
- 试验室 safety/machine/planned 是三种不同结局，装载阶段只标注、不合并。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

LAB_CENSOR_REASONS = ("safety", "machine", "planned")
FIELD_END_REASONS = ("seal_change", "censored", "corrosion")
STAGES = ("salt", "humid", "dry")


def _parse_date(value):
    return dt.date.fromisoformat(value) if value not in (None, "") else None


def _parse_dt(value):
    if value in (None, ""):
        return None
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("datetime 必须带时区：" + value)
    return stamp


def _number(value, integer=False):
    if value in (None, ""):
        return None
    return int(value) if integer else float(value)


def _bool(value):
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("布尔字段必须是 true/false")


def _read_csv(path, parsers):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(parsers):
            raise ValueError(f"字段不匹配：{path}")
        rows = []
        for raw in reader:
            row = {}
            for name, parser in parsers.items():
                cell = raw[name]
                row[name] = None if cell == "" else parser(cell)
            rows.append(row)
    return rows


@dataclass(frozen=True)
class Design:
    design_id: str
    batch_id: str
    seal: str


@dataclass(frozen=True)
class Specimen:
    specimen_id: str
    design_id: str
    batch_id: str
    protocol: str
    stop_reason: str
    end_hour: int
    physical_state: str


@dataclass(frozen=True)
class Exposure:
    exposure_id: str
    specimen_id: str
    start_hour: int
    end_hour: int
    salt_g_l: float
    humidity_pct: float
    stage: str


@dataclass(frozen=True)
class Inspection:
    inspection_id: str
    specimen_id: str
    hour: int
    finding: str
    available_at: dt.datetime


@dataclass(frozen=True)
class FieldSpell:
    spell_id: str
    unit_id: str
    design_id: str
    environment: str
    start: dt.date
    end: dt.date
    last_clear_day: int
    last_seen_day: int
    end_reason: str
    available_at: dt.datetime


@dataclass(frozen=True)
class RequestRow:
    request_id: str
    environment: str
    share: float
    duration_days: int
    risk_limit: float
    cabinets: int
    decision_by: dt.date


@dataclass(frozen=True)
class ChamberSlot:
    slot_id: str
    chamber_id: str
    protocol: str
    start: dt.date
    end: dt.date
    capacity: int
    cost_each: float
    reserved: int


@dataclass(frozen=True)
class TestAction:
    action_id: str
    protocol: str
    destructive: bool
    hours: int
    cost: float


@dataclass(frozen=True)
class Budget:
    plan_id: str
    budget: float
    decision_date: dt.date


@dataclass
class EvidenceStore:
    """一次分析运行使用的完整资料快照（已按时点过滤）。"""

    root: Path
    as_of: dt.datetime
    catalog_digests: dict
    designs: list[Design]
    specimens: list[Specimen]
    exposures: list[Exposure]
    inspections: list[Inspection]
    field_spells: list[FieldSpell]
    requests: list[RequestRow]
    chambers: list[ChamberSlot]
    actions: list[TestAction]
    budgets: list[Budget]
    excluded: dict = field(default_factory=dict)

    def by_id(self, rows, key):
        return {getattr(r, key): r for r in rows}

    def specimens_for(self, design_id):
        return [s for s in self.specimens if s.design_id == design_id]

    def spells_for(self, design_id):
        return [f for f in self.field_spells if f.design_id == design_id]

    def digest_fingerprint(self):
        """用于承诺/运行溯源的资料指纹。"""
        return {name: self.catalog_digests[name] for name in sorted(self.catalog_digests)}


def load_store(root, as_of=None):
    """读取资料目录并按 as_of 过滤，同时执行业务不变量校验。

    as_of 为 None 时取资料中最晚可知时点（等价于“当前全部已知”）。
    被时点过滤剔除的记录计入 excluded，保证可追溯。
    """
    root = Path(root).resolve()
    catalog = json.loads((root / "data" / "catalog.json").read_text(encoding="utf-8"))
    digests = {t["name"]: t["sha256"] for t in catalog["tables"]}

    designs = [
        Design(**r)
        for r in _read_csv(
            root / "data" / "designs.csv",
            {"design_id": str, "batch_id": str, "seal": str},
        )
    ]
    specimens = [
        Specimen(
            specimen_id=r["specimen_id"], design_id=r["design_id"], batch_id=r["batch_id"],
            protocol=r["protocol"], stop_reason=r["stop_reason"],
            end_hour=int(r["end_hour"]), physical_state=r["physical_state"],
        )
        for r in _read_csv(
            root / "data" / "specimens.csv",
            {
                "specimen_id": str, "design_id": str, "batch_id": str, "protocol": str,
                "stop_reason": str, "end_hour": str, "physical_state": str,
            },
        )
    ]
    exposures = [
        Exposure(
            exposure_id=r["exposure_id"], specimen_id=r["specimen_id"],
            start_hour=int(r["start_hour"]), end_hour=int(r["end_hour"]),
            salt_g_l=float(r["salt_g_l"]), humidity_pct=float(r["humidity_pct"]),
            stage=r["stage"],
        )
        for r in _read_csv(
            root / "data" / "exposures.csv",
            {
                "exposure_id": str, "specimen_id": str, "start_hour": str,
                "end_hour": str, "salt_g_l": str, "humidity_pct": str, "stage": str,
            },
        )
    ]
    inspections = [
        Inspection(
            inspection_id=r["inspection_id"], specimen_id=r["specimen_id"],
            hour=int(r["hour"]), finding=r["finding"],
            available_at=_parse_dt(r["available_at"]),
        )
        for r in _read_csv(
            root / "data" / "inspections.csv",
            {
                "inspection_id": str, "specimen_id": str, "hour": str,
                "finding": str, "available_at": str,
            },
        )
    ]
    field_rows = _read_csv(
        root / "data" / "field_spells.csv",
        {
            "spell_id": str, "unit_id": str, "design_id": str, "environment": str,
            "start": str, "end": str, "last_clear_day": str, "last_seen_day": str,
            "end_reason": str, "available_at": str,
        },
    )
    field_spells = [
        FieldSpell(
            spell_id=r["spell_id"], unit_id=r["unit_id"], design_id=r["design_id"],
            environment=r["environment"], start=_parse_date(r["start"]),
            end=_parse_date(r["end"]), last_clear_day=int(r["last_clear_day"]),
            last_seen_day=int(r["last_seen_day"]), end_reason=r["end_reason"],
            available_at=_parse_dt(r["available_at"]),
        )
        for r in field_rows
    ]
    request_rows = _read_csv(
        root / "data" / "requests.csv",
        {
            "request_id": str, "environment": str, "share": str,
            "duration_days": str, "risk_limit": str, "cabinets": str, "decision_by": str,
        },
    )
    requests = [
        RequestRow(
            request_id=r["request_id"], environment=r["environment"],
            share=float(r["share"]), duration_days=int(r["duration_days"]),
            risk_limit=float(r["risk_limit"]), cabinets=int(r["cabinets"]),
            decision_by=_parse_date(r["decision_by"]),
        )
        for r in request_rows
    ]
    chamber_rows = _read_csv(
        root / "data" / "chambers.csv",
        {
            "slot_id": str, "chamber_id": str, "protocol": str, "start": str,
            "end": str, "capacity": str, "cost_each": str, "reserved": str,
        },
    )
    chambers = [
        ChamberSlot(
            slot_id=r["slot_id"], chamber_id=r["chamber_id"], protocol=r["protocol"],
            start=_parse_date(r["start"]), end=_parse_date(r["end"]),
            capacity=int(r["capacity"]), cost_each=float(r["cost_each"]),
            reserved=int(r["reserved"]),
        )
        for r in chamber_rows
    ]
    action_rows = _read_csv(
        root / "data" / "test_actions.csv",
        {
            "action_id": str, "protocol": str, "destructive": str,
            "hours": str, "cost": str,
        },
    )
    actions = [
        TestAction(
            action_id=r["action_id"], protocol=r["protocol"],
            destructive=_bool(r["destructive"]), hours=int(r["hours"]),
            cost=float(r["cost"]),
        )
        for r in action_rows
    ]
    budget_rows = _read_csv(
        root / "data" / "planning_budget.csv",
        {"plan_id": str, "budget": str, "decision_date": str},
    )
    budgets = [
        Budget(plan_id=r["plan_id"], budget=float(r["budget"]),
               decision_date=_parse_date(r["decision_date"]))
        for r in budget_rows
    ]

    _validate_invariants(
        designs, specimens, exposures, inspections, field_spells,
        requests, chambers, actions,
    )

    known_times = [i.available_at for i in inspections] + [f.available_at for f in field_spells]
    effective_as_of = as_of or max(known_times)
    excluded = {}
    if as_of is not None:
        kept_insp = [i for i in inspections if i.available_at <= effective_as_of]
        excluded["inspections"] = len(inspections) - len(kept_insp)
        inspections = kept_insp
        kept_spells = [f for f in field_spells if f.available_at <= effective_as_of]
        excluded["field_spells"] = len(field_spells) - len(kept_spells)
        field_spells = kept_spells

    return EvidenceStore(
        root=root, as_of=effective_as_of, catalog_digests=digests, designs=designs,
        specimens=specimens, exposures=exposures, inspections=inspections,
        field_spells=field_spells, requests=requests, chambers=chambers,
        actions=actions, budgets=budgets, excluded=excluded,
    )


def _validate_invariants(designs, specimens, exposures, inspections, field_spells,
                         requests, chambers, actions):
    """业务不变量；任一不成立直接拒绝分析，避免静默错误。"""
    design_ids = {d.design_id for d in designs}
    batch_ids = {d.batch_id for d in designs}
    specimen_ids = {s.specimen_id for s in specimens}

    for s in specimens:
        if s.design_id not in design_ids:
            raise ValueError(f"试样 {s.specimen_id} 引用未知设计 {s.design_id}")
        if s.batch_id not in batch_ids:
            raise ValueError(f"试样 {s.specimen_id} 引用未知批次 {s.batch_id}")
        if s.stop_reason not in LAB_CENSOR_REASONS:
            raise ValueError(f"试样 {s.specimen_id} 停试原因非法：{s.stop_reason}")
        if s.physical_state not in ("available", "destructed"):
            raise ValueError(f"试样 {s.specimen_id} 实物状态非法：{s.physical_state}")

    exp_by_specimen = {}
    for e in exposures:
        if e.specimen_id not in specimen_ids:
            raise ValueError(f"暴露段 {e.exposure_id} 引用未知试样")
        if not e.start_hour < e.end_hour:
            raise ValueError(f"暴露段 {e.exposure_id} 小时区间必须左小右大")
        if e.stage not in STAGES:
            raise ValueError(f"暴露段 {e.exposure_id} 阶段非法：{e.stage}")
        exp_by_specimen.setdefault(e.specimen_id, []).append(e)
    specimen_end = {s.specimen_id: s.end_hour for s in specimens}
    for sid, segs in exp_by_specimen.items():
        ordered = sorted(segs, key=lambda x: x.start_hour)
        if ordered[0].start_hour != 0:
            raise ValueError(f"试样 {sid} 暴露未从 0 小时起算")
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev.end_hour != nxt.start_hour:
                raise ValueError(f"试样 {sid} 暴露段不连续，转舱经历不得抹断")
        if ordered[-1].end_hour != specimen_end[sid]:
            raise ValueError(f"试样 {sid} 暴露终点与 end_hour 不一致")
        profiles = {(x.salt_g_l, x.humidity_pct) for x in segs}
        if len(profiles) != 1:
            raise ValueError(f"试样 {sid} 盐雾/湿度剖面不唯一，拒绝混合协议解释")

    for i in inspections:
        if i.specimen_id not in specimen_ids:
            raise ValueError(f"巡检 {i.inspection_id} 引用未知试样")
        if i.finding not in ("clear", "corrosion"):
            raise ValueError(f"巡检 {i.inspection_id} 结论非法：{i.finding}")
        if i.hour > specimen_end[i.specimen_id]:
            raise ValueError(f"巡检 {i.inspection_id} 晚于试样结束小时")

    spell_units = {}
    for f in field_spells:
        if f.design_id not in design_ids:
            raise ValueError(f"现场经历 {f.spell_id} 引用未知设计")
        if f.end_reason not in FIELD_END_REASONS:
            raise ValueError(f"现场经历 {f.spell_id} 结局非法：{f.end_reason}")
        if not f.last_clear_day <= f.last_seen_day:
            raise ValueError(f"现场经历 {f.spell_id} 末次清晰晚于末次可见")
        spell_units.setdefault(f.unit_id, []).append(f)
    for unit, spells in spell_units.items():
        ordered = sorted(spells, key=lambda x: x.start)
        for prev, nxt in zip(ordered, ordered[1:]):
            # 同一设备换密封：新段必须从旧段终止日开始，且设计必须不同
            if nxt.start != prev.end:
                raise ValueError(f"设备 {unit} 现场经历不连续")
            if nxt.design_id == prev.design_id:
                raise ValueError(
                    f"设备 {unit} 换密封后仍挂同一设计 {nxt.design_id}，换型语义不成立"
                )
        for f in ordered:
            if f.end_reason == "seal_change" and f is ordered[-1]:
                raise ValueError(f"设备 {unit} 末段不能以 seal_change 结束")

    seen_requests = set()
    shares = {}
    for r in requests:
        key = (r.request_id, r.environment)
        if key in seen_requests:
            raise ValueError(f"用途环境权重重复：{key}")
        seen_requests.add(key)
        shares.setdefault(r.request_id, 0.0)
        shares[r.request_id] += r.share
        if not 0.0 < r.share <= 1.0 or not 0.0 < r.risk_limit < 1.0:
            raise ValueError(f"用途 {r.request_id} 权重或风险上限越界")
    for rid, total in shares.items():
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"用途 {rid} 环境构成比例之和必须为 1，实际 {total}")

    for c in chambers:
        if not c.start < c.end:
            raise ValueError(f"舱位 {c.slot_id} 日期区间非法")
        if c.reserved > c.capacity:
            raise ValueError(f"舱位 {c.slot_id} 已占用超过容量")
    for a in actions:
        if a.hours <= 0 or a.cost < 0:
            raise ValueError(f"动作 {a.action_id} 小时或费用非法")
