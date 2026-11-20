"""人可读文本报告；数值口径与 JSON 输出完全一致，只做排版。"""
from __future__ import annotations

from .evidence import STATUS_LABELS


def _pct(x):
    return "不可识别" if x is None else f"{x * 100:.2f}%"


def render_run(run):
    lines = []
    lines.append("=" * 72)
    lines.append("沿海储能柜证据分析运行（内部工程决策，不构成法定认证）")
    lines.append("=" * 72)
    fp = run["data_fingerprint"]
    lines.append(f"资料时点 as_of：{fp['as_of']}")
    lines.append(f"自助次数：{run['config']['replicates']}，种子：{run['config']['seed']}，"
                 f"最低样本量：{run['config']['min_subjects']}")
    lines.append("")

    lines.append("一、试验室证据（暴露小时；无加速年限换算）")
    for row in run["lab_diagnostic"]["rows"]:
        profile = "/".join(f"{salt}g·L⁻¹,{rh}%" for salt, rh in row["severity_profile"])
        lines.append(f"  {row['design_id']}（实际剖面：{profile}）")
        for p in row["protocols"]:
            est = p["risk_at_common_hour"]
            point = "不可识别" if est["point"] is None else _pct(est["point"])
            upper = "不可识别" if est["upper"] is None else _pct(est["upper"])
            stops = ",".join(f"{k}:{v}" for k, v in p["stop_breakdown"].items())
            lines.append(
                f"    {p['protocol']} n={p['n_subjects']} 失效={p['n_events']} "
                f"停试({stops}) 共同{run['lab_diagnostic']['common_hour']}h "
                f"风险点估计={point} 上分={upper} 朴素通过率={p['crude_pass_fraction']:.3f}"
            )
    lines.append(f"  防偏诊断：{run['lab_diagnostic']['bias_warning']}")
    lines.append("")

    lines.append("二、现场证据单元（服役日；密封换型后另起经历）")
    for cell in run["field_cells"]:
        lines.append(
            f"  {cell['design_id']} / {cell['cell_key']}: n={cell['n_subjects']} "
            f"失效={cell['n_events']} 最大随访={cell['max_followup']:.0f}日 "
            f"删失={cell['censor_counts']}"
        )
    lines.append("")

    lines.append("三、客户用途承诺评估")
    for req in run["request_evaluations"]:
        lines.append(f"  用途 {req['request_id']}（需求 {req['requested_cabinets']} 柜，"
                     f"答复期 {req.get('decision_by', '-')}）")
        for d in req["designs"]:
            lines.append(
                f"    {d['design_id']}: {d['overall_label']} "
                f"加权风险点估计={_pct(d['weighted_point_risk'])} "
                f"上分={_pct(d['weighted_upper_risk'])} "
                f"可接柜数={d['servable_cabinets']}"
            )
            for e in d["per_environment"]:
                lines.append(
                    f"      - {e['environment']}(权重{e['share']:.2f}): "
                    f"{e['status_label']} [{e.get('reason')}] "
                    f"n={e['n_subjects']} 失效={e['n_events']} "
                    f"点估计={_pct(e['point'])} 上分={_pct(e['upper'])} "
                    f"随访上限={e['max_followup']}日"
                )
            if d["uncovered_environments"]:
                lines.append(f"      未覆盖环境：{', '.join(d['uncovered_environments'])}"
                             "（总体不得判为支持）")
    lines.append("")

    lines.append("四、同环境设计比较")
    for cmp_ in run["field_comparisons"]:
        lines.append(f"  环境 {cmp_['environment']} 可比对数={cmp_['comparable_pairs']}，"
                     f"风险排序={cmp_['ranked'] or '无'}")
        for r in cmp_["rows"]:
            if r["comparable"]:
                lines.append(
                    f"    {r['design_id']}: {STATUS_LABELS[r['status']]} "
                    f"点估计={_pct(r['point'])} 上分={_pct(r['upper'])}"
                )
            else:
                lines.append(f"    {r['design_id']}: 不可比（{r['reason']}）")
    lines.append("=" * 72)
    return "\n".join(lines)


def render_planning(report):
    lines = ["=" * 72, f"试验安排校验：{report.get('plan_id', '-')}", "=" * 72]
    lines.append(f"结论：{'可执行' if report['feasible'] else '不可执行'}")
    if report["errors"]:
        lines.append("硬约束冲突：")
        lines.extend(f"  ✗ {e}" for e in report["errors"])
    if report["warnings"]:
        lines.append("提示：")
        lines.extend(f"  ! {w}" for w in report["warnings"])
    lines.append(f"安排试样数：{report['n_placements']}，"
                 f"破坏消耗：{', '.join(report['specimens_consumed']) or '无'}")
    lines.append(f"舱位占用：{report['slot_occupancy']}")
    lines.append(f"费用：{report['total_cost']:.0f} / 预算 {report['budget']:.0f}，"
                 f"剩余 {report['budget_remaining']:.0f}")
    if "occupancy_effect" in report:
        lines.append(report["occupancy_effect"])
    return "\n".join(lines)
