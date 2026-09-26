"""Re-grade a finished run with the CURRENT graders (after a grader fix), from the saved answers
and the per-task directories that are still on disk. Full answers come from the job journal
(`~/.zswarm/jobs/<job>/results.jsonl`) when it still exists, because the report keeps only the
first 300 characters and a grader may need the rest.

    python bench/regrade.py bench/out/<run>.json
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.compare import compare  # noqa: E402
from bench.run import OUT, render_md, suite_modules  # noqa: E402
from zswarm import config  # noqa: E402


def _journal_answers(job_id: str) -> dict[str, dict]:
    p = config.HOME / "jobs" / job_id / "results.jsonl"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        out[r.get("id")] = r
    return out


def _regrade_run(run: dict, by_id: dict, truth: dict, run_dir: Path) -> int:
    full = _journal_answers(run.get("job_id", ""))
    passed = 0
    for r in run["rows"]:
        t = by_id[r["task"]]
        j = full.get(r["task"], {})
        answer = j.get("answer") if j.get("answer") else r.get("answer", "")
        ok, detail = t.grade({"answer": answer, "data": j.get("data", r.get("data"))}, truth, run_dir / t.id)
        # a worker that answers "FAILED: premise false ..." used the prescribed refusal prefix; the grader decides
        gradable = r.get("status") == "ok" or (r.get("status") == "error" and str(answer).startswith("FAILED:"))
        # A criterion judge's recorded fail stands (bench/criterion.py); regrading spends nothing, so it never re-asks.
        ok = ok and gradable and not r.get("blocked") and r.get("judge") is not False
        r["pass"], r["detail"] = ok, detail
        passed += ok
    run["passed"] = passed
    return passed


def regrade(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    run_id = report["run"]
    suite = report.get("suite", "mechanical")
    _build, tasks = suite_modules(suite)
    by_id = {t.id: t for t in tasks}
    truth = json.loads((OUT / run_id / "fixture.truth.json").read_text(encoding="utf-8"))
    repeats = report.get("repeats", 1)
    for arm, d in report["arms"].items():
        arm_dir = OUT / run_id / arm.replace(":", "_").replace("@", "-")
        if repeats > 1 and d.get("runs"):
            per_task = {r["task"]: 0 for r in d["runs"][0]["rows"]}
            for k, run in enumerate(d["runs"]):
                _regrade_run(run, by_id, truth, arm_dir / f"r{k + 1}")
                for r in run["rows"]:
                    per_task[r["task"]] += r["pass"]
            rates = [r["passed"] / r["total"] for r in d["runs"]]
            d.update({
                **d["runs"][-1], "runs": d["runs"],
                "pass_rate_mean": round(statistics.mean(rates), 3), "pass_rate_min": round(min(rates), 3), "pass_rate_max": round(max(rates), 3),
                "passed_total": sum(r["passed"] for r in d["runs"]), "per_task": per_task,
            })
            print(f"=== {arm}: {d['passed_total']}/{d['attempts']} over {repeats} runs (mean {d['pass_rate_mean']:.0%})", file=sys.stderr)
        else:
            passed = _regrade_run(d, by_id, truth, arm_dir)
            print(f"== {arm}: {passed}/{d['total']}", file=sys.stderr)
    if report.get("compare"):
        # The verdicts are computed from the grades: regraded passes make the saved ones stale.
        c = report["compare"]
        report["compare"] = compare(report, c.get("baseline"), c["max_regression"], c["alpha"])
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    path.with_suffix(".md").write_text(render_md(report), encoding="utf-8")
    return report


if __name__ == "__main__":
    regrade(Path(sys.argv[1]))
