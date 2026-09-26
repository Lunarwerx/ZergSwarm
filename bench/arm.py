"""One benchmark arm's mechanics: fresh fixture copies per task, grading against computed truth,
and the standalone concurrency burst probe."""
from __future__ import annotations

import asyncio
import re
import shutil
import statistics
import sys
import time
from pathlib import Path

from zswarm.client import DeepSeekClient
from zswarm.spec import Task
from zswarm.trace import load_calls, score

# Anything matching this is a limit or login failure of the ARM, not a wrong answer: reported BLOCKED, never FAIL.
BLOCKED_RX = re.compile(r"rate.?limit|usage limit|hit your limit|\b429\b|\b529\b|overloaded|out of extra usage|authenticat|oauth|not logged in", re.I)


def specs(tasks, fixture_root: Path, run_dir: Path, backend: str, model: str, effort: str | None, system: str | None = None) -> list[Task]:
    """One fresh fixture copy per task, so edits never collide. `system` is an instruction-file arm's text
    (bench/run.py --instructions), appended to the worker system prompt on both backends."""
    out = []
    for t in tasks:
        d = run_dir / t.id
        shutil.copytree(fixture_root / t.subdir if t.subdir else fixture_root, d)
        # route=False is load-bearing for a benchmark: price routing would silently move an arm named
        # "deepseek-flash" to whichever provider is cheaper this hour, and the A/B would compare one
        # provider against itself. An arm measures the model it names, or it measures nothing.
        # isolated: a hook or MCP server that fires on every arm makes each arm secretly run it too. The fixture
        # copy is the arm's own throwaway folder, which is what confirm_write vouches for on a write task.
        spec = {"id": t.id, "prompt": t.prompt, "cwd": str(d), "tools": t.tools, "schema": t.schema, "backend": backend, "model": model, "max_turns": t.max_turns, "timeout_s": 900, "route": False,
                "isolated": True, "confirm_write": True}
        if system:
            spec["system"] = system
        if effort == "off":
            spec["thinking"] = False
        elif effort:
            spec.update({"reasoning_effort": effort, "max_cost_usd": 0.5})  # high/max may legitimately spend more than the default cap
        out.append(Task.from_dict(spec))
    return out


def grade_rows(tasks, job, truth, run_dir: Path, arm: str) -> tuple[list[dict], int]:
    rows, passed = [], 0
    for t in tasks:
        r = job.results[t.id]
        ok, detail = t.grade(r.as_dict(), truth, run_dir / t.id)
        # A worker that answered "FAILED: ..." used the prescribed refusal; the grader decides whether that was right.
        gradable = r.status == "ok" or (r.status == "error" and (r.answer or "").startswith("FAILED:"))
        ok = ok and gradable
        blocked = (not ok) and bool(BLOCKED_RX.search((r.error or "") + (r.answer or "")))
        passed += ok
        # The trace score rides beside the answer grade: right-by-luck and right-by-reading are different arms.
        tools_ok, tools_detail = score(load_calls(job.dir, t.id), t.expect) if getattr(t, "expect", None) else (None, "")
        rows.append({"task": t.id, "pass": ok, "blocked": blocked, "tools_ok": tools_ok, "tools_detail": tools_detail, "status": r.status, "seconds": r.seconds, "api_seconds": r.api_seconds,
                     "upstream": list(r.upstream), "cost_usd": r.cost_usd, "turns": r.turns, "tool_calls": r.tool_calls,
                     "reasoning_tokens": (r.usage or {}).get("reasoning", 0), "detail": detail, "answer": (r.answer or "")[:300], "error": (r.error or "")[:300]})
        who = ("/".join(r.upstream))[:18]
        print(f"  {arm:28s} {t.id:16s} {'PASS' if ok else ('BLOCKED' if blocked else 'FAIL')} {'' if tools_ok is None else ('tools-ok ' if tools_ok else 'TOOLS-BAD')} {r.seconds:6.1f}s api{r.api_seconds:6.1f}s ${(r.cost_usd or 0):.5f} {who:18s} {detail[:70]}", file=sys.stderr)
    return rows, passed


async def burst(n: int, model: str) -> dict:
    """N concurrent one-shot calls after one pilot (so the shared prefix is cached): wall, latency spread, errors, cost."""
    async with DeepSeekClient() as c:
        async def one(i: int):
            t = time.perf_counter()
            try:
                r = await c.chat([{"role": "system", "content": "You are a terse assistant. " * 40}, {"role": "user", "content": f"Reply with the single word OK. ({i})"}], model=model, max_tokens=5, thinking=False)
                return ("ok", time.perf_counter() - t, r.usage.hit, r.usage.miss, r.cost_usd)
            except Exception as e:  # noqa: BLE001 - an error IS the measurement here
                return (type(e).__name__, time.perf_counter() - t, 0, 0, 0.0)
        pilot = await one(-1)
        t0 = time.perf_counter()
        res = await asyncio.gather(*[one(i) for i in range(n)])
        wall = time.perf_counter() - t0
    lats = sorted(r[1] for r in res)
    codes: dict[str, int] = {}
    for r in res:
        codes[r[0]] = codes.get(r[0], 0) + 1
    return {
        "n": n, "model": model, "wall_s": round(wall, 2), "codes": codes, "pilot_s": round(pilot[1], 2),
        "p50_s": round(statistics.median(lats), 2), "p95_s": round(lats[int(len(lats) * 0.95) - 1], 2), "max_s": round(lats[-1], 2),
        "cache_hit_tokens": sum(r[2] for r in res), "cache_miss_tokens": sum(r[3] for r in res), "cost_usd": round(sum(r[4] for r in res), 6),
        "retries": c.retries, "calls": c.calls,
    }
