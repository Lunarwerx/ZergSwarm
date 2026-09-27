"""Run the benchmark across backends and grade every answer against computed truth.

    python zswarm.py bench --backend api,cc --model deepseek-flash
    python zswarm.py bench --burst 200                              # concurrency scaling only
    python zswarm.py bench --suite judgment --backend api:flash@off,api:flash@low,api:flash@high,api:flash@max --repeats 5

The Claude arms (Sonnet/Opus/Fable sub-agents run from a chat session) are graded by
bench/external.py. A backend spec is `api|cc` optionally suffixed `:model`, optionally suffixed
`@effort` where effort is `off` (thinking disabled), `low`, `high` or `max` (DeepSeek
`reasoning_effort`). `--repeats N` runs every arm N times on fresh fixture copies and reports
per-task pass counts plus mean/min/max pass rate: one run of a stochastic model is an anecdote,
N runs are a measurement. Output: bench/out/<run>.json and bench/out/<run>.md.
Arm mechanics: bench/arm.py; markdown: bench/report.py.

A/B experiments: `--instructions FILE` (or NAME=FILE, repeatable) runs every arm twice - as it is and with
the file's text appended to the worker's system prompt - so a CLAUDE.md rule, memory or skill becomes a
measured delta instead of a belief. With --repeats >= 2 and two or more arms, bench/compare.py then gives
each arm a Welch + Holm verdict (better / worse / same / inconclusive) against its baseline, and the run
exits 1 when any row is `worse` beyond --max-regression. Tasks listed in a suite's CRITERIA also pass an
LLM-judge criterion (bench/criterion.py, model --judge) on top of their mechanical grader.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import criterion  # noqa: E402
from bench import selftest  # noqa: E402
from bench.arm import burst, grade_rows, specs  # noqa: E402
from bench.compare import MAX_REGRESSION, compare  # noqa: E402
from zswarm import benchdb as db  # noqa: E402
from bench.fixture import build  # noqa: E402
from bench.report import render_md  # noqa: E402,F401 - re-exported for external.py and regrade.py
from bench.tasks import TASKS  # noqa: E402
from zswarm import config  # noqa: E402
from zswarm.jobs import JobManager  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
EFFORTS = ("off", "low", "high", "max")


def _safe(name: str) -> str:
    """A file-system-safe arm/run name. OpenRouter model ids carry `/` and `~`, and an unsanitised id
    turned `<run>.json` into `<run>/deep.json` - the report was written, into a directory nobody looked in."""
    return re.sub(r"[^A-Za-z0-9._+-]", "_", name)


def _parse_backends(spec: str, default_model: str) -> list[tuple[str, str, str | None]]:
    """`api`, `api:flash`, `api:flash@high` (reasoning effort suffix, or `@off` = thinking disabled), `cc:flash`."""
    arms = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        b, m = part.split(":", 1) if ":" in part else (part, default_model)
        effort = None
        if "@" in m:
            m, effort = m.split("@", 1)
            if effort not in EFFORTS:
                raise SystemExit(f"bench: effort must be one of {EFFORTS}, got {effort!r}")
        arms.append((b, m, effort))
    return arms


BENCH = Path(__file__).resolve().parent
SUITE_FILES = {"judgment": ("judgment.py", "judgment_code_fixture.py", "judgment_doc_fixture.py", "criterion.py"),
               "mechanical": ("tasks.py", "fixture.py", "fixture_files.py", "words.py")}


def db_suite(name: str) -> tuple[str, str]:
    """(results-DB suite name, suite version). A fixture suite IS its source, so the version hashes the source:
    edit a task, a fixture or a grader and every earlier row stops satisfying the reuse check."""
    return f"bench.{name}", db.version_of_files(*(BENCH / f for f in SUITE_FILES[name]))


def _cached_runs(cache: dict, tasks, repeats: int) -> list[dict] | None:
    """The DB already holds a graded row for every (task, repeat) of this arm: rebuild the runs from it."""
    if not all((t.id, k) in cache for t in tasks for k in range(repeats)):
        return None
    runs = []
    for k in range(repeats):
        rows = [{"task": t.id, "pass": bool(cache[(t.id, k)]["pass"]), "blocked": False, "status": "cached", "seconds": cache[(t.id, k)].get("s") or 0.0,
                 "api_seconds": cache[(t.id, k)].get("api_s") or 0.0, "upstream": cache[(t.id, k)].get("upstream") or [], "cost_usd": 0.0,
                 "turns": cache[(t.id, k)].get("turns") or 0, "tool_calls": cache[(t.id, k)].get("tools") or 0, "reasoning_tokens": 0, "tools_ok": cache[(t.id, k)].get("tools_ok"),
                 "detail": f"reused from the results DB ({cache[(t.id, k)]['ts'][:10]})", "answer": cache[(t.id, k)].get("answer", ""), "error": ""} for t in tasks]
        runs.append({"job_id": "results-db", "passed": sum(r["pass"] for r in rows), "blocked": 0, "total": len(tasks), "wall_s": 0.0, "cost_usd": 0.0, "rows": rows})
    return runs


def suite_criteria(name: str) -> dict[str, str]:
    """task id -> judge criterion for the suite (bench/criterion.py); empty for a suite without any."""
    if name == "judgment":
        from bench import judgment

        return judgment.CRITERIA
    return {}


def instruction_arms(arms: list[tuple], entries: list[str] | None) -> list[tuple]:
    """The experiment axis: every arm as-is (baseline) plus once per instruction file, the file's text riding
    in the worker system prompt. The arm name carries the file's content hash, so editing the file is a new
    arm and the results DB never hands back numbers measured against an older version of it."""
    out = [(*arm, None) for arm in arms]
    for entry in entries or []:
        name, path = entry.split("=", 1) if "=" in entry and not Path(entry).exists() else ("", entry)
        p = Path(path)
        if not p.is_file():
            raise SystemExit(f"bench: --instructions file not found: {path}")
        text = p.read_text(encoding="utf-8")
        tag = f"{_safe(name or p.stem)}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"
        out += [(*arm, (tag, text)) for arm in arms]
    return out


def arm_name(arm: tuple) -> str:
    backend, model, effort, *rest = arm
    instr = rest[0] if rest else None
    return f"{backend}:{model}" + (f"@{effort}" if effort else "") + (f"+{instr[0]}" if instr else "")


def suite_modules(name: str):
    if name == "judgment":
        from bench import judgment

        return judgment.build, judgment.TASKS
    return build, TASKS


def _db_row(suite_db: str, version: str, name: str, model: str, effort, backend: str, a, k: int, r: dict, skipped: int = 0) -> dict:
    # `skipped` rides on every row so the leaderboard can flag an arm graded on fewer tasks (a known gap), not just report.md.
    return {"run": getattr(a, "_db_run", ""), "suite": suite_db, "v": version, "arm": name, "model": model, "effort": effort, "backend": backend,
            "item": r["task"], "rep": k, "pass": r["pass"] if not r["blocked"] else None, "status": "blocked" if r["blocked"] else "ok", "worker_status": r["status"],
            "answer": (r.get("answer") or "")[:200], "cost": r["cost_usd"], "s": r["seconds"], "api_s": r["api_seconds"], "turns": r["turns"], "tools": r["tool_calls"],
            "upstream": r["upstream"], "detail": (r.get("detail") or "")[:160], "tools_ok": r.get("tools_ok"), **({"skipped": skipped} if skipped else {})}


async def _run_repeat(m: JobManager, a, arm, tasks, truth, fixture_root: Path, run_root: Path, name: str, suite_db: str, version: str,
                      repeats: int, k: int, conc: int | None, skipped: int = 0) -> dict:
    backend, model, effort, *rest = arm
    system = rest[0][1] if rest and rest[0] else None
    tag = f" r{k + 1}" if repeats > 1 else ""
    arm_dir = run_root / _safe(name)
    run_dir = arm_dir / f"r{k + 1}" if repeats > 1 else arm_dir
    # copytree is blocking disk work, once per task: off the event loop, or it stalls every other arm.
    specs_ = await asyncio.to_thread(specs, tasks, fixture_root, run_dir, backend, model, effort, system)
    t0 = time.perf_counter()
    job = await m.run_batch(specs_, concurrency=conc or a.concurrency, label=f"bench-{_safe(name)}{tag.replace(' ', '-')}")
    wall = time.perf_counter() - t0
    rows, passed = grade_rows(tasks, job, truth, run_dir, name + tag)
    crit = suite_criteria(getattr(a, "suite", None) or "mechanical")
    if crit:
        # Recorded AFTER the judge, so a reused DB row already carries the criterion verdict.
        passed = await criterion.apply(m, tasks, rows, truth, crit, criterion.judge_model(getattr(a, "judge", None)),
                                       {t.id: job.results[t.id].answer or "" for t in tasks})
    db.record_rows([_db_row(suite_db, version, name, model, effort, backend, a, k, r, skipped) for r in rows])
    cost = sum((r["cost_usd"] or 0) for r in rows)
    nblocked = sum(1 for r in rows if r["blocked"])
    print(f"== {name}{tag}: {passed}/{len(tasks)} pass ({nblocked} blocked by limits), wall {wall:.1f}s, cost ${cost:.4f}", file=sys.stderr)
    return {"job_id": job.id, "passed": passed, "blocked": nblocked, "total": len(tasks), "wall_s": round(wall, 1), "cost_usd": round(cost, 6), "rows": rows}


def _reuse_arm(a, name: str, tasks, repeats: int, suite_db: str, version: str, per_task: dict) -> dict | None:
    """The DB already holds a graded row for every (task, repeat) of this arm: rebuild it from the DB, or None."""
    if getattr(a, "fresh", False):
        return None
    reused = _cached_runs(db.cached(suite_db, version, name, getattr(a, "max_age_days", 30.0) or 30.0), tasks, repeats)
    if not reused:
        return None
    print(f"== {name}: every task x {repeats} already graded in the results DB for this suite version; reused, not re-run (--fresh to re-measure)", file=sys.stderr)
    for run in reused:
        for r in run["rows"]:
            per_task[r["task"]] += r["pass"]
    return aggregate(reused, per_task, len(tasks), repeats, name)


def known_gaps(tasks, model: str, include: bool = False) -> tuple[list, list[dict]]:
    """(tasks to run, skipped gaps with their evidence). A known gap is reported every run, never silently dropped;
    --include-known-gaps runs them anyway, which is how a re-enable condition gets checked."""
    if include:
        return list(tasks), []
    skipped = [t for t in tasks if getattr(t, "known_gap", None) and t.known_gap.applies(model)]
    return [t for t in tasks if t not in skipped], [{"task": t.id, "gap": t.known_gap.line()} for t in skipped]


async def _run_arm(m: JobManager, a, arm: tuple, tasks, truth, fixture_root: Path, run_root: Path, repeats: int, conc: int | None = None) -> tuple[str, dict | None, list[dict]]:
    name, model = arm_name(arm), arm[1]
    tasks, gaps = known_gaps(tasks, model, getattr(a, "include_known_gaps", False))
    for g in gaps:
        print(f"== {name}: SKIP {g['task']} - known gap: {g['gap']}", file=sys.stderr)
    if not tasks:
        return name, None, gaps
    per_task = {t.id: 0 for t in tasks}
    suite_db, version = db_suite(a.suite if getattr(a, "suite", None) in SUITE_FILES else "mechanical")
    reused = _reuse_arm(a, name, tasks, repeats, suite_db, version, per_task)
    if reused is not None:
        return name, reused, gaps
    # Repeats are independent measurements of the same arm, so they run together unless --sequential.
    if getattr(a, "sequential", False):
        runs = []
        for k in range(repeats):
            runs.append(await _run_repeat(m, a, arm, tasks, truth, fixture_root, run_root, name, suite_db, version, repeats, k, conc, len(gaps)))
    else:
        coros = [_run_repeat(m, a, arm, tasks, truth, fixture_root, run_root, name, suite_db, version, repeats, k, conc, len(gaps)) for k in range(repeats)]
        runs = list(await asyncio.gather(*coros))
    for run in runs:
        for r in run["rows"]:
            per_task[r["task"]] += r["pass"]
    return name, aggregate(runs, per_task, len(tasks), repeats, name), gaps


def aggregate(runs: list[dict], per_task: dict, n_tasks: int, repeats: int, name: str = "") -> dict:
    """Arm record: the last run's fields (for single-run readers) plus the per-repeat aggregate. Shared with regrade."""
    rates = [r["passed"] / r["total"] for r in runs]
    out = {
        **runs[-1], "runs": runs,
        "pass_rate_mean": round(statistics.mean(rates), 3), "pass_rate_min": round(min(rates), 3), "pass_rate_max": round(max(rates), 3),
        "passed_total": sum(r["passed"] for r in runs), "attempts": n_tasks * repeats,
        "cost_total_usd": round(sum(r["cost_usd"] for r in runs), 6), "wall_total_s": round(sum(r["wall_s"] for r in runs), 1),
        "per_task": per_task,
    }
    rows = [r for run in runs for r in run["rows"]]
    # WHO actually served the arm. An OpenRouter model id is a pool of hosts at different quantisations,
    # so an arm's accuracy number means nothing without the list of machines that produced it.
    served: dict = {}
    for r in rows:
        for u in r.get("upstream") or []:
            served[u] = served.get(u, 0) + 1
    out["upstreams"] = dict(sorted(served.items(), key=lambda kv: -kv[1]))
    # Trace score: of the rows whose task names tool expectations AND left a trace, how many did the work right.
    measured = [r["tools_ok"] for r in rows if r.get("tools_ok") is not None]
    out["tools_ok"], out["tools_measured"] = sum(measured), len(measured)
    api_s, turns = sum(r.get("api_seconds") or 0 for r in rows), sum(r.get("turns") or 0 for r in rows)
    # Provider latency per call, not task wall clock: a swarm task spends most of its time in local tool
    # work, and under a concurrent bench that local time is contended and says nothing about the provider.
    out["api_s_per_call"] = round(api_s / turns, 2) if turns else None
    out["api_s_total"] = round(api_s, 1)
    if repeats > 1 and name:
        print(f"=== {name}: {out['passed_total']}/{out['attempts']} over {repeats} runs (mean {out['pass_rate_mean']:.0%}, min {out['pass_rate_min']:.0%}, max {out['pass_rate_max']:.0%}), cost ${out['cost_total_usd']:.4f}", file=sys.stderr)
    return out


def _run_id(a, suite: str, repeats: int) -> str:
    # Timestamp, suite, repeat count and the arm list: the directory name says what the run was without opening it.
    arms = _safe((a.backend or "burst").replace(",", "+"))[:40]
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + suite[:4] + (f"-x{repeats}" if repeats > 1 else "") + "-" + arms


def write_report(report: dict, out_path: str | None = None) -> Path:
    """Write the JSON and the markdown. Called after EVERY arm, not just at the end: a long run that is
    killed (or whose wrapper dies) used to lose every arm it had already paid for."""
    text = json.dumps(report, indent=1, ensure_ascii=False)
    (OUT / f"{report['run']}.json").write_text(text, encoding="utf-8")
    (OUT / f"{report['run']}.md").write_text(render_md(report), encoding="utf-8")
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    return OUT / f"{report['run']}.md"


def _run_arm_list(a) -> list:
    """The arms --backend names, with --baseline checked against them."""
    arms = instruction_arms(_parse_backends(a.backend, a.model), getattr(a, "instructions", None)) if a.backend else []
    baseline = getattr(a, "baseline", None)
    if baseline and arms and baseline not in {arm_name(x) for x in arms}:
        raise SystemExit(f"bench: --baseline {baseline!r} is not one of this run's arms: {', '.join(arm_name(x) for x in arms)}")
    return arms


def _run_tasks(a, suite: str) -> tuple:
    """(build_fn, the suite's tasks --only selects)."""
    # `--only a,b` and `--only a --only b` both work. A filter that matched nothing used to run zero tasks
    # and write an empty report without a word - a benchmark that measured nothing and looked finished.
    only = {x.strip() for part in (a.only or []) for x in part.split(",") if x.strip()}
    build_fn, task_list = suite_modules(suite)
    tasks = [t for t in task_list if not only or t.id in only]
    if only and not tasks:
        raise SystemExit(f"bench: --only matched no {suite} task; known: {', '.join(t.id for t in task_list)}")
    if only - {t.id for t in tasks}:
        raise SystemExit(f"bench: unknown task id(s) {sorted(only - {t.id for t in tasks})}; known: {', '.join(t.id for t in task_list)}")
    return build_fn, tasks


async def _run_one_arm(m, a, arm, tasks, truth, fixture_root, report: dict, repeats: int, conc: int) -> None:
    # The gaps come back from the run itself, so the report can never list a different skip set than was run.
    name, record, gaps = await _run_arm(m, a, arm, tasks, truth, fixture_root, OUT / report["run"], repeats, conc)
    if gaps:
        report["known_gaps"][name] = gaps
    if record is not None:
        report["arms"][name] = record
    write_report(report, getattr(a, "out", None))


def _restore_arm_order(report: dict, arms: list) -> None:
    # gather fills report["arms"] in COMPLETION order (a results-DB reuse lands first). The compare step's
    # default baseline is the first arm named in --backend, so restore the parsed order.
    order = [arm_name(x) for x in arms]
    report["arms"] = {n: report["arms"][n] for n in sorted(report["arms"], key=lambda n: order.index(n) if n in order else len(order))}


async def _run_arms(a, report: dict, suite: str, repeats: int) -> None:
    arms = _run_arm_list(a)
    build_fn, tasks = _run_tasks(a, suite)
    if not arms or not tasks:
        return
    # Before a cent is spent: a grader that passes a lazy answer or fails a right one would score every
    # arm wrong and write that into the results DB, so a suite whose graders miss their references is refused.
    selftest.refuse_on_failure(build_fn, tasks, suite)
    fixture_root = OUT / report["run"] / "fixture"
    truth = build_fn(fixture_root)
    m = JobManager()
    # Nothing should queue behind the semaphore: every task of every arm and repeat can be in flight,
    # so the run measures the providers rather than our own backlog.
    conc = a.concurrency or min(len(arms) * repeats * len(tasks), config.MAX_CONCURRENCY["api"])
    try:
        # Arms run together by default: different providers do not contend, and running them in the same
        # minutes is FAIRER than back-to-back, which straddles peak/off-peak boundaries and load drift.
        if getattr(a, "sequential", False):
            for arm in arms:
                await _run_one_arm(m, a, arm, tasks, truth, fixture_root, report, repeats, conc)
        else:
            await asyncio.gather(*(_run_one_arm(m, a, arm, tasks, truth, fixture_root, report, repeats, conc) for arm in arms))
    finally:
        await m.aclose()
        _restore_arm_order(report, arms)


def run_selftest(a, suite: str) -> int:
    """`zswarm bench --selftest`: prove the suite's graders against their references, spend nothing."""
    only = {x.strip() for part in (getattr(a, "only", None) or []) for x in part.split(",") if x.strip()}
    build_fn, task_list = suite_modules(suite)
    tasks = [t for t in task_list if not only or t.id in only]
    failures = selftest.check(build_fn, tasks)
    for f in failures:
        print(f"  FAIL {f}", file=sys.stderr)
    print(f"selftest {suite}: {len(tasks) - len({f.split(':', 1)[0] for f in failures})}/{len(tasks)} graders pass their good and bad references", file=sys.stderr)
    return 1 if failures else 0


async def run_bench(a) -> int:
    suite = getattr(a, "suite", None) or "mechanical"
    if getattr(a, "selftest", False):
        return run_selftest(a, suite)
    OUT.mkdir(parents=True, exist_ok=True)
    repeats = max(1, int(getattr(a, "repeats", 1) or 1))
    run_id = _run_id(a, suite, repeats)
    report: dict = {"run": run_id, "suite": suite, "repeats": repeats, "arms": {}, "burst": None, "known_gaps": {}}
    a._db_run, started = run_id, db.now_iso()
    if a.burst:
        report["burst"] = await burst(a.burst, a.model)
        print(json.dumps(report["burst"]), file=sys.stderr)
    await _run_arms(a, report, suite, repeats)
    if report["arms"]:
        db.record_run(run_id, *db_suite(suite if suite in SUITE_FILES else "mechanical"), list(report["arms"]), started=started, items=None)
    rc = 0
    # The compare step: a bare mean over repeats cannot tell a real change from noise; Welch + Holm can, and
    # says `inconclusive` when it cannot. It needs two arms and at least two samples per arm.
    if repeats > 1 and len(report["arms"]) > 1:
        report["compare"] = compare(report, getattr(a, "baseline", None), MAX_REGRESSION if getattr(a, "max_regression", None) is None else a.max_regression)
        rc = 1 if report["compare"]["worse"] else 0
        print("regression gate: " + (f"FAIL ({', '.join(report['compare']['worse'])})" if rc else "pass"), file=sys.stderr)
    md = write_report(report, a.out)
    print(f"report: {md}", file=sys.stderr)
    return rc
