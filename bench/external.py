"""Grade answers produced OUTSIDE the zswarm runner (e.g. Claude Code `Agent` sub-agents on
Sonnet/Opus run by an orchestrating session) with the SAME fixture, prompts and graders.

    python bench/external.py prepare <run_id> <arm> [<arm>...]
        builds bench/out/<run_id>/fixture + one copy per (arm, task); prints a manifest JSON
        {arm: {task: {prompt, cwd}}} the orchestrator hands to its sub-agents verbatim.

    python bench/external.py grade <run_id> <arm> <answers.json>
        answers.json = {task_id: {"answer": str, "data": obj|null, "seconds": n, "cost_usd": n|null}}
        writes bench/out/<run_id>-<arm>.json/.md in the same shape as bench/run.py, and merges the
        arm into bench/out/<run_id>.json if it exists.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import selftest  # noqa: E402
from bench.run import OUT, render_md  # noqa: E402


def _suite(run_id: str, suite: str | None):
    from bench.run import suite_modules

    name = suite or ("judgment" if "judg" in run_id else "mechanical")
    return name, suite_modules(name)


def prepare(run_id: str, arms: list[str], suite: str | None = None) -> dict:
    name, (build_fn, task_list) = _suite(run_id, suite)
    # the Claude arms spend real usage once this manifest is handed out: prove the graders first, as run.py does
    selftest.refuse_on_failure(build_fn, task_list, name)
    fixture_root = OUT / run_id / "fixture"
    build_fn(fixture_root)
    manifest: dict = {"_suite": name}
    for arm in arms:
        arm_dir = OUT / run_id / arm.replace(":", "_")
        manifest[arm] = {}
        for t in task_list:
            d = arm_dir / t.id
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(fixture_root / t.subdir if t.subdir else fixture_root, d)
            manifest[arm][t.id] = {"prompt": t.prompt, "cwd": str(d), "tools": t.tools, "schema": t.schema}
    (OUT / run_id / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def _grade_one(t, a: dict, truth: dict, task_dir: Path, arm: str) -> dict:
    rd = {"answer": a.get("answer") or "", "data": a.get("data")}
    ok, detail = t.grade(rd, truth, task_dir)
    blocked = bool(a.get("blocked"))
    ok = ok and not blocked
    row = {"task": t.id, "pass": ok, "blocked": blocked, "status": "blocked" if blocked else ("ok" if a else "missing"), "seconds": float(a.get("seconds") or 0), "cost_usd": a.get("cost_usd"), "turns": a.get("turns") or 0, "tool_calls": a.get("tool_calls") or 0, "detail": detail, "answer": rd["answer"][:300], "error": a.get("error") or ""}
    print(f"  {arm:24s} {t.id:16s} {'PASS' if ok else ('BLOCKED' if blocked else 'FAIL')} {row['seconds']:6.1f}s  {detail[:90]}", file=sys.stderr)
    return row


def _write_reports(run_id: str, arm: str, name: str, report_arm: dict) -> dict:
    report = {"run": f"{run_id}-{arm}", "suite": name, "arms": {arm: report_arm}, "burst": None}
    stem = f"{run_id}-{arm.replace(':', '_')}"
    (OUT / f"{stem}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (OUT / f"{stem}.md").write_text(render_md(report), encoding="utf-8")
    merged = OUT / f"{run_id}.json"
    if merged.exists():
        doc = json.loads(merged.read_text(encoding="utf-8"))
        doc["arms"][arm] = report_arm
        merged.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        (OUT / f"{run_id}.md").write_text(render_md(doc), encoding="utf-8")
    return report


def grade(run_id: str, arm: str, answers_path: str, suite: str | None = None) -> dict:
    name, (_build_fn, tasks) = _suite(run_id, suite)
    truth = json.loads((OUT / run_id / "fixture.truth.json").read_text(encoding="utf-8"))
    answers = json.loads(Path(answers_path).read_text(encoding="utf-8"))
    arm_dir = OUT / run_id / arm.replace(":", "_")
    rows = [_grade_one(t, answers.get(t.id) or {}, truth, arm_dir / t.id, arm) for t in tasks]
    passed = sum(r["pass"] for r in rows)
    cost = sum((r["cost_usd"] or 0.0) for r in rows)
    report_arm = {"job_id": f"external-{run_id}", "passed": passed, "blocked": sum(r["blocked"] for r in rows), "total": len(tasks), "wall_s": round(max((r["seconds"] for r in rows), default=0.0), 1), "cost_usd": round(cost, 6), "rows": rows}
    print(f"== {arm}: {passed}/{len(tasks)} pass, cost ${cost:.4f}", file=sys.stderr)
    return _write_reports(run_id, arm, name, report_arm)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "prepare":
        print(json.dumps(prepare(sys.argv[2], sys.argv[3:]), indent=1))
    elif cmd == "grade":
        grade(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        raise SystemExit(__doc__)
