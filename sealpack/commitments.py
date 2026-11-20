"""客户用途评估、承诺快照台账与换版影响。

承诺查询把客户条件（环境构成、时长、风险上限）与当前证据逐单元连接：
- 总体风险是各环境单元按需求构成加权；任一环境完全没有可识别证据时，
  总体不允许给出 supported，未覆盖环境单独列出，不被平均结果掩盖；
- 同一平均风险下，adverse（观测到较差表现）与 limited（支持范围有限）分开；
- 资料不足的工况保留 unknown，不做通过率混合。

台账只追加、不改写：保存承诺时冻结当时资料指纹、逐单元证据句柄（巡检/经历
标识与剔除理由）和限制。后来补入失效检查或设计更正时，已确认承诺原样保留，
重新评估只输出影响清单；显式换版才生成引用原承诺编号的新快照。
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

from .evidence import (
    FIELD_STREAM, SUPPORTED, LIMITED, ADVERSE, UNKNOWN, STATUS_LABELS,
    classify_cell,
)

OVERALL_ORDER = {SUPPORTED: 0, LIMITED: 1, ADVERSE: 2, UNKNOWN: 3}


def _conditions_from_store(store, request_id):
    rows = [r for r in store.requests if r.request_id == request_id]
    if not rows:
        raise KeyError(f"资料中没有用途 {request_id}")
    return [{
        "environment": r.environment, "share": r.share,
        "duration_days": r.duration_days, "risk_limit": r.risk_limit,
    } for r in rows], rows[0].cabinets, rows[0].decision_by


def evaluate_conditions(store, cells, conditions, cabinets, *, source="custom",
                        request_id=None, min_subjects=6,
                        replicates=500, seed=7123):
    """对一组客户条件逐设计评估，返回每个设计的承诺结论与数量依据。"""
    if not conditions:
        raise ValueError("客户条件不能为空")
    share_total = sum(c["share"] for c in conditions)
    if abs(share_total - 1.0) > 1e-9:
        raise ValueError(
            f"环境构成比例之和必须为 1，实际 {share_total:.6f}；"
            "不允许用未归一化权重做平均"
        )
    for c in conditions:
        if not 0.0 < c["risk_limit"] < 1.0 or c["duration_days"] <= 0:
            raise ValueError("风险上限须在 (0,1)，承诺时长须为正")
    designs = sorted({d.design_id for d in store.designs})
    results = []
    for design in designs:
        per_env, uncovered = [], []
        weighted_point = 0.0
        weighted_upper = 0.0
        for cond in conditions:
            cell = cells.get((FIELD_STREAM, (design, cond["environment"])))
            verdict = classify_cell(
                cell, cond["duration_days"], cond["risk_limit"],
                min_subjects=min_subjects, replicates=replicates, seed=seed,
            )
            entry = {
                "environment": cond["environment"],
                "share": cond["share"],
                "duration_days": cond["duration_days"],
                "risk_limit": cond["risk_limit"],
                "status": verdict["status"],
                "status_label": STATUS_LABELS[verdict["status"]],
                "reason": verdict.get("reason"),
                "n_subjects": verdict.get("n_subjects", 0),
                "n_events": verdict.get("n_events", 0),
                "max_followup": verdict.get("max_followup"),
                "point": (verdict["risk"] or {}).get("point"),
                "upper": (verdict["risk"] or {}).get("upper"),
                "upper_source": (verdict["risk"] or {}).get("upper_source"),
                "event_traces": verdict.get("event_traces", []),
                "censor_traces": verdict.get("censor_traces", []),
            }
            per_env.append(entry)
            if verdict["status"] == UNKNOWN:
                uncovered.append(cond["environment"])
            else:
                weighted_point += cond["share"] * entry["point"]
                weighted_upper += cond["share"] * entry["upper"]

        statuses = {e["status"] for e in per_env}
        if uncovered:
            overall = UNKNOWN
        elif ADVERSE in statuses:
            overall = ADVERSE
        elif statuses == {SUPPORTED}:
            overall = SUPPORTED
        else:
            overall = LIMITED
        results.append({
            "design_id": design,
            "overall_status": overall,
            "overall_label": STATUS_LABELS[overall],
            "per_environment": per_env,
            "uncovered_environments": uncovered,
            "weighted_point_risk": None if uncovered else round(weighted_point, 6),
            "weighted_upper_risk": None if uncovered else round(weighted_upper, 6),
            "servable_cabinets": cabinets if overall == SUPPORTED else 0,
        })
    return {
        "source": source,
        "request_id": request_id,
        "conditions": conditions,
        "requested_cabinets": cabinets,
        "min_subjects": min_subjects,
        "replicates": replicates,
        "seed": seed,
        "designs": results,
    }


def evaluate_request(store, cells, request_id, **kwargs):
    conditions, cabinets, decision_by = _conditions_from_store(store, request_id)
    out = evaluate_conditions(store, cells, conditions, cabinets,
                              source="requests.csv", request_id=request_id, **kwargs)
    out["decision_by"] = decision_by.isoformat()
    return out


# ---------------------------------------------------------------- 缩小/降档情景

def scenario_narrow(evaluation, drop_environments):
    """客户接受缩小适用环境：剔除环境后重归一化权重并重算加权风险。

    可接需求按保留环境的原始构成比例折算；被剔除环境的柜数不再承诺。
    """
    keep = [c for c in evaluation["conditions"]
            if c["environment"] not in set(drop_environments)]
    if not keep:
        raise ValueError("至少保留一个环境")
    scale = sum(c["share"] for c in keep)
    scaled = [{**c, "share": c["share"] / scale} for c in keep]
    out_designs = []
    for d in evaluation["designs"]:
        env_rows = [e for e in d["per_environment"]
                    if e["environment"] not in set(drop_environments)]
        # 只看保留环境：被剔除环境原本是否未知与本情景无关
        if any(e["status"] == UNKNOWN for e in env_rows):
            overall = UNKNOWN
            wp = wu = None
        else:
            wp = sum(e["share"] * e["point"] for e in env_rows) / scale
            wu = sum(e["share"] * e["upper"] for e in env_rows) / scale
            if any(e["status"] == ADVERSE for e in env_rows):
                overall = ADVERSE
            elif all(e["status"] == SUPPORTED for e in env_rows):
                overall = SUPPORTED
            else:
                overall = LIMITED
        out_designs.append({
            "design_id": d["design_id"],
            "overall_status": overall,
            "overall_label": STATUS_LABELS[overall],
            "weighted_point_risk": None if wp is None else round(wp, 6),
            "weighted_upper_risk": None if wu is None else round(wu, 6),
            "servable_cabinets": round(
                evaluation["requested_cabinets"] * scale, 3
            ) if overall == SUPPORTED else 0,
        })
    return {
        "kind": "narrow_environment",
        "dropped": list(drop_environments),
        "kept_share_of_original": round(scale, 6),
        "conditions": scaled,
        "designs": out_designs,
    }


def scenario_reduce_duration(store, cells, evaluation, new_duration_days):
    """客户接受降低承诺时长：在新时长上重新评估全部环境单元。"""
    conditions = [{**c, "duration_days": new_duration_days}
                  for c in evaluation["conditions"]]
    out = evaluate_conditions(
        store, cells, conditions, evaluation["requested_cabinets"],
        source="scenario_reduced_duration",
        min_subjects=evaluation["min_subjects"],
        replicates=evaluation["replicates"], seed=evaluation["seed"],
    )
    out["kind"] = "reduce_duration"
    out["original_duration_days"] = evaluation["conditions"][0]["duration_days"]
    return out


# ---------------------------------------------------------------- 承诺台账

def _ledger_path(ledger_dir):
    path = Path(ledger_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_commitment(ledger_dir, evaluation, store, chosen_design, note=""):
    """冻结一次承诺快照；台账只追加，返回新记录 id。"""
    directory = _ledger_path(ledger_dir)
    record_id = "CMT-" + uuid.uuid4().hex[:10].upper()
    design_eval = next(d for d in evaluation["designs"]
                       if d["design_id"] == chosen_design)
    if design_eval["overall_status"] == UNKNOWN:
        raise ValueError("证据未知的工况不能保存为承诺，只能保留为未支持结论")
    snapshot = {
        "commitment_id": record_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "issued",
        "supersedes": None,
        "note": note,
        "chosen_design": chosen_design,
        "verdict": {
            "overall_status": design_eval["overall_status"],
            "overall_label": design_eval["overall_label"],
            "weighted_point_risk": design_eval["weighted_point_risk"],
            "weighted_upper_risk": design_eval["weighted_upper_risk"],
            "servable_cabinets": design_eval["servable_cabinets"],
        },
        "conditions": evaluation["conditions"],
        "requested_cabinets": evaluation["requested_cabinets"],
        "evidence_basis": [
            {
                "environment": e["environment"],
                "status": e["status"],
                "n_subjects": e["n_subjects"],
                "n_events": e["n_events"],
                "max_followup": e["max_followup"],
                "point": e["point"],
                "upper": e["upper"],
                "event_ids": [t["spell_id"] for t in e["event_traces"]],
                "censor_ids": [t["spell_id"] for t in e["censor_traces"]],
                "censor_reasons": {
                    reason: sum(1 for t in e["censor_traces"]
                                if t["outcome"] == reason)
                    for reason in sorted({t["outcome"] for t in e["censor_traces"]})
                },
            }
            for e in design_eval["per_environment"]
        ],
        "limits": {
            "no_acceleration_conversion": True,
            "internal_decision_only": True,
            "unidentified_horizons_not_covered": True,
        },
        "data_fingerprint": {
            "as_of": store.as_of.isoformat(),
            "digests": store.digest_fingerprint(),
            "excluded_at_as_of": store.excluded,
        },
        "analysis_config": {
            "min_subjects": evaluation["min_subjects"],
            "replicates": evaluation["replicates"],
            "seed": evaluation["seed"],
        },
    }
    target = directory / f"{record_id}.json"
    if target.exists():
        raise RuntimeError("承诺 id 冲突，拒绝覆盖")
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return record_id


def list_commitments(ledger_dir):
    directory = _ledger_path(ledger_dir)
    items = []
    for path in sorted(directory.glob("CMT-*.json")):
        items.append(json.loads(path.read_text(encoding="utf-8")))
    return items


def load_commitment(ledger_dir, commitment_id):
    path = _ledger_path(ledger_dir) / f"{commitment_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def reassess_commitments(ledger_dir, store, cells):
    """用当前证据重新评估每份已发承诺；原记录不改写，只输出影响清单。"""
    impacts = []
    for snapshot in list_commitments(ledger_dir):
        current = evaluate_conditions(
            store, cells, snapshot["conditions"],
            snapshot["requested_cabinets"], source="reassessment",
            min_subjects=snapshot["analysis_config"]["min_subjects"],
            replicates=snapshot["analysis_config"]["replicates"],
            seed=snapshot["analysis_config"]["seed"],
        )
        now_eval = next(d for d in current["designs"]
                        if d["design_id"] == snapshot["chosen_design"])
        old = snapshot["verdict"]
        changed = now_eval["overall_status"] != old["overall_status"]
        env_changes = []
        old_by_env = {e["environment"]: e for e in snapshot["evidence_basis"]}
        for e in now_eval["per_environment"]:
            prior = old_by_env.get(e["environment"])
            if prior is None or prior["status"] != e["status"]:
                env_changes.append({
                    "environment": e["environment"],
                    "from": None if prior is None else prior["status"],
                    "to": e["status"],
                    "point": e["point"],
                    "upper": e["upper"],
                })
        fingerprint_changed = (
            store.digest_fingerprint() != snapshot["data_fingerprint"]["digests"]
        )
        impacts.append({
            "commitment_id": snapshot["commitment_id"],
            "chosen_design": snapshot["chosen_design"],
            "issued_status": old["overall_status"],
            "current_status": now_eval["overall_status"],
            "status_changed": changed,
            "environment_changes": env_changes,
            "fingerprint_changed": fingerprint_changed,
            "action_required": "需显式换版，原承诺不得悄悄替换" if (changed or env_changes)
                               else "结论不变，原承诺继续有效",
        })
    return impacts


def supersede_commitment(ledger_dir, new_evaluation, store, old_commitment_id,
                         chosen_design, note=""):
    """显式换版：生成引用原编号的新快照，并把旧快照标记为 superseded。"""
    old = load_commitment(ledger_dir, old_commitment_id)
    new_id = save_commitment(ledger_dir, new_evaluation, store, chosen_design, note=note)
    old["status"] = "superseded"
    old["superseded_by"] = new_id
    (Path(ledger_dir) / f"{old_commitment_id}.json").write_text(
        json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    new = load_commitment(ledger_dir, new_id)
    new["supersedes"] = old_commitment_id
    (Path(ledger_dir) / f"{new_id}.json").write_text(
        json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return new_id
