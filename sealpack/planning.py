"""试验安排校验、资源竞争判断与情景推演。

硬约束（违反即不可执行）：
- 动作协议必须与舱位协议、与试样既有暴露协议一致（转舱允许，但前序暴露不抹掉，
  不同协议不能混成一个通过率）；
- 舱位剩余容量 = capacity - reserved（含外部追加占用）- 已排试样数；
- 已破坏试样不得再排；破坏性动作执行后该试样在本计划内即视为消耗，不能再延续；
- 同一试样不能被多个销售请求/多份计划重复占用，时间重叠的舱段也不行；
- 总费用（动作费用 + 舱位每样费用）不得超过本轮预算；
- 舱段开始日不得早于安排决策日。

未知结果只能作为情景：plan_scenario 只描述覆盖范围如何变化，
不把假设结局写进证据，也不提前改变任何承诺。
"""
from __future__ import annotations

from dataclasses import dataclass

from .evidence import LAB_STREAM, severity_map


@dataclass
class Placement:
    slot_id: str
    action_id: str
    specimen_id: str
    purpose: dict = None

    @classmethod
    def from_dict(cls, raw):
        return cls(
            slot_id=raw["slot_id"], action_id=raw["action_id"],
            specimen_id=raw["specimen_id"], purpose=raw.get("purpose", {}),
        )


def validate_plan(store, plan, extra_reservations=None, blocked_specimens=None):
    """校验单份安排；返回结构化错误/警告与费用、占用清单。"""
    extra_reservations = extra_reservations or {}
    blocked = set(blocked_specimens or [])
    slots = {c.slot_id: c for c in store.chambers}
    actions = {a.action_id: a for a in store.actions}
    specimens = {s.specimen_id: s for s in store.specimens}
    decision_date = max(b.decision_date for b in store.budgets)
    budget = sum(b.budget for b in store.budgets)

    errors, warnings = [], []
    placements = [Placement.from_dict(p) for p in plan.get("placements", [])]
    slot_use, specimen_use, consumed = {}, {}, set()
    total_cost = 0.0

    for idx, p in enumerate(placements):
        where = f"placements[{idx}]"
        slot = slots.get(p.slot_id)
        action = actions.get(p.action_id)
        specimen = specimens.get(p.specimen_id)
        if slot is None:
            errors.append(f"{where}: 未知舱段 {p.slot_id}")
            continue
        if action is None:
            errors.append(f"{where}: 未知动作 {p.action_id}")
            continue
        if specimen is None:
            errors.append(f"{where}: 未知试样 {p.specimen_id}")
            continue

        if action.protocol != slot.protocol:
            errors.append(f"{where}: 动作协议 {action.protocol} 与舱段协议 "
                          f"{slot.protocol} 不一致")
        if action.protocol != specimen.protocol:
            errors.append(f"{where}: 试样 {p.specimen_id} 前序协议为 "
                          f"{specimen.protocol}，不能改挂 {action.protocol} 混合解释")
        if specimen.physical_state == "destructed" or p.specimen_id in blocked:
            errors.append(f"{where}: 试样 {p.specimen_id} 已破坏或被其他项目占用")
        if p.specimen_id in consumed:
            errors.append(f"{where}: 试样 {p.specimen_id} 已经历破坏性拆检，"
                          "不能再安排延续暴露")
        if slot.start < decision_date:
            errors.append(f"{where}: 舱段 {slot.slot_id} 早于安排日 {decision_date}")

        reserved_total = slot.reserved + extra_reservations.get(slot.slot_id, 0)
        slot_use.setdefault(p.slot_id, []).append(p.specimen_id)
        if reserved_total + len(slot_use[p.slot_id]) > slot.capacity:
            errors.append(f"{where}: 舱段 {slot.slot_id} 容量超员 "
                          f"(占用 {reserved_total + len(slot_use[p.slot_id])} "
                          f"/ 容量 {slot.capacity})")

        prior = specimen_use.get(p.specimen_id)
        if prior is not None:
            prior_slot = slots[prior.slot_id]
            if max(slot.start, prior_slot.start) < min(slot.end, prior_slot.end):
                errors.append(f"{where}: 试样 {p.specimen_id} 舱段时间重叠，"
                              "同一试样不能同时入两舱")
            if actions[prior.action_id].destructive:
                errors.append(f"{where}: 试样 {p.specimen_id} 前序动作已是破坏性拆检")
        specimen_use[p.specimen_id] = p
        if action.destructive:
            consumed.add(p.specimen_id)

        total_cost += action.cost + slot.cost_each

    if total_cost > budget + 1e-9:
        errors.append(f"总费用 {total_cost:.0f} 元超出本轮预算 {budget:.0f} 元")

    return {
        "plan_id": plan.get("plan_id"),
        "feasible": not errors,
        "errors": errors,
        "warnings": warnings,
        "n_placements": len(placements),
        "specimens_used": sorted(specimen_use),
        "specimens_consumed": sorted(consumed),
        "slot_occupancy": {k: len(v) for k, v in sorted(slot_use.items())},
        "total_cost": total_cost,
        "budget": budget,
        "budget_remaining": round(budget - total_cost, 2),
    }


def validate_multi(store, plans):
    """跨多份销售请求/计划联合校验：同一份试样不得被重复占用。"""
    reports = []
    seen_specimens = {}
    union_slot_use = {}
    for plan in plans:
        report = validate_plan(store, plan)
        for p in plan.get("placements", []):
            owner = plan.get("plan_id", "?")
            if p["specimen_id"] in seen_specimens:
                report["feasible"] = False
                report["errors"].append(
                    f"试样 {p['specimen_id']} 已被 {seen_specimens[p['specimen_id']]} "
                    f"占用，{owner} 不能重复占用"
                )
            seen_specimens[p["specimen_id"]] = owner
            union_slot_use.setdefault(p["slot_id"], []).append(p["specimen_id"])
        reports.append(report)
    for slot_id, used in union_slot_use.items():
        slot = next(c for c in store.chambers if c.slot_id == slot_id)
        if slot.reserved + len(used) > slot.capacity:
            reports[-1]["feasible"] = False
            reports[-1]["errors"].append(
                f"联合排舱后 {slot_id} 超员：{len(used)} 份安排 + 已占 {slot.reserved} "
                f"> 容量 {slot.capacity}"
            )
    return reports


# ---------------------------------------------------------------- 缺口与情景

def coverage_gaps(store, cells, evaluation=None):
    """列出当前证据相对承诺/探索目标的缺口。

    现场缺口（unknown/limited/adverse 的设计×环境单元）只能由后续现场观测补齐：
    本应用不提供加速小时到服役年限的换算，试验室排舱不能直接关闭现场缺口。
    试验室缺口按 (设计,协议) 给出当前最大随访小时与可延伸动作。
    """
    field_gaps, lab_gaps = [], []
    if evaluation is not None:
        for d in evaluation["designs"]:
            for env in d["per_environment"]:
                if env["status"] != "supported":
                    field_gaps.append({
                        "design_id": d["design_id"],
                        "environment": env["environment"],
                        "status": env["status"],
                        "reason": env["reason"],
                        "horizon_days": env["duration_days"],
                        "n_subjects": env["n_subjects"],
                        "max_followup": env["max_followup"],
                        "closable_by_lab": False,
                        "note": "无加速换算，现场承诺缺口需现场观测或缩小/降档处理",
                    })

    action_by_protocol = {}
    for a in store.actions:
        action_by_protocol.setdefault(a.protocol, []).append(
            {"action_id": a.action_id, "destructive": a.destructive,
             "hours": a.hours, "cost": a.cost}
        )
    for (stream, key), cell in sorted(cells.items(), key=lambda kv: str(kv[0])):
        if stream != LAB_STREAM:
            continue
        design_id, protocol = key
        lab_gaps.append({
            "design_id": design_id,
            "protocol": protocol,
            "current_n": cell.n_subjects,
            "current_events": cell.n_events,
            "current_max_hour": cell.max_followup,
            "available_actions": action_by_protocol.get(protocol, []),
            "destructed_specimens": [
                s.specimen_id for s in store.specimens
                if s.design_id == design_id and s.protocol == protocol
                and s.physical_state == "destructed"
            ],
            "available_specimens": [
                s.specimen_id for s in store.specimens
                if s.design_id == design_id and s.protocol == protocol
                and s.physical_state == "available"
            ],
        })
    return {"field_gaps": field_gaps, "lab_gaps": lab_gaps,
            "severity_profile": severity_map(store)}


def plan_scenario(store, cells, plan, evaluation):
    """描述安排若执行将如何改变证据覆盖；全部未来结局标注 hypothetical。

    只计算可识别随访的延伸与样本量变化，不伪造风险数值，不改变当前承诺。
    """
    slots = {c.slot_id: c for c in store.chambers}
    actions = {a.action_id: a for a in store.actions}
    specimens = {s.specimen_id: s for s in store.specimens}
    affected = {}
    for raw in plan.get("placements", []):
        p = Placement.from_dict(raw)
        sp = specimens[p.specimen_id]
        action = actions[p.action_id]
        cell_key = (LAB_STREAM, (sp.design_id, sp.protocol))
        cell = cells.get(cell_key)
        entry = affected.setdefault(cell_key, {
            "design_id": sp.design_id, "protocol": sp.protocol,
            "current_n": cell.n_subjects if cell else 0,
            "current_max_hour": cell.max_followup if cell else 0.0,
            "added_subjects": [], "destructive_subjects": [],
            "extended_followup_hour": cell.max_followup if cell else 0.0,
        })
        entry["added_subjects"].append(p.specimen_id)
        new_hour = sp.end_hour + action.hours
        entry["extended_followup_hour"] = max(entry["extended_followup_hour"], new_hour)
        if action.destructive:
            entry["destructive_subjects"].append(p.specimen_id)

    scenarios = []
    for key, change in sorted(affected.items(), key=lambda kv: str(kv[0])):
        scenarios.append({
            "target": {"design_id": change["design_id"],
                       "protocol": change["protocol"]},
            "coverage_change": {
                "current_n_subjects": change["current_n"],
                "planned_added_subjects": len(change["added_subjects"]),
                "current_max_followup_hour": change["current_max_hour"],
                "extended_followup_hour": change["extended_followup_hour"],
                "specimens_destroyed_after_plan": change["destructive_subjects"],
            },
            "hypothetical_outcomes": [
                {
                    "case": "若新增经历在终点巡检仍为 clear",
                    "effect": "右删失样本增加，自助区间可能收窄；真实分级须重算",
                    "evidence_now": False,
                },
                {
                    "case": "若两次巡检之间首次出现 corrosion",
                    "effect": "新增区间失效单元，可能把 limited 推为 adverse；"
                              "真实分级须重算",
                    "evidence_now": False,
                },
            ],
            "note": "情景不是证据；在真实巡检 available_at 到达前，承诺结论不变",
        })

    field_gap_ids = [(g["design_id"], g["environment"])
                     for g in coverage_gaps(store, cells, evaluation)["field_gaps"]]
    return {
        "plan_id": plan.get("plan_id"),
        "lab_cells_affected": scenarios,
        "field_gaps_remaining": [
            {"design_id": d, "environment": e} for d, e in field_gap_ids
        ],
        "commitment_change_policy": "新证据到达并重新运行评估前，已发承诺保持原样",
    }


def recheck_after_occupancy(store, cells, plan, evaluation,
                            extra_reservations, blocked_specimens):
    """舱位/试样被其他项目占用后重新判断可执行性与仍开放的承诺缺口。"""
    report = validate_plan(
        store, plan, extra_reservations=extra_reservations,
        blocked_specimens=blocked_specimens,
    )
    open_gaps = coverage_gaps(store, cells, evaluation)["field_gaps"]
    if report["feasible"]:
        report["occupancy_effect"] = "安排仍可执行，承诺缺口清单不变（证据未变化）"
    else:
        report["occupancy_effect"] = (
            "安排已不可执行；受影响的补证目标退回缺口清单，"
            "已发承诺不因资源竞争而改写，只是补证延后"
        )
    report["open_field_gaps"] = [
        {"design_id": g["design_id"], "environment": g["environment"],
         "status": g["status"]}
        for g in open_gaps
    ]
    return report
