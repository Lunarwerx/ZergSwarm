"""Offline: a routed task fails over to its next leg when a leg is UNAVAILABLE, and only then.

A strictly pinned OpenRouter leg (one host, no fallbacks) is only as available as that host, and an
OpenRouter key's account can be unable to reach a host at all (a privacy policy excluding it answered
404 on two of three real keys, 2026-09-17). Failover is what keeps either from failing every task routed
there. It must never fire on the task's OWN failure - a worker's FAILED, a turn budget, a timeout, or a
400 (our request) - because re-running that on another provider doubles the spend for the same answer.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zswarm import config, jobs  # noqa: E402
from zswarm.jobs import JobManager, leg_unavailable  # noqa: E402
from zswarm.spec import Result, Task  # noqa: E402


def _err(msg: str) -> Result:
    return Result(id="t", status="error", error=msg)


@pytest.mark.parametrize("msg", [
    'openrouter API 404: {"error":{"message":"0 endpoints out of 1 requested are available matching your guardrail restrictions"}}',
    "openrouter API 402: insufficient credits",
    "deepseek API 503: service unavailable",
    "openrouter API 502: bad gateway",
    "NoUsableKey: every one of the 16 deepseek keys is disabled: out of credit (402)",
    "SlowLeg: gemini-3.8-flash averaged 58s a turn over 3 turns (budget 30s) - failing over while the task still has time",
    "ConnectError: [Errno 11001] getaddrinfo failed",
    "openrouter API 404: No endpoints found for deepseek/deepseek-v4.1-flash.",
    'deepseek API 400: {"error":{"message":"Content Exists Risk","type":"invalid_request_error","param":null,"code":"invalid_request_error"}}',
    "gemini API 429: RESOURCE_EXHAUSTED Quota exceeded",  # a free tier spent for the day must down-route
    # the MODEL's bad sample, still bad after the client's resamples: Dredd's drafter died on it (2026-09-26)
    'groq API 400: {"error":{"message":"Tool call validation failed: attempted to call tool \'search\' which was not in request.tools","code":"tool_use_failed"}}',
    # Claude Code's renderings, from a cc worker (2026-09-17: the DeepSeek keys will not be topped up again)
    "claude exit 1: API Error: 503 upstream connect error",
    "NoUsableKey: no key to run on: every key in the pool is disabled; top up and run `zswarm keys probe`",
    "API Error: 402 Insufficient Balance (key 1234abcd disabled as out of credit; NoUsableKey: no key with credit left to retry on: every key in the pool is disabled)",
])
def test_these_mean_the_leg_could_not_serve(msg):
    assert leg_unavailable(_err(msg))


@pytest.mark.parametrize("msg", [
    "FAILED: the premise is wrong, four functions are uncalled",
    "turn budget exhausted without a final answer",
    "task exceeded 600s",
    'deepseek API 400: {"error":{"message":"Invalid tool_choice"}}',
    "openrouter API 4040: not a real status",
    "the worker could not find endpoint handlers for the 404 page",
    "claude exit 1: API Error: 400 invalid tool schema",
])
def test_these_are_the_tasks_own_failure(msg):
    assert not leg_unavailable(_err(msg))


def test_an_ok_result_is_never_unavailable():
    assert not leg_unavailable(Result(id="t", status="ok", error="openrouter API 404"))


SLOWS: list = []  # the slow-leg budget each scripted leg was handed, in call order


def _job_with(monkeypatch, tmp_path, outcomes: dict, plan: list[str]):
    """A JobManager whose route plan and api runner are scripted: `outcomes[model]` is the Result a leg yields."""
    calls: list[str] = []

    async def fake_run(client, task, warm=None, is_pilot=False, user_tag=None, slow_turn_s=None):
        calls.append(task.model)
        SLOWS.append(slow_turn_s)
        r = outcomes[task.model]
        out = Result(id=task.id, backend="api", model=task.model, status=r.status, error=r.error, answer=r.answer,
                     cost_usd=r.cost_usd, turns=r.turns, seconds=r.seconds, usage=dict(r.usage))
        if warm is not None and is_pilot:
            warm.set()
        return out, [{"role": "user", "content": task.prompt}]

    monkeypatch.setattr(jobs, "run_api_task", fake_run)
    m = JobManager(client=object())
    monkeypatch.setattr(m, "route_plan", lambda model: list(plan))
    monkeypatch.setattr(m, "client_for", lambda model: object())

    async def no_probe(client):
        return None

    monkeypatch.setattr(m, "_park_broke_keys", no_probe)
    return m, calls


def _run(m, task):
    async def go():
        job = m.submit([task])
        return await asyncio.wait_for(m.wait(job.id, None), 10)
    return asyncio.run(go())


def test_an_unavailable_leg_fails_over_and_its_spend_is_kept(monkeypatch, tmp_path):
    dead = Result(id="t", status="error", error="openrouter API 404: 0 endpoints ... are available", cost_usd=0.001, turns=1, seconds=1.5,
                  usage={"in_hit": 0, "in_miss": 10, "out": 0, "reasoning": 0})
    good = Result(id="t", status="ok", answer="42", cost_usd=0.002, turns=2, seconds=2.0, usage={"in_hit": 5, "in_miss": 5, "out": 3, "reasoning": 1})
    m, calls = _job_with(monkeypatch, tmp_path, {"deepseek-flash-or": dead, "deepseek-flash": good}, ["deepseek-flash-or", "deepseek-flash"])
    job = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"}))
    r = job.results["t1"]
    assert calls == ["deepseek-flash-or", "deepseek-flash"]
    assert r.status == "ok" and r.answer == "42" and r.model == "deepseek-flash"
    assert r.failover == ["deepseek-flash-or"]
    assert r.cost_usd == pytest.approx(0.003) and r.turns == 3 and r.usage["in_miss"] == 15  # the dead leg was billed
    import json
    row = [json.loads(line) for line in config.LEDGER.read_text(encoding="utf-8").splitlines()][-1]
    assert row["failover"] == "deepseek-flash-or" and row["model"] == "deepseek-flash"


def test_the_tasks_own_failure_is_not_retried_elsewhere(monkeypatch, tmp_path):
    own = Result(id="t", status="error", error="FAILED: the premise is wrong", cost_usd=0.001)
    m, calls = _job_with(monkeypatch, tmp_path, {"deepseek-flash-or": own, "deepseek-flash": own}, ["deepseek-flash-or", "deepseek-flash"])
    r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"})).results["t1"]
    assert calls == ["deepseek-flash-or"] and r.failover == [] and r.error.startswith("FAILED")


def test_every_leg_down_returns_the_last_legs_error(monkeypatch, tmp_path):
    down = Result(id="t", status="error", error="deepseek API 503: unavailable")
    m, calls = _job_with(monkeypatch, tmp_path, {"a": down, "b": down}, ["a", "b"])
    config.MODELS["a"] = config.MODELS["b"] = {"provider": "deepseek"}
    try:
        r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"})).results["t1"]
    finally:
        config.MODELS.pop("a", None), config.MODELS.pop("b", None)
    assert calls == ["a", "b"] and r.status == "error" and r.failover == ["a"] and r.model == "b"


def test_a_prompt_too_large_for_a_legs_limit_fails_over_and_too_large_for_all_says_so(monkeypatch, tmp_path):
    """2026-09-24: 25 of 32 pilot tasks over ~8k tokens died on groq's free-tier 413 with `failover: []`,
    though the tool-free chain has a cerebras leg behind it. The request, not the leg, is too big there."""
    big = 'groq API 413: {"error":{"message":"Request too large for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, Requested 13666"}}'
    also = 'cerebras API 413: {"message":"Request too large: context length exceeded"}'
    m, calls = _job_with(monkeypatch, tmp_path, {"groq-gpt-oss-120b": _err(big), "cerebras-gpt-oss-120b": _err(also)},
                         ["groq-gpt-oss-120b", "cerebras-gpt-oss-120b"])
    r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"})).results["t1"]
    assert calls == ["groq-gpt-oss-120b", "cerebras-gpt-oss-120b"] and r.failover == ["groq-gpt-oss-120b"]
    assert r.status == "error" and r.error.startswith("PromptTooLarge:") and "every tool-free leg" in r.error


def test_a_pinned_task_never_fails_over(monkeypatch, tmp_path):
    """route=False (the bench) means exactly this model, even when it is down."""
    down = Result(id="t", status="error", error="deepseek API 503: unavailable")
    m, calls = _job_with(monkeypatch, tmp_path, {"deepseek-flash": down}, ["deepseek-flash-or", "deepseek-flash"])
    # The model is named EXPLICITLY: this test is about pinning, not about whatever the default is. Since
    # 2026-09-20 an unnamed model is AUTO and resolves by tools (tools:"none" -> the tool-free default).
    r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash", "route": False})).results["t1"]
    assert calls == ["deepseek-flash"] and r.failover == [] and r.status == "error"


def test_a_stalled_leg_fails_over_and_the_ledger_names_the_stall(monkeypatch, tmp_path):
    """client._post's read-timeout retry raises `... API 504: stalled: ...`, which leg_unavailable already
    treats like any other unavailable leg (see test_these_mean_the_leg_could_not_serve above) - this pins
    that a task recovering on its next leg still records WHICH leg stalled, in both the Result and the
    ledger row, not just the ones that ran out of legs and ended the whole task in "error"."""
    stalled = Result(id="t", status="error", error="deepseek API 504: stalled: no reply within 180s (read timeout, retried once)",
                      cost_usd=0.001, turns=2, seconds=360.0, usage={"in_hit": 0, "in_miss": 10, "out": 0, "reasoning": 0})
    good = Result(id="t", status="ok", answer="42", cost_usd=0.002, turns=1, seconds=2.0, usage={"in_hit": 5, "in_miss": 5, "out": 3, "reasoning": 1})
    m, calls = _job_with(monkeypatch, tmp_path, {"deepseek-flash-or": stalled, "deepseek-flash": good}, ["deepseek-flash-or", "deepseek-flash"])
    job = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"}))
    r = job.results["t1"]
    assert calls == ["deepseek-flash-or", "deepseek-flash"]
    assert r.status == "ok" and r.failover == ["deepseek-flash-or"] and r.stalled == ["deepseek-flash-or"]
    import json
    row = [json.loads(line) for line in config.LEDGER.read_text(encoding="utf-8").splitlines()][-1]
    assert row["stalled"] == "deepseek-flash-or" and row["failover"] == "deepseek-flash-or"


def test_a_task_that_stalls_out_on_every_leg_names_the_stall_too(monkeypatch, tmp_path):
    stalled_a = Result(id="t", status="error", error="deepseek API 504: stalled: no reply within 180s (read timeout, retried once)")
    stalled_b = Result(id="t", status="error", error="openrouter API 504: stalled: no reply within 180s (read timeout, retried once)")
    m, calls = _job_with(monkeypatch, tmp_path, {"a": stalled_a, "b": stalled_b}, ["a", "b"])
    config.MODELS["a"] = config.MODELS["b"] = {"provider": "deepseek"}
    try:
        r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"})).results["t1"]
    finally:
        config.MODELS.pop("a", None), config.MODELS.pop("b", None)
    assert calls == ["a", "b"] and r.status == "error" and r.failover == ["a"] and r.stalled == ["a", "b"]


def test_a_leg_with_no_key_at_all_fails_over(monkeypatch, tmp_path):
    good = Result(id="t", status="ok", answer="ok")
    m, calls = _job_with(monkeypatch, tmp_path, {"deepseek-flash": good}, ["deepseek-flash-or", "deepseek-flash"])

    def client_for(model):
        if model == "deepseek-flash-or":
            raise RuntimeError("No openrouter API key found.")
        return object()

    monkeypatch.setattr(m, "client_for", client_for)
    r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"})).results["t1"]
    assert calls == ["deepseek-flash"] and r.status == "ok" and r.failover == ["deepseek-flash-or"]


def test_a_one_shot_ask_fails_over_the_same_way(monkeypatch, tmp_path):
    """zswarm_ask and `zswarm ask` route and fail over like a job task: only on an unavailable path."""
    import zswarm.agent as agent

    seen = []

    async def fake_ask(client, prompt, model=None, **kw):
        seen.append(model)
        if model == "deepseek-flash-hf":
            return Result(id="ask", model=model, status="error", error="huggingface API 503: overloaded", cost_usd=0.0, seconds=0.5)
        return Result(id="ask", model=model, status="ok", answer="391", cost_usd=0.00001, seconds=1.0)

    monkeypatch.setattr(agent, "ask", fake_ask)
    m = JobManager(client=object())
    monkeypatch.setattr(m, "route_plan", lambda model: ["deepseek-flash-hf", "deepseek-flash-or"])
    monkeypatch.setattr(m, "client_for", lambda model: object())
    r = asyncio.run(m.ask_routed("17*23?", "deepseek-flash"))
    assert seen == ["deepseek-flash-hf", "deepseek-flash-or"] and r.status == "ok"
    assert r.failover == ["deepseek-flash-hf"] and r.seconds == pytest.approx(1.5)
    # route=False: exactly the model, no plan consulted
    seen.clear()
    r = asyncio.run(m.ask_routed("17*23?", "deepseek-flash-hf", route=False))
    assert seen == ["deepseek-flash-hf"] and r.status == "error" and r.failover == []
    # the task's own failure is not retried
    async def own_fail(client, prompt, model=None, **kw):
        seen.append(model)
        return Result(id="ask", model=model, status="error", error="deepseek API 400: bad schema")
    seen.clear()
    monkeypatch.setattr(agent, "ask", own_fail)
    r = asyncio.run(m.ask_routed("x", "deepseek-flash"))
    assert seen == ["deepseek-flash-hf"] and r.failover == []


# --- cc tasks fail over too (2026-09-17: the DeepSeek keys will not be topped up again) ---------------------


def _cc_job(monkeypatch, tmp_path, plan, pools):
    from types import SimpleNamespace

    used: list[tuple[str, str]] = []

    async def fake_cc(task, api_key):
        used.append((task.model, api_key))
        return Result(id=task.id, backend="cc", model=task.model, status="ok", answer="done", cost_usd=0.01), {"exit": 0}

    monkeypatch.setattr(config, "KEYS_STATE", tmp_path / "keys.json")
    monkeypatch.setattr(jobs, "run_cc_task", fake_cc)
    m = JobManager(client=object())
    monkeypatch.setattr(m, "route_plan", lambda model, backend="api": list(plan))
    monkeypatch.setattr(m, "client_for", lambda model: SimpleNamespace(pool=pools[model]))

    async def no_probe(client):
        return None

    monkeypatch.setattr(m, "_park_broke_keys", no_probe)
    return m, used


def test_a_cc_task_fails_over_to_hugging_face_when_no_deepseek_key_is_left(monkeypatch, tmp_path):
    from zswarm.client import KeyPool

    monkeypatch.setattr(config, "KEYS_STATE", tmp_path / "keys.json")
    ds, hf = KeyPool(["sk-ds-1"]), KeyPool(["hf_tok_1"], provider="huggingface")
    ds.broke("sk-ds-1")
    m, used = _cc_job(monkeypatch, tmp_path, ["deepseek-flash", "deepseek-flash-hf", "deepseek-flash-or"],
                      {"deepseek-flash": ds, "deepseek-flash-hf": hf})
    r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "backend": "cc", "tools": "none", "model": "deepseek-flash"})).results["t1"]
    assert used == [("deepseek-flash-hf", "hf_tok_1")]  # the disabled DeepSeek key never launched a process
    assert r.status == "ok" and r.model == "deepseek-flash-hf" and r.failover == ["deepseek-flash"], r.as_dict()


def test_a_cc_route_skips_a_provider_claude_code_cannot_talk_to(monkeypatch, tmp_path):
    from zswarm.client import KeyPool

    monkeypatch.setattr(config, "KEYS_STATE", tmp_path / "keys.json")
    ds = KeyPool(["sk-ds-1"])
    ds.broke("sk-ds-1")
    config.MODELS["x-no-anthropic"] = {"provider": "moonshot"}  # an OpenAI-only endpoint
    try:
        m, used = _cc_job(monkeypatch, tmp_path, ["deepseek-flash", "x-no-anthropic"], {"deepseek-flash": ds})
        r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "backend": "cc", "tools": "none", "model": "deepseek-flash"})).results["t1"]
    finally:
        config.MODELS.pop("x-no-anthropic", None)
    assert used == [] and r.status == "error" and "NoUsableKey" in (r.error or "") and r.failover == [], r.as_dict()


def test_a_cc_worker_on_hugging_face_gets_its_endpoint_its_bearer_token_and_the_pinned_model(monkeypatch, tmp_path):
    from zswarm import claude_env

    monkeypatch.setattr(config, "CC_CONFIG_DIR", tmp_path / "cc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "the-operators-own")
    env = claude_env.cc_env("hf_tok", "deepseek-flash-hf")
    assert env["ANTHROPIC_BASE_URL"] == "https://router.huggingface.co"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "hf_tok" and "ANTHROPIC_API_KEY" not in env  # the operator's key never leaks in
    pinned = "deepseek-ai/DeepSeek-V4.1-Flash:deepinfra"
    assert env["ANTHROPIC_MODEL"] == env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == env["ANTHROPIC_SMALL_FAST_MODEL"] == pinned
    assert claude_env.cc_model_id("deepseek-flash-hf") == pinned
    orr = claude_env.cc_env("sk-or", "deepseek-flash-or")
    assert orr["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api" and orr["ANTHROPIC_MODEL"] == "deepseek/deepseek-v4.1-flash"
    # DeepSeek itself is exactly as before.
    ds = claude_env.cc_env("sk-ds")
    assert ds["ANTHROPIC_BASE_URL"] == config.PROVIDERS["deepseek"]["anthropic_url"] and ds["ANTHROPIC_API_KEY"] == "sk-ds" and "ANTHROPIC_AUTH_TOKEN" not in ds
    assert ds["ANTHROPIC_MODEL"] == "deepseek-flash" and claude_env.cc_model_id("deepseek-flash") == "deepseek-flash"
    config.MODELS["x-no-anthropic"] = {"provider": "moonshot"}
    try:
        with pytest.raises(RuntimeError, match="cannot run cc"):
            claude_env.cc_env("k", "x-no-anthropic")
    finally:
        config.MODELS.pop("x-no-anthropic", None)


def test_a_cc_worker_gets_no_operator_credential_even_when_the_allowlist_is_widened(monkeypatch, tmp_path):
    # cc_env used to copy os.environ minus only CLAUDE_*/ANTHROPIC_*: a DeepSeek-driven Claude Code with a
    # shell got every other provider key and cloud credential. Widening to everything must not undo that.
    from zswarm import claude_env

    monkeypatch.setattr(config, "CC_CONFIG_DIR", tmp_path / "cc")
    monkeypatch.setenv("GITHUB_TOKEN", "leak")
    monkeypatch.setenv("GEMINI_API_KEY", "leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://operator.example")
    env = claude_env.cc_env("sk-ds")
    assert "leak" not in env.values() and env["ANTHROPIC_API_KEY"] == "sk-ds" and "PATH" in {k.upper() for k in env}
    monkeypatch.setenv("ZSWARM_CHILD_ENV_ALLOW", ".*")
    monkeypatch.setenv("SOME_TOOL_DIR", "/opt/tool")
    widened = claude_env.cc_env("sk-ds")
    assert "leak" not in widened.values() and "CLAUDECODE" not in {k.upper() for k in widened}
    assert widened["ANTHROPIC_BASE_URL"] == config.PROVIDERS["deepseek"]["anthropic_url"] and "/opt/tool" in widened.values()


def test_a_crawling_leg_fails_over_and_only_a_leg_with_somewhere_to_go_is_timed(monkeypatch, tmp_path):
    """2026-09-22: at concurrency 48 the free gemini leg answered at ~60 s a turn and 47 of 48 tasks timed out on it."""
    SLOWS.clear()
    slow = Result(id="t", status="error", error="SlowLeg: gemini-3.8-flash averaged 58s a turn over 3 turns (budget 30s)", cost_usd=0.0, turns=3, seconds=174.0)
    good = Result(id="t", status="ok", answer="done", cost_usd=0.001, turns=2, seconds=10.0)
    m, calls = _job_with(monkeypatch, tmp_path, {"gemini-3.8-flash": slow, "deepseek-flash-or": good}, ["gemini-3.8-flash", "deepseek-flash-or"])
    r = _run(m, Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "read", "model": "gemini-3.8-flash"})).results["t1"]
    assert calls == ["gemini-3.8-flash", "deepseek-flash-or"] and r.status == "ok" and r.failover == ["gemini-3.8-flash"]
    assert SLOWS == [config.SLOW_LEG_TURN_S, None], "the last leg has nowhere to go, so it is never timed"
    assert r.turns == 5 and r.seconds == pytest.approx(184.0)  # the slow leg's turns and time are kept


def test_a_route_cut_short_by_missing_credit_fails_fast_with_one_message(monkeypatch, tmp_path):
    """2026-09-24, job 20260924-200352-b60d: every OpenRouter key out of credit left gemini-3.8-flash as the tool
    route's only leg, and the last leg was never timed, so 105 of 140 tasks sat the full 600 s. Its next leg is
    the caller's own model now: the leg is timed, the first trip stops its siblings, and a later job does not call it."""
    calls: list[str] = []

    async def fake_run(client, task, warm=None, is_pilot=False, user_tag=None, slow_turn_s=None):
        calls.append(task.model)
        if slow_turn_s is None:  # untimed, the crawl runs into the task's own budget
            return Result(id=task.id, backend="api", model=task.model, status="timeout", error="task exceeded 600s"), []
        if not is_pilot:
            await warm.wait()
            await asyncio.sleep(3600)  # crawling; only a trip ends this
        await asyncio.sleep(0.05)
        return Result(id=task.id, backend="api", model=task.model, status="error", turns=3,
                      error="SlowLeg: gemini-3.8-flash averaged 58s a turn over 3 turns (budget 30s)"), []

    monkeypatch.setattr(jobs, "run_api_task", fake_run)
    monkeypatch.setattr(jobs, "_TRIPS", {}, raising=False)
    monkeypatch.setattr(jobs.keys, "has_credit", lambda provider: provider != "openrouter")
    monkeypatch.setattr(jobs.keys, "pool_for", lambda provider: None)
    m = JobManager(client=object())
    monkeypatch.setattr(m, "route_plan", lambda model: ["gemini-3.8-flash"])  # deepseek-flash-or dropped: no credit
    monkeypatch.setattr(m, "client_for", lambda model: object())

    async def no_probe(client):
        return None

    monkeypatch.setattr(m, "_park_broke_keys", no_probe)
    tasks = [Task.from_dict({"id": f"t{i}", "prompt": "x", "cwd": str(tmp_path), "tools": "read", "model": "gemini-3.8-flash"}, {}, i) for i in range(3)]

    async def go():
        first = m.submit(tasks)
        await asyncio.wait_for(m.wait(first.id, None), 5)
        with pytest.raises(ValueError) as refused:  # a later job is refused at submit, before any task runs
            m.submit([Task.from_dict({"id": "u", "prompt": "x", "cwd": str(tmp_path), "tools": "read", "model": "gemini-3.8-flash"})])
        return first, str(refused.value)

    job, later = asyncio.run(go())
    errors = {r.error for r in job.results.values()}
    assert [r.status for r in job.results.values()] == ["error"] * 3 and len(errors) == 1
    msg = errors.pop()
    assert msg.startswith("NoCreditLeft:") and "deepseek-flash-or" in msg and job.summary()["error"] == msg
    # Only the pilot ever calls the leg: its siblings wait for the pilot before taking a gate slot (jobs._run_legs),
    # so the trip reaches them before their first call, and the later job is refused at submit.
    assert later.startswith("NoCreditLeft:") and "tripped" in later and calls == ["gemini-3.8-flash"], "a tripped leg is not called again"


def test_a_pinned_task_whose_route_gives_out_goes_on_by_its_profile(monkeypatch, tmp_path):
    """Owner, 2026-09-25: zswarm deals with dead keys itself. 15 of 67 builders pinned to gemini-3.8-flash died
    PoolSaturated with "no other leg" while the same work on the code profile ran; a pinned model is a preference,
    so a routed task goes on by its profile. A strict pin (route=False, an A/B arm) still reports its own leg."""
    import zswarm.agent as agent
    from zswarm import dispatch

    async def pinned_leg(client, task, warm=None, is_pilot=False, user_tag=None, slow_turn_s=None, **kw):
        return Result(id=task.id, backend="api", model=task.model, status="error", cost_usd=0.01, turns=1,
                      error="gemini API 429: PoolSaturated: every gemini key is rate-limited and the task has no other leg"), []

    async def profile_leg(client, task, warm=None, is_pilot=False, user_tag=None, slow_turn_s=None, resume_messages=None, **kw):
        return Result(id=task.id, backend="api", model=task.model, status="ok", answer="done", cost_usd=0.02, turns=2), []

    monkeypatch.setattr(jobs, "run_api_task", pinned_leg)
    monkeypatch.setattr(agent, "run_api_task", profile_leg)
    monkeypatch.setattr(dispatch, "plan_for", lambda task, explain=False: {
        "profile": task.profile, "candidates": [{"model": m, "reasoning_effort": "high", "thinking": True}
                                                for m in ("rank:glm-5-3",) if m not in task.exclude_models]})
    m = JobManager(client=object())
    monkeypatch.setattr(m, "route_plan", lambda model: ["gemini-3.8-flash"])
    monkeypatch.setattr(m, "client_for", lambda model: SimpleNamespace(pool=None))

    async def no_probe(client):
        return None

    monkeypatch.setattr(m, "_park_broke_keys", no_probe)
    spec = {"prompt": "x", "cwd": str(tmp_path), "tools": "read", "model": "gemini-3.8-flash"}
    tasks = [Task.from_dict({**spec, "id": "routed"}, {}, 0), Task.from_dict({**spec, "id": "strict", "route": False}, {}, 1)]

    async def go():
        job = m.submit(tasks)
        return await asyncio.wait_for(m.wait(job.id, None), 5)

    job = asyncio.run(go())
    routed, strict = job.results["routed"], job.results["strict"]
    assert routed.status == "ok" and routed.model == "rank:glm-5-3" and "gemini-3.8-flash" in routed.failover
    assert routed.selection["unpinned_from"] == "gemini-3.8-flash" and abs(routed.cost_usd - 0.03) < 1e-9
    assert strict.status == "error" and "PoolSaturated" in strict.error and strict.model == "gemini-3.8-flash"


def test_a_task_every_route_failed_rests_and_runs_again(monkeypatch, tmp_path):
    """Owner, 2026-09-25: "rerun them if they're dead." A task whose whole ladder could not serve comes back only
    after zswarm has rested and tried it again with the budget it has left, and the time its calls sat on 429s is
    not taken off that budget (job 20260926-003426-92c1: 13 builders' run clocks spent on Gemini 429s)."""
    import zswarm.agent as agent
    from zswarm import dispatch

    outcomes = iter(["down", "ok"])

    async def leg(client, task, warm=None, is_pilot=False, user_tag=None, slow_turn_s=None, resume_messages=None, **kw):
        if next(outcomes) == "down":
            # Its whole clock went on rate-limited waits. (A 413 from its only leg is no longer this case: that
            # prompt fits no leg, so it fails at once as PromptTooLarge; see the test below.)
            return Result(id=task.id, backend="api", model=task.model, status="error", cost_usd=0.01, turns=1,
                          seconds=task.timeout_s, rested_s=task.timeout_s - 5,
                          error='groq API 429: {"error":{"message":"Rate limit reached for model qwen3"}}'), []
        return Result(id=task.id, backend="api", model=task.model, status="ok", answer="done", cost_usd=0.02, turns=1), []

    monkeypatch.setattr(agent, "run_api_task", leg)
    monkeypatch.setattr(dispatch, "plan_for", lambda task, explain=False: {
        "profile": task.profile, "candidates": [{"model": "rank:glm-5-3", "reasoning_effort": "high", "thinking": True}]})
    monkeypatch.setattr(config, "DEAD_RERUN_PATIENCE_S", 60.0)
    monkeypatch.setattr(config, "DEAD_RERUN_REST_S", 0.0)
    m = JobManager(client=object())
    monkeypatch.setattr(m, "client_for", lambda model: SimpleNamespace(pool=None))

    async def no_probe(client):
        return None

    monkeypatch.setattr(m, "_park_broke_keys", no_probe)

    async def go():
        job = m.submit([Task.from_dict({"id": "t", "prompt": "x", "cwd": str(tmp_path), "tools": "read"}, {}, 0)])
        return await asyncio.wait_for(m.wait(job.id, None), 5)

    res = asyncio.run(go()).results["t"]
    assert res.status == "ok" and res.selection["dead_reruns"] == 1 and abs(res.cost_usd - 0.03) < 1e-9


def test_a_413_over_one_keys_org_limit_is_served_by_another_key_without_the_dead_rerun_rest(monkeypatch, tmp_path):
    """Jobs 20260926-050510-e32e and three siblings: Groq answered 413 "Request too large ... in organization ...
    on tokens per minute" for one key's org while keys of other orgs served the same size. The client raised it at
    once, the one-leg profile route came back dead, and every such task slept the dead-rerun rest in its job's
    concurrency slot, past its own timeout, at turns 0."""
    import httpx

    from zswarm import dispatch
    from zswarm.client import DeepSeekClient

    small_org, big_org = "sk-orgsmall0000xxxx", "sk-orgbig00000xxxx"
    seen: list[str] = []

    def handler(request):
        key = request.headers["Authorization"].removeprefix("Bearer ")
        seen.append(key)
        if key == small_org:
            return httpx.Response(413, json={"error": {
                "message": "Request too large for model `qwen/qwen3.8-27b` in organization `org_small` service tier "
                           "`on_demand` on tokens per minute (TPM): Limit 6000, Requested 9120, please reduce your message size",
                "type": "tokens", "code": "rate_limit_exceeded"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 9120, "completion_tokens": 1}, "model": "deepseek-flash"})

    c = DeepSeekClient(api_keys=[small_org, big_org])
    c._http = httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(dispatch, "plan_for", lambda task, explain=False: {
        "profile": task.profile, "candidates": [{"model": "deepseek-flash", "reasoning_effort": None, "thinking": None}]})
    # Production's rerun timings: the old code sleeps 150 s here, far past the wait below.
    monkeypatch.setattr(config, "DEAD_RERUN_PATIENCE_S", 3 * 3600.0)
    monkeypatch.setattr(config, "DEAD_RERUN_REST_S", 150.0)
    m = JobManager(client=c)

    async def no_probe(client):
        return None

    monkeypatch.setattr(m, "_park_broke_keys", no_probe)

    async def go():
        job = m.submit([Task.from_dict({"id": "t", "prompt": "x", "cwd": str(tmp_path), "tools": "none"}, {}, 0)])
        return await asyncio.wait_for(m.wait(job.id, None), 5)

    try:
        res = asyncio.run(go()).results["t"]
    except TimeoutError:
        pytest.fail(f"the task went to the dead-rerun rest instead of the other key (keys tried: {seen})")
    assert seen == [small_org, big_org], seen
    assert res.status == "ok" and res.answer == "done" and "dead_reruns" not in (res.selection or {}), res
    assert c.pool.available() == 2  # the small org's key is fine for a smaller request: neither rested nor disabled


def test_run_api_task_gives_up_a_crawling_leg_after_the_minimum_turns(tmp_path):
    import zswarm.agent as agent
    from zswarm.usage import ChatResult, Usage

    class Crawl:
        async def chat(self, messages, **kw):
            return ChatResult(message={"role": "assistant", "content": ""}, finish_reason="stop", usage=Usage(), model="slow",
                              seconds=40.0, cost_usd=0.0, peak=False)

    task = Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"})
    res, _ = asyncio.run(agent.run_api_task(Crawl(), task, slow_turn_s=30.0))
    assert res.status == "error" and res.error.startswith("SlowLeg:") and res.turns == config.SLOW_LEG_MIN_TURNS
    assert leg_unavailable(res)
    # With no next leg (slow_turn_s None) the same crawl runs on to its own end - a slow answer beats none.
    task = Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"})
    res, _ = asyncio.run(agent.run_api_task(Crawl(), task))
    assert not (res.error or "").startswith("SlowLeg") and res.turns > config.SLOW_LEG_MIN_TURNS - 1


def test_a_task_that_hits_its_wall_hands_back_what_it_gathered(tmp_path):
    """2026-09-24, job 20260924-234903-1c5b: 20 tasks timed out after ~9 turns and 11 tool calls each and came
    back with an empty answer. The turns it spent now come back as a PARTIAL answer; the status stays timeout."""
    import zswarm.agent as agent
    from zswarm.usage import ChatResult, Usage

    (tmp_path / "evidence.txt").write_text("the line that matters\n", encoding="utf-8")

    class ThenHang:
        turns = 0

        async def chat(self, messages, **kw):
            self.turns += 1
            if self.turns > 1:
                await asyncio.sleep(3600)
            call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "evidence.txt"}'}}
            return ChatResult(message={"role": "assistant", "content": "", "tool_calls": [call]}, finish_reason="tool_calls",
                              usage=Usage(), model="m", seconds=0.01, cost_usd=0.0, peak=False)

    task = Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "read", "model": "deepseek-flash"})
    task.timeout_s = 0.5
    res, _ = asyncio.run(agent.run_api_task(ThenHang(), task))
    assert res.status == "timeout" and res.answer.startswith("PARTIAL")
    assert "read_file" in res.answer and "the line that matters" in res.answer



def test_a_pilot_that_fails_before_its_first_reply_does_not_strand_the_job(monkeypatch, tmp_path):
    """Every api task but the pilot waits for the pilot's first reply (it lands the shared prefix in the cache).
    The real agent loop sets that event only after a SUCCESSFUL reply, so a pilot that failed first (an error,
    no key, every leg down) left the rest of the job waiting until each task's own timeout, with 0 calls made -
    the 900 s zero-call gemini timeouts seen on 2026-09-24. A finished pilot must release the others either way."""

    async def fake_run(client, task, warm=None, is_pilot=False, user_tag=None, slow_turn_s=None):
        if warm is not None and not is_pilot:
            await warm.wait()  # what agent._loop does before its first call
        if is_pilot:  # fails before any reply, so, like the real loop, it never sets `warm`
            return Result(id=task.id, backend="api", model=task.model, status="error", error="deepseek API 400: bad request"), []
        return Result(id=task.id, backend="api", model=task.model, status="ok", answer="done", turns=1), []

    monkeypatch.setattr(jobs, "run_api_task", fake_run)
    m = JobManager(client=object())
    monkeypatch.setattr(m, "route_plan", lambda model: ["deepseek-flash"])
    monkeypatch.setattr(m, "client_for", lambda model: object())

    async def no_probe(client):
        return None

    monkeypatch.setattr(m, "_park_broke_keys", no_probe)
    tasks = [Task.from_dict({"id": f"t{i}", "prompt": "x", "cwd": str(tmp_path), "tools": "read", "model": "gemini-3.8-flash"}, {}, i) for i in range(3)]

    async def go():
        job = m.submit(tasks)
        return await asyncio.wait_for(m.wait(job.id, None), 5)

    job = asyncio.run(go())
    statuses = sorted(r.status for r in job.results.values())
    assert statuses == ["error", "ok", "ok"], statuses
