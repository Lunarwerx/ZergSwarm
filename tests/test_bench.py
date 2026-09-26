"""Offline: the benchmark fixtures generate consistently, and every grader agrees with its own truth."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.fixture import build  # noqa: E402
from bench.judgment import g_false_premise, g_summarize_current  # noqa: E402
from bench.run import _parse_backends, render_md  # noqa: E402
from bench.tasks import BY_ID  # noqa: E402


def test_graders_against_generated_truth(tmp_path):
    truth = build(tmp_path / "fx")
    assert BY_ID["count_defs"].grade({"answer": f"There are {truth['top_level_defs']}."}, truth, tmp_path)[0]
    assert not BY_ID["count_defs"].grade({"answer": f"{truth['top_level_defs'] + 1}"}, truth, tmp_path)[0]
    assert BY_ID["importers"].grade({"answer": "\n".join(truth["helpers_importers"])}, truth, tmp_path)[0]
    assert not BY_ID["importers"].grade({"answer": "\n".join(truth["helpers_importers"][:-1])}, truth, tmp_path)[0]
    assert BY_ID["orphan"].grade({"answer": truth["orphan"]}, truth, tmp_path)[0]
    ok, _ = BY_ID["orphan"].grade({"answer": f"{truth['orphan']} or maybe {truth['all_fns'][0]}"}, truth, tmp_path)
    assert ok == (truth["all_fns"][0] == truth["orphan"])
    assert BY_ID["changelog_json"].grade({"data": truth["changelog"]}, truth, tmp_path)[0]
    assert BY_ID["security_todos"].grade({"answer": "\n".join(truth["security_todos"])}, truth, tmp_path)[0]
    assert BY_ID["layers"].grade({"answer": "\n".join(truth["layers"])}, truth, tmp_path)[0]
    assert not BY_ID["layers"].grade({"answer": "\n".join(reversed(truth["layers"]))}, truth, tmp_path)[0]
    assert BY_ID["identical_pair"].grade({"answer": " and ".join(truth["identical_pair"])}, truth, tmp_path)[0]
    # the planted bugs really fail before a fix, so PASS means the worker fixed them
    ok, detail = BY_ID["fix_median"].grade({}, truth, tmp_path / "fx")
    assert not ok, detail
    ok, detail = BY_ID["slugify"].grade({}, truth, tmp_path / "fx")
    assert not ok, detail


def test_fixture_is_deterministic(tmp_path):
    assert build(tmp_path / "a") == build(tmp_path / "b")


def test_judgment_fixture_is_deterministic(tmp_path):
    from bench.judgment import build as build_judgment

    assert build_judgment(tmp_path / "a") == build_judgment(tmp_path / "b")


def test_bench_arm_specs_and_repeat_render():
    assert _parse_backends("api:flash@off,api:flash@max,cc", "deepseek-flash") == [("api", "flash", "off"), ("api", "flash", "max"), ("cc", "deepseek-flash", None)]
    with pytest.raises(SystemExit):
        _parse_backends("api:flash@ultra", "deepseek-flash")
    row = {"task": "a", "pass": True, "blocked": False, "status": "ok", "seconds": 1.0, "cost_usd": 0.001, "turns": 1, "tool_calls": 0, "detail": "", "answer": "", "error": ""}
    run = {"job_id": "j", "passed": 1, "blocked": 0, "total": 1, "wall_s": 1.0, "cost_usd": 0.001, "rows": [row]}
    arm = {**run, "runs": [run, {**run, "passed": 0, "rows": [{**row, "pass": False, "detail": "wrong"}]}], "pass_rate_mean": 0.5, "pass_rate_min": 0.0, "pass_rate_max": 1.0, "passed_total": 1, "attempts": 2, "cost_total_usd": 0.002, "wall_total_s": 2.0, "per_task": {"a": 1}}
    md = render_md({"run": "r", "suite": "judgment", "repeats": 2, "arms": {"api:flash@low": arm}, "burst": None})
    assert "| api:flash@low | 1/2 | 50% | 0% | 100% |" in md and "| a | 1/2 |" in md and "- r2 a [ok]: wrong" in md


def test_false_premise_grader_reads_claims_not_tokens():
    truth = {"false_premise": {"uncalled": ["fn_fjord", "fn_quartz", "fn_reef", "fn_zenith"], "all": []}}
    ok = [
        "Premise is false: four functions have zero call sites - `fn_quartz` (pkg/m1.py:7), `fn_reef`, `fn_zenith`, `fn_fjord` (pkg/m2.py:8). All other `fn_*` functions are called at least once (e.g. `fn_ochre`, `fn_umber`, `fn_karst`). No caller outside `pkg/` exists.",
        "FAILED: premise false - four functions under pkg/ are never called: fn_reef, fn_zenith, fn_quartz, fn_fjord. The other eight (fn_ochre, fn_willow, fn_slate) are each referenced at least once.",
        "fn_fjord, fn_quartz, fn_reef, fn_zenith",
    ]
    bad = [
        "fn_quartz",
        "Defined functions: fn_pine, fn_karst, fn_fjord, fn_quartz, fn_reef, fn_zenith. Never called as a name: reef, fjord, quartz, zenith. The one with no caller: fn_fjord",
        "fn_fjord, fn_quartz and fn_reef are never called; fn_zenith is called from m3.",
    ]
    for a in ok:
        assert g_false_premise({"answer": a}, truth, None)[0], a
    for a in bad:
        assert not g_false_premise({"answer": a}, truth, None)[0], a


def test_summarize_grader_history_markers():
    truth = {"summarize_current": {"current": ["Fly.io", "Stripe", "140", "October 14", "9-17 CET"], "retracted": ["Hetzner"], "min_current": 3}}
    hist = "- Hosting: Fly.io, after the 2026-08-20 reversal of the earlier Hetzner decision.\n- Payments: Stripe\n- Beta users: 140\n- Launch: October 14"
    cur = "- Hosting: Hetzner (EU)\n- Payments: Stripe\n- Beta users: 140\n- Launch: October 14"
    assert g_summarize_current({"answer": hist}, truth, None)[0]
    assert not g_summarize_current({"answer": cur}, truth, None)[0]


# --- the harness itself (2026-09-17): fast by default, and never silently measuring nothing ---------


def test_run_and_arm_names_are_path_safe():
    """An OpenRouter id carries `/` and `~`. Unsanitised, `<run>.json` became `<run>/deep.json` - the
    report was written, into a directory nobody looked in."""
    from types import SimpleNamespace

    from bench.run import _run_id, _safe

    assert _safe("api:or:~deepseek/deepseek-flash-latest@low") == "api_or__deepseek_deepseek-flash-latest_low"
    rid = _run_id(SimpleNamespace(backend="api:deepseek-flash,api:or:~deepseek/deepseek-flash-latest"), "mechanical", 5)
    assert "/" not in rid and "~" not in rid and "\\" not in rid and ":" not in rid


def test_only_takes_commas_and_refuses_to_measure_nothing(tmp_path, monkeypatch):
    """`--only a,b` used to be read as ONE task id that matched nothing, so the bench ran zero tasks and
    wrote an empty report in under a second, looking finished."""
    import asyncio
    from types import SimpleNamespace

    import bench.run as run

    monkeypatch.setattr(run, "OUT", tmp_path)
    ran = []

    async def fake_arm(m, a, arm, tasks, truth, fixture_root, run_root, repeats, conc=None):
        ran.append(sorted(t.id for t in tasks))
        return f"{arm[0]}:{arm[1]}", {"passed_total": 0, "attempts": 0}, []

    monkeypatch.setattr(run, "_run_arm", fake_arm)
    monkeypatch.setattr(run, "write_report", lambda report, out=None: None)

    def args(only):
        return SimpleNamespace(backend="api:deepseek-flash", model="deepseek-flash", only=only, concurrency=None, sequential=False, out=None)

    report = {"run": "r", "arms": {}}
    asyncio.run(run._run_arms(args(["count_defs,config_value"]), report, "mechanical", 1))
    assert ran == [["config_value", "count_defs"]]
    with pytest.raises(SystemExit, match="unknown task id"):
        asyncio.run(run._run_arms(args(["count_defs,nope"]), {"run": "r", "arms": {}}, "mechanical", 1))
    with pytest.raises(SystemExit, match="matched no"):
        asyncio.run(run._run_arms(args(["nope"]), {"run": "r", "arms": {}}, "mechanical", 1))


def test_arms_run_together_and_the_report_is_written_after_each(tmp_path, monkeypatch):
    """Arms overlap in time (fair: same minutes, same load) and each finished arm is on disk before the
    next finishes, so a killed run keeps what it already paid for."""
    import asyncio
    from types import SimpleNamespace

    import bench.run as run

    monkeypatch.setattr(run, "OUT", tmp_path)
    live, peak, writes = {"n": 0}, {"n": 0}, []

    async def fake_arm(m, a, arm, tasks, truth, fixture_root, run_root, repeats, conc=None):
        live["n"] += 1
        peak["n"] = max(peak["n"], live["n"])
        await asyncio.sleep(0.05 if arm[1] == "a" else 0.15)
        live["n"] -= 1
        return f"api:{arm[1]}", {"passed_total": 1, "attempts": 1}, []

    monkeypatch.setattr(run, "_run_arm", fake_arm)
    monkeypatch.setattr(run, "write_report", lambda report, out=None: writes.append(sorted(report["arms"])))
    report = {"run": "r", "arms": {}}
    a = SimpleNamespace(backend="api:a,api:b", model="x", only=["count_defs"], concurrency=None, sequential=False, out=None)
    asyncio.run(run._run_arms(a, report, "mechanical", 1))
    assert peak["n"] == 2  # both arms in flight at once
    assert writes == [["api:a"], ["api:a", "api:b"]]  # written after each arm, fastest first
    live["n"] = peak["n"] = 0
    a.sequential = True
    asyncio.run(run._run_arms(a, {"run": "r", "arms": {}}, "mechanical", 1))
    assert peak["n"] == 1  # --sequential really is one at a time


def test_the_repeat_report_names_who_served_each_arm():
    from bench.run import aggregate

    rows = lambda ups: [{"task": "t", "pass": True, "status": "ok", "detail": "", "error": "", "cost_usd": 0.001,
                         "upstream": ups, "api_seconds": 2.0, "turns": 2}]
    runs = [{"passed": 1, "total": 1, "wall_s": 1.0, "cost_usd": 0.001, "rows": rows(["DeepInfra"])},
            {"passed": 1, "total": 1, "wall_s": 1.0, "cost_usd": 0.001, "rows": rows(["DeepInfra", "Reka"])}]
    agg = aggregate(runs, {"t": 2}, 1, 2)
    assert agg["upstreams"] == {"DeepInfra": 2, "Reka": 1} and agg["api_s_per_call"] == 1.0
    md = render_md({"run": "r", "repeats": 2, "arms": {"api:x": agg}})
    assert "DeepInfra x2, Reka x1" in md and "| 1.0 |" in md


def test_combine_sums_arms_across_suites():
    from bench.combine import combine, render

    row = lambda ups, secs, turns: {"upstream": ups, "api_seconds": secs, "turns": turns}
    mech = {"suite": "mechanical", "arms": {"api:x": {"passed_total": 9, "attempts": 10, "cost_total_usd": 0.1,
                                                      "runs": [{"rows": [row(["DeepInfra"], 4.0, 2)]}]}}}
    judg = {"suite": "judgment", "arms": {
        "api:x": {"passed_total": 8, "attempts": 8, "cost_total_usd": 0.05, "runs": [{"rows": [row(["DeepInfra", "Relace"], 2.0, 2)]}]},
        "api:direct": {"passed_total": 8, "attempts": 8, "cost_total_usd": 0.2, "runs": [{"rows": [row([], 2.0, 1)]}]},
    }}
    arms = combine([mech, judg])
    assert arms["api:x"]["passed"] == 17 and arms["api:x"]["attempts"] == 18
    assert arms["api:x"]["hosts"] == {"DeepInfra": 2, "Relace": 1}
    md = render(arms)
    assert md.index("api:direct") < md.index("api:x")  # best rate first
    assert "mechanical 9/10; judgment 8/8" in md and "| 1.50 |" in md and "| direct |" in md


# --- grader self-test (2026-09-24): a grader proves itself on known references before it scores an arm ---


def test_every_grader_passes_its_good_and_fails_its_bad_reference():
    """The fix_median grader passed an answer that rewrote the tests to match the bug; its bad reference
    (bend the tests) is what caught it. Every task in both suites carries both references."""
    from bench import judgment, selftest
    from bench.tasks import TASKS

    assert selftest.check(build, TASKS) == []
    assert selftest.check(judgment.build, judgment.TASKS) == []


def test_selftest_names_a_missing_reference_and_a_bad_caught_on_the_wrong_axis(monkeypatch):
    from bench import references, selftest
    from bench.references import References

    refs = dict(references.REFERENCES)
    del refs["config_value"]
    refs["count_defs"] = References(refs["count_defs"].good, refs["count_defs"].bad, r"decoys flagged")
    monkeypatch.setattr(selftest, "REFERENCES", refs)
    failures = selftest.check(build, [BY_ID["config_value"], BY_ID["count_defs"]])
    assert [f.split(":", 1)[0] for f in failures] == ["config_value", "count_defs"]
    assert "no good/bad reference" in failures[0] and "wrong axis" in failures[1]


def test_run_refuses_to_score_a_suite_whose_grader_passes_its_bad_reference(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import bench.run as run
    from bench import references, selftest
    from bench.references import References

    monkeypatch.setattr(run, "OUT", tmp_path)
    ran = []

    async def fake_arm(m, a, arm, tasks, truth, fixture_root, run_root, repeats, conc=None):
        ran.append(arm)
        return "api:x", {"passed_total": 0, "attempts": 0}

    monkeypatch.setattr(run, "_run_arm", fake_arm)
    good = references.REFERENCES["count_defs"].good
    monkeypatch.setattr(selftest, "REFERENCES", {**references.REFERENCES, "count_defs": References(good, good, r"expected")})
    a = SimpleNamespace(backend="api:x", model="x", only=["count_defs"], concurrency=None, sequential=False, out=None)
    with pytest.raises(SystemExit, match="refusing to score"):
        asyncio.run(run._run_arms(a, {"run": "r", "arms": {}}, "mechanical", 1))
    assert ran == []  # nothing was spent
    assert run.run_selftest(SimpleNamespace(only=["count_defs"]), "mechanical") == 1
