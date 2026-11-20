"""命令行入口：可重复分析运行、承诺查询与试验安排。

统一约定：
- 所有读取资料目录的命令都以隔离模式运行（python -I -m sealpack ...），
  脚本不依赖工作目录，资料路径通过 --data 显式传入；
- JSON 输出含资料指纹、随机种子与逐记录溯源句柄，同输入同种子可完全复现；
- 默认 stdout 输出人可读报告，--json 输出机器可读结构，--out 落盘。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .commitments import (
    evaluate_conditions, evaluate_request, list_commitments,
    reassess_commitments, save_commitment,
    scenario_narrow, scenario_reduce_duration, supersede_commitment,
)
from .evidence import (
    LAB_STREAM, FIELD_STREAM, build_cells, lab_diagnostic,
    compare_field,
)
from .planning import (
    coverage_gaps, plan_scenario, recheck_after_occupancy, validate_multi,
    validate_plan,
)
from .records import load_store
from .report import render_planning, render_run


def _emit(payload, text, args):
    if args.json:
        blob = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        blob = text
    if args.out:
        Path(args.out).write_text(blob, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(blob)


def _cell_summary(cells, stream):
    out = []
    for (st, key), cell in sorted(cells.items(), key=lambda kv: str(kv[0])):
        if st != stream or cell.result is None:
            continue
        out.append({
            "stream": st,
            "design_id": key[0],
            "cell_key": key[1],
            "n_subjects": cell.n_subjects,
            "n_events": cell.n_events,
            "n_clusters": cell.result.n_clusters,
            "max_followup": cell.max_followup,
            "censor_counts": dict(cell.result.censor_counts),
        })
    return out


def cmd_run(args):
    store = load_store(args.data, as_of=_parse_asof(args.as_of))
    cells = build_cells(store)
    diagnostic = lab_diagnostic(store, cells, args.common_hour,
                                replicates=args.replicates, seed=args.seed)
    request_evals = [
        evaluate_request(store, cells, rid, replicates=args.replicates,
                         seed=args.seed)
        for rid in sorted({r.request_id for r in store.requests})
    ]
    environments = sorted({r.environment for r in store.requests})
    durations = sorted({r.duration_days for r in store.requests})
    comparisons = [
        compare_field(store, cells, env, durations[0], 0.05,
                      replicates=args.replicates, seed=args.seed)
        for env in environments
    ]
    payload = {
        "run_kind": "full_analysis",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data_fingerprint": {
            "as_of": store.as_of.isoformat(),
            "digests": store.digest_fingerprint(),
            "excluded_at_as_of": store.excluded,
        },
        "config": {
            "common_hour": args.common_hour,
            "replicates": args.replicates,
            "seed": args.seed,
            "min_subjects": 6,
        },
        "lab_cells": _cell_summary(cells, LAB_STREAM),
        "field_cells": _cell_summary(cells, FIELD_STREAM),
        "lab_diagnostic": diagnostic,
        "request_evaluations": request_evals,
        "field_comparisons": comparisons,
        "disclaimers": [
            "仅内部工程决策，不签发法定认证",
            "试验室小时不换算为服役年限",
            "未知工况保留 unknown，不混合协议通过率",
        ],
    }
    _emit(payload, render_run(payload), args)


def cmd_query(args):
    store = load_store(args.data, as_of=_parse_asof(args.as_of))
    cells = build_cells(store)
    if args.request:
        payload = evaluate_request(store, cells, args.request,
                                   replicates=args.replicates, seed=args.seed)
    else:
        conditions = json.loads(Path(args.conditions).read_text(encoding="utf-8"))
        payload = evaluate_conditions(
            store, cells, conditions["conditions"], conditions["cabinets"],
            source="adhoc", min_subjects=conditions.get("min_subjects", 6),
            replicates=args.replicates, seed=args.seed,
        )
    _emit(payload, json.dumps(payload, ensure_ascii=False, indent=2, default=str), args)


def cmd_save(args):
    store = load_store(args.data)
    cells = build_cells(store)
    evaluation = (
        evaluate_request(store, cells, args.request,
                         replicates=args.replicates, seed=args.seed)
        if args.request else
        evaluate_conditions(
            store, cells,
            json.loads(Path(args.conditions).read_text(encoding="utf-8"))["conditions"],
            json.loads(Path(args.conditions).read_text(encoding="utf-8"))["cabinets"],
            source="adhoc", replicates=args.replicates, seed=args.seed,
        )
    )
    cid = save_commitment(args.ledger, evaluation, store, args.design, note=args.note)
    print(f"承诺已冻结：{cid}（台账 {args.ledger}，原证据与限制已随快照保存）")


def cmd_list(args):
    items = list_commitments(args.ledger)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2, default=str))
        return
    for s in items:
        print(f"{s['commitment_id']} [{s['status']}] 设计 {s['chosen_design']} "
              f"结论 {s['verdict']['overall_label']} 建于 {s['created_at']}")


def cmd_impact(args):
    store = load_store(args.data, as_of=_parse_asof(args.as_of))
    cells = build_cells(store)
    payload = {"reassessed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "impacts": reassess_commitments(args.ledger, store, cells)}
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    _emit(payload, text, args)


def cmd_supersede(args):
    store = load_store(args.data)
    cells = build_cells(store)
    conditions = json.loads(Path(args.conditions).read_text(encoding="utf-8"))
    evaluation = evaluate_conditions(
        store, cells, conditions["conditions"], conditions["cabinets"],
        source="supersession", replicates=args.replicates, seed=args.seed,
    )
    new_id = supersede_commitment(args.ledger, evaluation, store,
                                  args.old_commitment, args.design, note=args.note)
    print(f"已显式换版：{args.old_commitment} -> {new_id}，原快照保留并标记 superseded")


def cmd_scenario(args):
    store = load_store(args.data)
    cells = build_cells(store)
    base = evaluate_request(store, cells, args.request,
                            replicates=args.replicates, seed=args.seed)
    if args.kind == "narrow":
        payload = scenario_narrow(base, args.drop.split(","))
    else:
        payload = scenario_reduce_duration(store, cells, base, args.duration)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_plan_validate(args):
    store = load_store(args.data)
    cells = build_cells(store)
    plans = [json.loads(Path(p).read_text(encoding="utf-8"))
             for p in args.plan]
    if len(plans) == 1:
        report = validate_plan(store, plans[0])
    else:
        reports = validate_multi(store, plans)
        report = {"plan_id": "multi", "feasible": all(r["feasible"] for r in reports),
                  "reports": reports,
                  "errors": [e for r in reports for e in r["errors"]],
                  "warnings": [], "n_placements": sum(r["n_placements"] for r in reports),
                  "specimens_used": sorted({s for r in reports
                                            for s in r["specimens_used"]}),
                  "specimens_consumed": sorted({s for r in reports
                                                for s in r["specimens_consumed"]}),
                  "slot_occupancy": {k: v for r in reports
                                     for k, v in r["slot_occupancy"].items()},
                  "total_cost": sum(r["total_cost"] for r in reports),
                  "budget": reports[0]["budget"],
                  "budget_remaining": reports[0]["budget"]
                                      - sum(r["total_cost"] for r in reports)}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_planning(report))


def cmd_plan_gaps(args):
    store = load_store(args.data)
    cells = build_cells(store)
    evaluation = evaluate_request(store, cells, args.request,
                                  replicates=args.replicates, seed=args.seed)
    payload = coverage_gaps(store, cells, evaluation)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_plan_scenario(args):
    store = load_store(args.data)
    cells = build_cells(store)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    evaluation = evaluate_request(store, cells, args.request,
                                  replicates=args.replicates, seed=args.seed)
    payload = plan_scenario(store, cells, plan, evaluation)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def cmd_plan_recheck(args):
    store = load_store(args.data)
    cells = build_cells(store)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    evaluation = evaluate_request(store, cells, args.request,
                                  replicates=args.replicates, seed=args.seed)
    overrides = json.loads(Path(args.overrides).read_text(encoding="utf-8"))
    report = recheck_after_occupancy(
        store, cells, plan, evaluation,
        extra_reservations=overrides.get("extra_reservations", {}),
        blocked_specimens=overrides.get("blocked_specimens", []),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_planning(report))


def _parse_asof(value):
    if not value:
        return None
    stamp = dt.datetime.fromisoformat(value)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="sealpack",
        description="沿海储能柜暴露证据分析与送试承诺（内部工程决策工具）",
    )
    parser.add_argument("--replicates", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7123)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="完整分析运行（证据单元+诊断+用途评估+比较）")
    p.add_argument("--data", required=True)
    p.add_argument("--as-of")
    p.add_argument("--common-hour", type=int, default=240)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("query", help="按资料用途或自定义条件查询承诺范围")
    p.add_argument("--data", required=True)
    p.add_argument("--as-of")
    p.add_argument("--request")
    p.add_argument("--conditions")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("save", help="冻结承诺快照到只追加台账")
    p.add_argument("--data", required=True)
    p.add_argument("--ledger", default="work/ledger")
    p.add_argument("--request")
    p.add_argument("--conditions")
    p.add_argument("--design", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("list", help="列出台账中的承诺")
    p.add_argument("--ledger", default="work/ledger")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("impact", help="资料变更后重评已发承诺（不改写原记录）")
    p.add_argument("--data", required=True)
    p.add_argument("--ledger", default="work/ledger")
    p.add_argument("--as-of")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("supersede", help="显式换版，新快照引用原承诺编号")
    p.add_argument("--data", required=True)
    p.add_argument("--ledger", default="work/ledger")
    p.add_argument("--old-commitment", required=True)
    p.add_argument("--conditions", required=True)
    p.add_argument("--design", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser("scenario", help="比较缩小环境/降低时长对风险与可接需求的影响")
    p.add_argument("--data", required=True)
    p.add_argument("--request", required=True)
    p.add_argument("--kind", choices=["narrow", "reduce"], required=True)
    p.add_argument("--drop", default="", help="kind=narrow 时逗号分隔的剔除环境")
    p.add_argument("--duration", type=int, help="kind=reduce 时的新时长（天）")
    p.set_defaults(func=cmd_scenario)

    p = sub.add_parser("plan-validate", help="校验一轮/多轮试验安排的硬约束")
    p.add_argument("--data", required=True)
    p.add_argument("--plan", nargs="+", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan_validate)

    p = sub.add_parser("plan-gaps", help="输出当前承诺/探索缺口清单")
    p.add_argument("--data", required=True)
    p.add_argument("--request", required=True)
    p.set_defaults(func=cmd_plan_gaps)

    p = sub.add_parser("plan-scenario", help="安排执行后的覆盖变化情景（不预支证据）")
    p.add_argument("--data", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--request", required=True)
    p.set_defaults(func=cmd_plan_scenario)

    p = sub.add_parser("plan-recheck", help="资源被占用后重新判断可兑现范围")
    p.add_argument("--data", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--request", required=True)
    p.add_argument("--overrides", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan_recheck)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
