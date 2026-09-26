"""Offline: the bench A/B rig - instruction-file arms, the criterion judge, and the Welch + Holm regression gate."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import compare as cmp  # noqa: E402
from bench import criterion  # noqa: E402
from bench.arm import specs  # noqa: E402
from bench.run import arm_name, instruction_arms  # noqa: E402
from bench.tasks import BenchTask  # noqa: E402


def _arm(per_task: dict[str, list[bool]]) -> dict:
    """An arm record shaped like run.aggregate's: one run per repeat, one row per task."""
    n = len(next(iter(per_task.values())))
    runs = []
    for k in range(n):
        rows = [{"task": t, "pass": v[k], "blocked": False, "status": "ok", "detail": "", "error": ""} for t, v in per_task.items()]
        runs.append({"passed": sum(r["pass"] for r in rows), "total": len(rows), "rows": rows})
    rates = [r["passed"] / r["total"] for r in runs]
    return {"runs": runs, "per_task": {t: sum(v) for t, v in per_task.items()}, "passed_total": sum(r["passed"] for r in runs), "attempts": n * len(per_task),
            "pass_rate_mean": sum(rates) / n, "pass_rate_min": min(rates), "pass_rate_max": max(rates), "cost_total_usd": 0.0}


def test_student_t_and_holm_match_reference_values():
    # Reference: two-sided p of t=2.0 at df=10 is 0.0734; the 95% critical t at df=10 is 2.228.
    assert cmp.t_pvalue(2.0, 10) == pytest.approx(0.07339, abs=1e-4)
    assert cmp.t_critical(10) == pytest.approx(2.2281, abs=1e-3)
    assert cmp.t_pvalue(0.0, 5) == pytest.approx(1.0)
    # Holm step-down: 0.01*3, then max(0.03*2, prior), then max(0.04*1, prior); None is outside the family.
    assert cmp.holm([0.01, 0.04, 0.03, None]) == pytest.approx([0.03, 0.06, 0.06, None])


def test_gate_fails_only_on_a_real_regression_and_calls_noise_inconclusive():
    ten = 10
    base = _arm({"broken": [True] * ten, "noisy": [True, False] * 5, "steady": [True] * ten})
    cand = _arm({"broken": [False] * ten, "noisy": [False, True, True, False, False, True, False, False, True, False], "steady": [True] * ten})
    out = cmp.compare({"arms": {"api:a": base, "api:b": cand}}, max_regression=0.05)
    rows = {r["task"]: r for r in out["pairs"][0]["tasks"]}
    assert rows["broken"]["verdict"] == "worse" and rows["broken"]["delta"] == -1.0
    assert rows["noisy"]["verdict"] == "inconclusive"  # 5/10 vs 4/10 is noise, not a regression
    assert rows["steady"]["verdict"] != "worse"
    assert "api:b broken" in out["worse"] and not {"api:b noisy", "api:b steady"} & set(out["worse"])
    md = "\n".join(cmp.render_compare(out))
    assert "| api:b | api:a | broken | -100% |" in md and "Regression gate: FAIL" in md
    # One repeat is an anecdote: nothing is testable, so nothing can fail the gate.
    one = cmp.compare({"arms": {"api:a": _arm({"x": [True]}), "api:b": _arm({"x": [False]})}})
    assert one["worse"] == [] and one["pairs"][0]["tasks"][0]["verdict"] == "inconclusive"


def test_instruction_arm_is_compared_with_itself_without_the_file(tmp_path):
    rules = tmp_path / "RULES.md"
    rules.write_text("Always run the tests.", encoding="utf-8")
    arms = instruction_arms([("api", "flash", None), ("api", "pro", "off")], [f"rules={rules}"])
    names = [arm_name(x) for x in arms]
    assert names[:2] == ["api:flash", "api:pro@off"] and names[2].startswith("api:flash+rules-") and names[3].startswith("api:pro@off+rules-")
    assert arms[2][3][1] == "Always run the tests."
    rules.write_text("Never run the tests.", encoding="utf-8")
    changed = arm_name(instruction_arms([("api", "flash", None)], [f"rules={rules}"])[1])
    assert changed.startswith("api:flash+rules-") and changed != names[2]  # an edited file is a new arm, never a DB reuse
    assert cmp.baseline_of(names[3], names) == "api:pro@off" and cmp.baseline_of("api:pro@off", names) == "api:flash"
    with pytest.raises(SystemExit, match="not found"):
        instruction_arms([("api", "flash", None)], [str(tmp_path / "missing.md")])
    # The file's text reaches the worker as its system prompt; the baseline arm carries none.
    fixture = tmp_path / "fx"
    fixture.mkdir()
    t = BenchTask("t1", "p", "read", lambda *a: (True, ""))
    assert specs([t], fixture, tmp_path / "with", "api", "flash", None, "Always run the tests.")[0].system == "Always run the tests."
    assert specs([t], fixture, tmp_path / "without", "api", "flash", None)[0].system is None


def test_run_bench_writes_verdicts_and_exits_1_on_a_regression(tmp_path, monkeypatch):
    import bench.run as run

    monkeypatch.setattr(run, "OUT", tmp_path)
    monkeypatch.setattr(run.db, "record_run", lambda *a, **k: None)
    recs = {"api:a": _arm({"count_defs": [True] * 8}), "api:b": _arm({"count_defs": [False] * 8})}

    async def fake_arm(m, a, arm, tasks, truth, fixture_root, run_root, repeats, conc=None):
        return arm_name(arm), recs[arm_name(arm)], []  # _run_arm also returns the known gaps it skipped (trace-evals)

    monkeypatch.setattr(run, "_run_arm", fake_arm)
    monkeypatch.setattr(run, "suite_modules", lambda s: (lambda root: {}, [BenchTask("count_defs", "p", "read", lambda *a: (True, ""))]))
    monkeypatch.setattr(run, "JobManager", lambda: SimpleNamespace(aclose=lambda: asyncio.sleep(0)))
    # The suite here is a stand-in grader with no good/bad references: the grader self-test (bench-graders,
    # tests/test_bench.py) would refuse it; this test is about verdicts, not about proving graders.
    monkeypatch.setattr(run.selftest, "refuse_on_failure", lambda *a, **k: None)
    a = SimpleNamespace(backend="api:a,api:b", model="x", only=None, concurrency=None, sequential=False, out=None, burst=0, suite="mechanical",
                        repeats=8, instructions=None, baseline=None, max_regression=0.05)
    assert asyncio.run(run.run_bench(a)) == 1
    md = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "A/B verdicts" in md and "| api:b | api:a | count_defs |" in md and "worse" in md
    a.baseline = "api:nope"
    with pytest.raises(SystemExit, match="not one of this run's arms"):
        asyncio.run(run.run_bench(a))


def test_default_baseline_is_the_first_named_arm_even_when_it_finishes_last(tmp_path, monkeypatch):
    # Regression: arms run under gather and report["arms"] filled in completion order, so a fast second arm
    # became the default baseline and the gate fired the wrong way round.
    import bench.run as run

    monkeypatch.setattr(run, "OUT", tmp_path)
    monkeypatch.setattr(run.db, "record_run", lambda *a, **k: None)
    recs = {"api:a": _arm({"count_defs": [True] * 8}), "api:b": _arm({"count_defs": [False] * 8})}

    async def fake_arm(m, a, arm, tasks, truth, fixture_root, run_root, repeats, conc=None):
        if arm_name(arm) == "api:a":
            for _ in range(5):
                await asyncio.sleep(0)  # the first-named arm finishes after the second
        return arm_name(arm), recs[arm_name(arm)], []  # _run_arm also returns the known gaps it skipped (trace-evals)

    monkeypatch.setattr(run, "_run_arm", fake_arm)
    monkeypatch.setattr(run, "suite_modules", lambda s: (lambda root: {}, [BenchTask("count_defs", "p", "read", lambda *a: (True, ""))]))
    monkeypatch.setattr(run, "JobManager", lambda: SimpleNamespace(aclose=lambda: asyncio.sleep(0)))
    # The suite here is a stand-in grader with no good/bad references: the grader self-test (bench-graders,
    # tests/test_bench.py) would refuse it; this test is about verdicts, not about proving graders.
    monkeypatch.setattr(run.selftest, "refuse_on_failure", lambda *a, **k: None)
    monkeypatch.setattr(run, "compare", lambda report, *a, **k: seen.append(list(report["arms"])) or cmp.compare(report, *a, **k))
    seen: list[list[str]] = []
    a = SimpleNamespace(backend="api:a,api:b", model="x", only=None, concurrency=None, sequential=False, out=None, burst=0, suite="mechanical",
                        repeats=8, instructions=None, baseline=None, max_regression=0.05)
    assert asyncio.run(run.run_bench(a)) == 1 and seen == [["api:a", "api:b"]]


def test_overall_row_ignores_blocked_rows_and_tests_a_constant_split():
    # A throttled run is a harness failure: 2 passed of 4 with 2 blocked is 100% of what was measured.
    assert cmp._run_rates({"runs": [{"passed": 2, "total": 4, "blocked": 2}, {"passed": 0, "total": 3, "blocked": 3}]}) == [1.0]
    # Every run 1.0 against every run 0.0 is a certain regression, not a zero-variance "inconclusive".
    out = cmp.compare({"arms": {"api:a": _arm({"x": [True] * 6}), "api:b": _arm({"x": [False] * 6})}})
    assert out["pairs"][0]["overall"]["verdict"] == "worse" and "api:b (all tasks)" in out["worse"]


def test_criterion_judge_is_anded_in_and_never_passes_on_its_own_failure():
    calls = []

    class FakeManager:
        def __init__(self, replies):
            self.replies = replies

        async def ask_routed(self, prompt, model, **kw):
            calls.append((prompt, model))
            return self.replies.pop(0)

    task = BenchTask("summ", "Summarise.", "read", lambda *a: (True, ""))
    truth = {"summ": {"retracted": ["Hetzner"], "current": ["Fly.io"]}}
    crit = {"summ": "Never present {retracted} as current."}
    rows = [{"task": "summ", "pass": True, "blocked": False, "detail": "ok"}]
    reject = SimpleNamespace(status="ok", data={"pass": False, "reason": "says hosting is Hetzner"}, cost_usd=0.001, error=None, answer="")
    assert asyncio.run(criterion.apply(FakeManager([reject]), [task], rows, truth, crit, "judge-model", {"summ": "Hosting: Hetzner"})) == 0
    assert rows[0]["judge"] is False and "Hetzner" in rows[0]["detail"]
    assert "Never present ['Hetzner'] as current." in calls[0][0] and "Hosting: Hetzner" in calls[0][0] and calls[0][1] == "judge-model"
    # A judge that errors blocks the row rather than passing it.
    rows = [{"task": "summ", "pass": True, "blocked": False, "detail": "ok"}]
    down = SimpleNamespace(status="error", data=None, cost_usd=0.0, error="429", answer="")
    assert asyncio.run(criterion.apply(FakeManager([down]), [task], rows, truth, crit, "j")) == 0 and rows[0]["blocked"]
    # A row the mechanical grader already failed never costs a judge call.
    calls.clear()
    rows = [{"task": "summ", "pass": False, "blocked": False, "detail": "wrong"}]
    assert asyncio.run(criterion.apply(FakeManager([]), [task], rows, truth, crit, "j")) == 0 and calls == []
