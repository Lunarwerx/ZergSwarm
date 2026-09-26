"""Markdown rendering of a bench report: one shape for a single run, another for repeats."""
from __future__ import annotations

from bench.compare import render_compare


def _failures(rows: list[dict], prefix: str = "") -> list[str]:
    fails = [f"- {prefix}{r['task']} [{r['status']}]: {r['detail']}" + (f" · error: {r['error']}" if r["error"] else "") for r in rows if not r["pass"]]
    # A right answer reached the wrong way is its own finding: listed even when the answer grade passed.
    return fails + [f"- {prefix}{r['task']} [tools]: {r.get('tools_detail', '')}" for r in rows if r.get("tools_ok") is False]


def _tools(d: dict) -> str:
    return f"{d['tools_ok']}/{d['tools_measured']}" if d.get("tools_measured") else "-"


def _gaps(report: dict) -> list[str]:
    """Known gaps are printed every run, with their evidence, so an accepted failure never goes quiet."""
    lines = [f"- {arm} skipped {g['task']}: {g['gap']}" for arm, gaps in (report.get("known_gaps") or {}).items() for g in gaps]
    return ["## Known gaps (skipped; `--include-known-gaps` runs them)", "", *lines, ""] if lines else []


def _md_repeats(arms: dict, repeats: int) -> list[str]:
    lines = [f"## Accuracy over {repeats} runs per arm (same prompts, fresh fixture per run, mechanical graders)", "",
             "| arm | passed / attempts | mean | min | max | tools ok | cost $ (all runs) | api s/call | served by |",
             "|---|---|---|---|---|---|---|---|---|"]
    for arm, d in arms.items():
        served = ", ".join(f"{k} x{v}" for k, v in (d.get("upstreams") or {}).items()) or "direct"
        lines.append(f"| {arm} | {d['passed_total']}/{d['attempts']} | {d['pass_rate_mean']:.0%} | {d['pass_rate_min']:.0%} | "
                     f"{d['pass_rate_max']:.0%} | {_tools(d)} | {d['cost_total_usd']:.4f} | {d.get('api_s_per_call') or '-'} | {served} |")
    # Arms can run different task sets (a known gap skips a task for one model), so the grid is the union.
    task_ids = list(dict.fromkeys(tid for d in arms.values() for tid in d["per_task"]))
    lines += ["", f"Per task: passes out of {repeats} runs.", "", "| task | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    lines += [f"| {tid} | " + " | ".join(f"{d['per_task'][tid]}/{repeats}" if tid in d["per_task"] else "skip" for d in arms.values()) + " |" for tid in task_ids]
    lines.append("")
    for arm, d in arms.items():
        fails = [f for k, run in enumerate(d["runs"]) for f in _failures(run["rows"], f"r{k + 1} ")]
        if fails:
            lines += [f"### {arm} failures", *fails, ""]
    return lines


def _md_single(arms: dict) -> list[str]:
    lines = ["## Accuracy (same prompts, same fixture, mechanical graders)", "", "| arm | pass | tools ok | blocked by limits | wall s | cost $ |", "|---|---|---|---|---|---|"]
    lines += [f"| {arm} | {d['passed']}/{d['total']} | {_tools(d)} | {d.get('blocked', 0)} | {d['wall_s']} | {d['cost_usd']:.4f} |" for arm, d in arms.items()]
    by_arm = {arm: {r["task"]: r for r in d["rows"]} for arm, d in arms.items()}
    task_ids = list(dict.fromkeys(tid for rows in by_arm.values() for tid in rows))
    lines += ["", "| task | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for tid in task_ids:
        cells = [("PASS" if r["pass"] else "FAIL") + ("" if r.get("tools_ok") is not False else " tools-bad") + f" {r['seconds']:.0f}s ${(r['cost_usd'] or 0):.4f}"
                 if (r := by_arm[arm].get(tid)) else "skip" for arm in arms]
        lines.append(f"| {tid} | " + " | ".join(cells) + " |")
    lines.append("")
    for arm, d in arms.items():
        fails = _failures(d["rows"])
        if fails:
            lines += [f"### {arm} failures", *fails, ""]
    return lines


def render_md(report: dict) -> str:
    repeats = report.get("repeats", 1)
    lines = [f"# zswarm bench {report['run']} (suite: {report.get('suite', 'mechanical')}, repeats: {repeats})", ""]
    if report.get("burst"):
        b = report["burst"]
        lines += [f"## Burst: {b['n']} concurrent one-shot calls on {b['model']}", "",
                  f"- wall {b['wall_s']}s · p50 {b['p50_s']}s · p95 {b['p95_s']}s · max {b['max_s']}s · codes {b['codes']} · retries {b['retries']}",
                  f"- cache hit tokens {b['cache_hit_tokens']} · miss {b['cache_miss_tokens']} · cost ${b['cost_usd']}", ""]
    if report["arms"]:
        # Repeats get the aggregate table; a single run keeps the per-task PASS/FAIL grid older readers expect.
        lines += _md_repeats(report["arms"], repeats) if repeats > 1 else _md_single(report["arms"])
    if report.get("compare"):
        lines += render_compare(report["compare"])
    lines += _gaps(report)
    return "\n".join(lines)
