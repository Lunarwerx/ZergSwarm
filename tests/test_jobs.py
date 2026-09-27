"""Offline: the job manager against a fake client - the budget guard and the crash guard."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zswarm import config  # noqa: E402
from zswarm.client import ChatResult, Usage  # noqa: E402
from zswarm.jobs import JobManager  # noqa: E402
from zswarm.spec import Task  # noqa: E402


class _FakeClient:
    """Stands in for DeepSeekClient: every chat() answers OK at a fixed cost, or raises."""

    def __init__(self, cost: float = 0.0, raise_exc: Exception | None = None):
        self.cost, self.raise_exc, self.calls = cost, raise_exc, 0

    async def chat(self, messages, **kw):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        await asyncio.sleep(0.01)
        return ChatResult(message={"role": "assistant", "content": "OK"}, finish_reason="stop", usage=Usage(), model="deepseek-flash", seconds=0.01, cost_usd=self.cost, peak=False)

    async def aclose(self):
        pass


def _isolate(monkeypatch, tmp_path):
    # Never write a test job into the real ~/.zswarm ledger.
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "LEDGER", tmp_path / "ledger.jsonl")


def test_job_budget_cancels_the_rest(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)

    async def go():
        m = JobManager(client=_FakeClient(cost=0.2))
        tasks = [Task.from_dict({"id": f"t{i}", "prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"}, {}, i) for i in range(6)]
        return await m.run_batch(tasks, concurrency=1, budget_usd=0.5)

    job = asyncio.run(go())
    st = [job.results[f"t{i}"].status for i in range(6)]
    assert job.state == "cancelled" and st[:3] == ["ok", "ok", "ok"] and set(st[3:]) == {"cancelled"}, st
    assert "job budget exceeded" in (job.results["t5"].error or "")
    assert job.summary()["budget_usd"] == 0.5 and abs(job.cost() - 0.6) < 1e-9


def test_a_done_job_json_says_finished_at_the_top_level(tmp_path, monkeypatch):
    # A hand-written waiter polls `json.load(job.json).get("finished")`; with the stamp only under
    # "summary", two such loops ran 70 minutes past a done job on 2026-09-23.
    _isolate(monkeypatch, tmp_path)

    async def go():
        m = JobManager(client=_FakeClient())
        tasks = [Task.from_dict({"id": "t0", "prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"}, {}, 0)]
        return await m.run_batch(tasks, concurrency=1)

    job = asyncio.run(go())
    doc = json.loads((job.dir / "job.json").read_text(encoding="utf-8"))
    assert doc["state"] == "done" and doc["finished"], {k: doc.get(k) for k in ("state", "finished")}
    assert doc["finished"] == doc["summary"]["finished"]


def test_resume_from_job_reruns_only_changed_or_failed_tasks(tmp_path, monkeypatch):
    # resume_from_job: a re-run batch pays only for what changed. Answers are matched by content hash from the
    # earlier job's journal on disk (a second manager, as after a crash), never by task id: a renamed task with the
    # same content reuses, an edited prompt runs again, and a task that errored there runs again.
    _isolate(monkeypatch, tmp_path)

    class _FailsOnMarker(_FakeClient):
        async def chat(self, messages, **kw):
            # Only the user turn: the worker system prompt itself mentions "FAILED:".
            if any(m.get("role") == "user" and "FAIL" in str(m.get("content")) for m in messages):
                self.calls += 1
                raise RuntimeError("boom")
            return await super().chat(messages, **kw)

    def spec(tid: str, prompt: str) -> Task:
        return Task.from_dict({"id": tid, "prompt": prompt, "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"}, {}, 0)

    async def go():
        first = await JobManager(client=_FailsOnMarker(cost=0.1)).run_batch([spec("a", "one"), spec("b", "two"), spec("c", "three FAIL")], concurrency=1)
        again = _FakeClient(cost=0.1)
        second = await JobManager(client=again).run_batch([spec("a", "one, fixed"), spec("renamed", "two"), spec("c", "three FAIL")],
                                                          concurrency=1, resume_from=first.id)
        return first, second, again.calls

    first, second, calls = asyncio.run(go())
    assert [first.results[t].status for t in "abc"] == ["ok", "ok", "error"]
    r = second.results
    assert calls == 2, calls  # "a" (edited) and "c" (errored before) ran; "renamed" did not
    assert r["renamed"].cached_from == first.id and r["renamed"].status == "ok" and r["renamed"].cost_usd == 0.0
    assert r["a"].cached_from == "" and r["c"].cached_from == "" and r["c"].status == "ok"
    s = second.summary()
    assert s["resumed_from"] == first.id and s["cached"] == 1 and abs(second.cost() - 0.2) < 1e-9
    ledger = [json.loads(line) for line in config.LEDGER.read_text(encoding="utf-8").splitlines()]
    assert [row["task"] for row in ledger if row.get("cached")] == ["renamed"]


def test_prunable_finds_only_finished_folders_older_than_the_window(tmp_path, monkeypatch):
    # The job folders are a cache of delivered work (results + worker transcripts); the numbers live in the
    # ledger and the synced shard. One day put 1.2 GiB here, so the cache needs a broom (Michael, 2026-09-16).
    import datetime as dt

    from zswarm.job import Job

    _isolate(monkeypatch, tmp_path)
    now = dt.datetime.now(dt.timezone.utc)
    for days, name in ((10, "old"), (0, "new")):
        d = config.JOBS_DIR / ((now - dt.timedelta(days=days)).strftime("%Y%m%d-%H%M%S") + f"-{name}")
        d.mkdir(parents=True)
        (d / "job.json").write_text("{}" * 100, encoding="utf-8")
        (d / "transcripts").mkdir()
        (d / "transcripts" / "t1.json").write_text("x" * 5000, encoding="utf-8")

    victims = Job.prunable(7)
    assert [d.name.split("-")[-1] for d, _ in victims] == ["old"]  # the fresh one stays
    assert victims[0][1] > 5000  # size counts the nested transcript
    # A window wider than the oldest folder spares everything; a zero window is "older than right now", and a
    # folder stamped in the current second is not older than it, so only the aged one is certain to appear.
    assert Job.prunable(30) == [] and any(d.name.endswith("old") for d, _ in Job.prunable(0))
    assert config.JOBS_DIR.exists()  # prunable() only reports; deleting is --apply's job


def test_backend_exception_becomes_error_result_not_a_hang(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)

    async def go():
        m = JobManager(client=_FakeClient(raise_exc=RuntimeError("boom")))
        # model pinned: these tests inject a fake client and are about job mechanics, not model choice.
        # Since 2026-09-20 an unnamed model is AUTO and tools:"none" would resolve to the tool-free default.
        t = Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"}, {}, 0)
        return await asyncio.wait_for(m.run_batch([t]), 10)

    job = asyncio.run(go())
    r = job.results["t1"]
    assert job.state == "done" and r.status == "error" and "boom" in (r.error or "")
    assert (tmp_path / "ledger.jsonl").exists()  # even a crashed task is journaled


def test_a_running_task_reports_its_age_and_is_not_listed_as_queued(tmp_path, monkeypatch):
    # A `cc` worker reports nothing until its process exits, so for its whole life the job read
    # cost 0 and longest_task_s 0, and results listed it under "pending" beside the tasks still
    # queued - a stuck worker and a busy one looked identical (measured 2026-09-15, a 5-task cc job).
    _isolate(monkeypatch, tmp_path)
    from zswarm.results import job_payload

    class _SlowClient(_FakeClient):
        async def chat(self, messages, **kw):
            await asyncio.sleep(1.2)
            return await super().chat(messages, **kw)

    async def go():
        m = JobManager(client=_SlowClient())
        tasks = [Task.from_dict({"id": f"t{i}", "prompt": "x", "cwd": str(tmp_path), "tools": "none", "model": "deepseek-flash"}, {}, i) for i in range(3)]
        job = m.submit(tasks, concurrency=1)
        await asyncio.sleep(1.1)
        mid = job.summary(), job_payload(job, 100)
        await m.wait(job.id, 20)
        return mid, job

    (summary, payload), job = asyncio.run(go())
    assert summary["counts"].get("running") == 1 and summary["counts"].get("pending") == 2, summary["counts"]
    assert summary["oldest_running_s"] >= 1.0, summary
    assert [r["id"] for r in payload["running"]] == ["t0"] and payload["running"][0]["elapsed_s"] >= 1.0, payload
    assert payload["pending"] == ["t1", "t2"], payload  # queued only - the running task is not repeated here
    assert "1 running" in payload["hint"] and "2 queued" in payload["hint"], payload["hint"]
    assert job.summary()["oldest_running_s"] == 0.0  # nothing runs once the job is done


# --- a cc worker's key runs out mid-task (2026-09-16) --------------------------------------------


def _cc_pool_client(keys):
    from zswarm.client import KeyPool

    fc = _FakeClient()
    fc.pool = KeyPool(keys)

    async def probe(max_age_s=0.0):
        return []

    fc.probe_balances = probe
    return fc


def test_a_cc_task_that_dies_of_402_goes_again_on_the_next_key(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    from zswarm import jobs
    from zswarm.spec import Result

    keys = ["sk-aaaa1111", "sk-bbbb2222", "sk-cccc3333"]
    used = []

    async def fake_cc(task, api_key):
        used.append(api_key)
        if api_key == keys[0]:
            return Result(id=task.id, backend="cc", model=task.model, status="error", error="API Error: 402 Insufficient Balance"), {}
        return Result(id=task.id, backend="cc", model=task.model, status="ok", answer="done"), {}

    monkeypatch.setattr(jobs, "run_cc_task", fake_cc)
    fc = _cc_pool_client(keys)

    async def go():
        m = JobManager(client=fc)
        t = Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "backend": "cc", "tools": "none", "model": "deepseek-flash"}, {}, 0)
        return m, await asyncio.wait_for(m.run_batch([t]), 10)

    m, job = asyncio.run(go())
    r = job.results["t1"]
    assert r.status == "ok" and used == [keys[0], keys[1]] and m.cc_rotations == 1
    st = fc.pool.status()
    assert st[0]["broke"] and st[0]["status"] == 402 and not st[1]["broke"]
    # The parked key is not handed to the next cc task either.
    used.clear()
    m2, job2 = asyncio.run(go())
    assert job2.results["t1"].status == "ok" and keys[0] not in used


def test_a_cc_task_with_no_key_left_says_so(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    from zswarm import jobs
    from zswarm.spec import Result

    keys = ["sk-aaaa1111", "sk-bbbb2222"]
    used = []

    async def fake_cc(task, api_key):
        used.append(api_key)
        return Result(id=task.id, backend="cc", model=task.model, status="error", error="API Error: 402 Insufficient Balance"), {}

    monkeypatch.setattr(jobs, "run_cc_task", fake_cc)
    fc = _cc_pool_client(keys)

    async def go():
        m = JobManager(client=fc)
        t = Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "backend": "cc", "tools": "none", "model": "deepseek-flash"}, {}, 0)
        return m, await asyncio.wait_for(m.run_batch([t]), 10)

    m, job = asyncio.run(go())
    r = job.results["t1"]
    assert r.status == "error" and "no key with credit left" in (r.error or "") and used == keys and m.cc_rotations == 1
    assert all(s["broke"] for s in fc.pool.status())


# --- the review of the cc rotation (2026-09-16) ------------------------------------------------------------


def _cc_task(tmp_path):
    return Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "backend": "cc", "tools": "none", "model": "deepseek-flash"}, {}, 0)


def _dies_on(keys_that_die, dead_cost=0.0, ok_cost=0.0, used=None):
    from zswarm.spec import Result

    async def fake_cc(task, api_key):
        if used is not None:
            used.append((api_key, time.monotonic()))
        if api_key in keys_that_die:
            r = Result(id=task.id, backend="cc", model=task.model, status="error", error="API Error: 402 Insufficient Balance",
                       cost_usd=dead_cost, turns=2, seconds=1.0)
            r.usage["out"] = 5
            return r, {"exit": 1}
        r = Result(id=task.id, backend="cc", model=task.model, status="ok", answer="done", cost_usd=ok_cost, turns=3, seconds=2.0)
        r.usage["out"] = 7
        return r, {"exit": 0}

    return fake_cc


def test_a_cc_task_waits_for_a_rate_limited_key_instead_of_giving_up(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    from zswarm import jobs

    keys = ["sk-aaaa1111", "sk-bbbb2222"]
    used = []
    monkeypatch.setattr(jobs, "run_cc_task", _dies_on({keys[0]}, used=used))
    fc = _cc_pool_client(keys)
    fc.pool.rest(keys[1], 0.3, status=429)  # the other key is rate-limited for a moment, not out of balance

    async def go():
        m = JobManager(client=fc)
        return m, await asyncio.wait_for(m.run_batch([_cc_task(tmp_path)]), 10)

    t0 = time.monotonic()
    m, job = asyncio.run(go())
    r = job.results["t1"]
    assert r.status == "ok" and [k for k, _ in used] == [keys[0], keys[1]] and m.cc_rotations == 1, r.error
    assert used[1][1] - t0 >= 0.25  # it waited for the rest, rather than giving up as "no key left"


def test_a_cc_task_does_not_wait_hours_for_a_dead_key(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    from zswarm import jobs

    keys = ["sk-aaaa1111", "sk-bbbb2222"]
    used = []
    monkeypatch.setattr(jobs, "run_cc_task", _dies_on({keys[0]}, used=used))
    fc = _cc_pool_client(keys)
    fc.pool.rest(keys[1], 0, status=401, dead=True)  # revoked: ten minutes at least

    async def go():
        m = JobManager(client=fc)
        return m, await asyncio.wait_for(m.run_batch([_cc_task(tmp_path)]), 10)

    m, job = asyncio.run(go())
    r = job.results["t1"]
    assert r.status == "error" and [k for k, _ in used] == [keys[0]] and m.cc_rotations == 0
    assert "no key with credit left" in (r.error or "") and "wakes in" in (r.error or ""), r.error


def test_a_cc_task_never_launches_on_a_disabled_key(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    from zswarm import jobs

    keys = ["sk-aaaa1111", "sk-bbbb2222"]
    used = []
    monkeypatch.setattr(jobs, "run_cc_task", _dies_on(set(keys), used=used))
    fc = _cc_pool_client(keys)
    for k in keys:
        fc.pool.broke(k)  # every key is disabled: a run would only die of 402 and cost a process

    async def go():
        m = JobManager(client=fc)
        return m, await asyncio.wait_for(m.run_batch([_cc_task(tmp_path)]), 10)

    m, job = asyncio.run(go())
    r = job.results["t1"]
    assert used == [] and r.status == "error" and "every key in the pool is disabled" in (r.error or ""), r.error
    assert (tmp_path / "ledger.jsonl").exists()  # still journaled


def test_a_dead_runs_spend_is_kept(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    from zswarm import jobs

    keys = ["sk-aaaa1111", "sk-bbbb2222"]
    monkeypatch.setattr(jobs, "run_cc_task", _dies_on({keys[0]}, dead_cost=0.01, ok_cost=0.02))
    fc = _cc_pool_client(keys)

    async def go():
        m = JobManager(client=fc)
        return m, await asyncio.wait_for(m.run_batch([_cc_task(tmp_path)]), 10)

    m, job = asyncio.run(go())
    r = job.results["t1"]
    # The run that died on the empty key had already made calls: its spend is real and the ledger sees it.
    assert r.status == "ok" and abs((r.cost_usd or 0.0) - 0.03) < 1e-9 and r.usage["out"] == 12 and r.turns == 5, r.as_dict()
    assert abs(job.cost() - 0.03) < 1e-9
    t = json.loads((job.dir / "transcripts" / "t1.json").read_text(encoding="utf-8"))
    assert t["key_rotations"] == 1 and t["dead_runs"][0]["key"] == config.fingerprint(keys[0]) and t["dead_runs"][0]["cost_usd"] == 0.01


def test_out_of_balance_reads_the_runs_error_and_nothing_else():
    from zswarm.cc import out_of_balance
    from zswarm.spec import Result

    def r(**kw):
        return Result(id="t", backend="cc", **kw)

    assert out_of_balance(r(status="error", error='API Error: 402 {"error":{"message":"Insufficient Balance","type":"unknown_error"}}'))
    assert out_of_balance(r(status="error", error="claude exit 1: API Error: 402 Payment Required"))
    assert not out_of_balance(r(status="error", error="claude exit 1", answer="API Error: 402 Insufficient Balance"))  # the answer is the worker's
    assert not out_of_balance(r(status="error", error="FAILED: the insufficient balance test (402 path) is missing"))  # the worker's own verdict
    assert not out_of_balance(r(status="error", error="HTTP 4020 upstream, balance page"))  # not a 402
    assert not out_of_balance(r(status="ok", answer="API Error: 402 Insufficient Balance"))
    # A failed run's error is the worker's own last text when Claude Code has one: a worker talking about a
    # payment gateway's 402 has not run out of anything.
    assert not out_of_balance(r(status="error", error="Stopped: the payment gateway returned 402 payment required."))


def test_a_cc_task_that_waited_picks_again_because_the_pool_moved(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    from zswarm import jobs

    keys = ["sk-aaaa1111", "sk-bbbb2222"]
    used = []
    monkeypatch.setattr(jobs, "run_cc_task", _dies_on({keys[0]}, used=used))
    fc = _cc_pool_client(keys)
    fc.pool.rest(keys[1], 0.3, status=429)

    async def another_task_kills_it():
        await asyncio.sleep(0.1)
        fc.pool.broke(keys[1])  # a sibling cc task died of 402 on it while this one slept, disabling it

    async def go():
        m = JobManager(client=fc)
        asyncio.create_task(another_task_kills_it())
        return m, await asyncio.wait_for(m.run_batch([_cc_task(tmp_path)]), 10)

    m, job = asyncio.run(go())
    r = job.results["t1"]
    # It woke up, looked again, and did not launch on the key that was disabled meanwhile.
    assert [k for k, _ in used] == [keys[0]] and r.status == "error" and "every key in the pool is disabled" in (r.error or ""), r.error
