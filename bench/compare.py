"""The regression gate for A/B bench runs: is an arm really better or worse than its baseline, or is it noise?

A bare mean pass rate over --repeats cannot tell a real change from a lucky run. This compares every arm
with its baseline the way a significance-tested benchmark gate does:

- Welch's t-test per task over the repeats (each repeat is one 0/1 sample), plus one overall row over the
  per-run pass rates (blocked rows excluded from both). Both are padded with one pass and one fail per arm
  (the Agresti-Caffo adjustment), so 5/5 against 0/5 is a measurable difference instead of a zero-variance division.
- Holm-Bonferroni step-down across the whole family (every task row of every comparison; the overall rows
  are their own family), so testing many tasks does not manufacture a "significant" one by chance.
- A verdict per row: `worse` only when the adjusted p < alpha AND the whole 95% CI of the pass-rate delta
  lies below -max_regression; `better` symmetrically; `same` when the whole CI sits inside
  +-max_regression; otherwise `inconclusive`, which means run more repeats, not "passed".

Baselines: an instruction-file arm (`api:flash+rules-<sha>`) is compared with the same arm without the
file (`api:flash`); every other arm with --baseline, or with the first arm of the run.

    python bench/compare.py bench/out/<run>.json [--baseline api:flash] [--max-regression 0.05]

Exit status 1 when any row is `worse`, so the compare step can gate a change.
Idea adapted from nodejs/node benchmark/compare.js (MIT); written fresh for zswarm.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ALPHA = 0.05
MAX_REGRESSION = 0.05


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularised incomplete beta (modified Lentz)."""
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for num in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + num * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + num / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 1e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_pvalue(t: float, df: float) -> float:
    """Two-sided p of Student's t with df degrees of freedom (no scipy: the repo keeps two runtime deps)."""
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def t_critical(df: float, alpha: float = ALPHA) -> float:
    """The t with two-sided p == alpha, by bisection on t_pvalue (monotone in t)."""
    lo, hi = 0.0, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if t_pvalue(mid, df) > alpha else (lo, mid)
    return (lo + hi) / 2.0


def welch(base: list[float], cand: list[float], binary: bool = False, alpha: float = ALPHA) -> dict | None:
    """Welch's t-test of cand - base: delta (raw means), p, and the (1 - alpha) CI of the delta. None when
    either side has fewer than 2 samples. `binary` pads each side with one 1 and one 0 before testing."""
    if len(base) < 2 or len(cand) < 2:
        return None
    delta = statistics.mean(cand) - statistics.mean(base)
    a, b = (list(base) + [1.0, 0.0], list(cand) + [1.0, 0.0]) if binary else (list(base), list(cand))
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    se = math.sqrt(va + vb)
    if se == 0.0:
        # Identical constant samples: no evidence of any difference. Constant but unequal: no variance to test.
        return {"delta": delta, "p": 1.0 if delta == 0 else None, "lo": delta, "hi": delta, "df": None}
    df = (va + vb) ** 2 / ((va * va / (len(a) - 1) if va else 0.0) + (vb * vb / (len(b) - 1) if vb else 0.0))
    est = statistics.mean(b) - statistics.mean(a)
    half = t_critical(df, alpha) * se
    return {"delta": delta, "p": t_pvalue(est / se, df), "lo": est - half, "hi": est + half, "df": df}


def holm(ps: list[float | None]) -> list[float | None]:
    """Holm-Bonferroni step-down adjusted p-values; None (untestable) rows stay None and are not counted."""
    idx = sorted((i for i, p in enumerate(ps) if p is not None), key=lambda i: ps[i])
    out: list[float | None] = [None] * len(ps)
    running = 0.0
    for rank, i in enumerate(idx):
        running = max(running, min(1.0, (len(idx) - rank) * ps[i]))
        out[i] = running
    return out


def verdict(row: dict, max_regression: float = MAX_REGRESSION, alpha: float = ALPHA) -> str:
    if row.get("p_adj") is None:
        return "inconclusive"
    significant = row["p_adj"] < alpha
    if significant and row["hi"] < -max_regression:
        return "worse"
    if significant and row["lo"] > max_regression:
        return "better"
    if row["lo"] >= -max_regression and row["hi"] <= max_regression:
        return "same"
    return "inconclusive"


def baseline_of(arm: str, arms: list[str], baseline: str | None = None) -> str | None:
    """An instruction-file arm's baseline is the same arm without the file; anything else uses --baseline or
    the first arm. An arm is never its own baseline."""
    base = arm.split("+", 1)[0] if "+" in arm else (baseline or arms[0])
    return base if base in arms and base != arm else None


def _task_samples(record: dict, task: str) -> list[float]:
    return [1.0 if r["pass"] else 0.0 for run in record.get("runs", []) for r in run["rows"] if r["task"] == task and not r.get("blocked")]


def _run_rates(record: dict) -> list[float]:
    """Per-run pass rate over the rows that were actually measured: a blocked row (rate limit, judge error) is a
    harness failure, not the arm's, so it leaves the denominator exactly as the per-task rows drop it."""
    return [run["passed"] / (run["total"] - (run.get("blocked") or 0)) for run in record.get("runs", []) if run.get("total") and run["total"] - (run.get("blocked") or 0) > 0]


def compare(report: dict, baseline: str | None = None, max_regression: float = MAX_REGRESSION, alpha: float = ALPHA) -> dict:
    """Every non-baseline arm against its baseline: an overall row and one row per task, Holm-adjusted, with verdicts."""
    arms = report.get("arms") or {}
    names = list(arms)
    if baseline and baseline not in arms:
        raise SystemExit(f"bench compare: --baseline {baseline!r} is not an arm of this run; arms: {', '.join(names)}")
    pairs = []
    for arm in names:
        base = baseline_of(arm, names, baseline)
        if base is None:
            continue
        b, c = arms[base], arms[arm]
        # The overall row is padded too (rates live in 0..1): every run at 1.0 against every run at 0.0 is the
        # clearest regression there is, not a zero-variance "inconclusive".
        overall = {"task": "(all tasks)", **(welch(_run_rates(b), _run_rates(c), binary=True, alpha=alpha) or {"delta": None, "p": None, "lo": None, "hi": None})}
        tasks = [{"task": t, **(welch(_task_samples(b, t), _task_samples(c, t), binary=True, alpha=alpha) or {"delta": None, "p": None, "lo": None, "hi": None})}
                 for t in (c.get("per_task") or {}) if t in (b.get("per_task") or {})]
        pairs.append({"arm": arm, "baseline": base, "overall": overall, "tasks": tasks})
    # Two families: the overall rows (one per arm) and every task row of every arm, each Holm-adjusted as a whole.
    for family in ([p["overall"] for p in pairs], [r for p in pairs for r in p["tasks"]]):
        for row, adj in zip(family, holm([r["p"] for r in family])):
            row["p_adj"] = adj
            row["verdict"] = verdict(row, max_regression, alpha)
    worse = [f"{p['arm']} {r['task']}" for p in pairs for r in (p["overall"], *p["tasks"]) if r["verdict"] == "worse"]
    return {"alpha": alpha, "max_regression": max_regression, "baseline": baseline, "pairs": pairs, "worse": worse}


def _fmt(x: float | None, pct: bool = True) -> str:
    if x is None:
        return "-"
    return f"{x:+.0%}" if pct else (f"{x:.3f}" if x >= 0.001 else f"{x:.1e}")


def render_compare(cmp: dict) -> list[str]:
    lines = [f"## A/B verdicts (Welch per task, Holm-adjusted, alpha {cmp['alpha']}, max regression {cmp['max_regression']:.0%})", "",
             "`worse`/`better` need adjusted p < alpha AND the whole 95% CI beyond the max regression; `same` means the whole CI is inside it; "
             "`inconclusive` means run more repeats, not pass.", "",
             "| arm | baseline | task | delta | 95% CI | p (Holm) | verdict |", "|---|---|---|---|---|---|---|"]
    for p in cmp["pairs"]:
        for r in (p["overall"], *p["tasks"]):
            ci = f"{_fmt(r['lo'])} .. {_fmt(r['hi'])}" if r.get("lo") is not None else "-"
            lines.append(f"| {p['arm']} | {p['baseline']} | {r['task']} | {_fmt(r['delta'])} | {ci} | {_fmt(r['p_adj'], pct=False)} | {r['verdict']} |")
    lines += ["", f"**Regression gate: FAIL** - worse: {', '.join(cmp['worse'])}" if cmp["worse"] else "**Regression gate: pass** (no row is `worse`)", ""]
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="A/B verdicts for a finished bench run (Welch + Holm + max-regression CI).")
    ap.add_argument("report", help="bench/out/<run>.json")
    ap.add_argument("--baseline", help="arm every non-instruction arm is compared with (default: the first arm)")
    ap.add_argument("--max-regression", type=float, default=MAX_REGRESSION, help="tolerated pass-rate drop, 0..1")
    a = ap.parse_args(argv)
    cmp = compare(json.loads(Path(a.report).read_text(encoding="utf-8")), a.baseline, a.max_regression)
    print("\n".join(render_compare(cmp)))
    return 1 if cmp["worse"] else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
