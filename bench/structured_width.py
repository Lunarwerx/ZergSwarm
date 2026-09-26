"""Live: structured work at width, and structured work on every route. Both cases are from 2026-09-25.

    python bench/structured_width.py width [--n 20] [--width 24]
        N schema'd read-only tasks on profile `general` at concurrency W, through the real JobManager. PASS when
        every task is ok and its `data` matches the file it read. Jobs 20260925-143635-e3f6 / -145240-b39d / -150040-3e37:
        23 such tasks came back 23/23 errored (dead DeepSeek legs ate the timeout, then Gemini 429'd every task).

    python bench/structured_width.py routes [--rows 24]
        One schema'd, tool-free translation on EVERY evaluated route of profile `general` with a live key. PASS per
        route when the answer is ok and carries at least the rows asked for. Jobs 20260925-143537-e2e8 / -143658-29b0:
        the Gemini route answered 140-row translations with `{"rows": []}` and a `dummy` row, reported ok.

Exit 0 when every case passes, 1 otherwise; prints one JSON report. Costs real (small) money on paid routes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zswarm import agent, dispatch, selection  # noqa: E402
from zswarm.jobs import JobManager  # noqa: E402
from zswarm.spec import Task  # noqa: E402

WORDS = ["amber", "birch", "cedar", "delta", "ember", "fjord", "grove", "heron", "iris", "juniper"]
FILE_SCHEMA = {"type": "object", "required": ["first_word", "lines"], "properties": {
    "first_word": {"type": "string", "minLength": 1}, "lines": {"type": "integer", "minimum": 1}}}
KEYS = ["save", "cancel", "open", "close", "delete", "rename", "share", "print", "undo", "redo", "search", "help"]
LOCALES = ["fr", "de", "es", "it", "pt", "nl", "sv", "pl", "tr", "ja"]


def _fixture(root: Path, n: int) -> dict[str, dict]:
    truth = {}
    for i in range(n):
        lines = 3 + i % 7
        first = WORDS[i % len(WORDS)]
        body = "\n".join([f"{first} is the first word of note {i}."] + [f"filler line {j}" for j in range(1, lines)])
        (root / f"note{i:02d}.txt").write_text(body + "\n", encoding="utf-8")
        truth[f"t{i:02d}"] = {"first_word": first, "lines": lines}
    return truth


async def width(n: int, w: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="zswarm-width-") as tmp:
        root = Path(tmp)
        truth = _fixture(root, n)
        tasks = []
        for tid in truth:
            tasks.append(Task.from_dict({"id": tid, "prompt": f"Read note{tid[1:]}.txt. Report its first word (no punctuation) "
                                                              "and how many lines it has.",
                                         "cwd": str(root), "tools": "read", "schema": FILE_SCHEMA, "timeout_s": 420,
                                         "profile": "general"}))
        mgr = JobManager()
        outlook = dispatch.route_outlook(tasks, mgr._gates)
        t0 = time.monotonic()
        job = await mgr.run_batch(tasks, concurrency=w, label="bench:structured-width")
        await mgr.aclose()
    rows = {tid: r for tid, r in job.results.items()}
    wrong = {tid: (r.status, (r.error or "")[:160], r.data) for tid, r in rows.items()
             if r.status != "ok" or not r.data or r.data.get("first_word", "").strip(".").lower() != truth[tid]["first_word"]
             or r.data.get("lines") != truth[tid]["lines"]}
    return {"case": "width", "job": job.id, "n": n, "width": w, "seconds": round(time.monotonic() - t0, 1),
            "ok": n - len(wrong), "route_outlook": outlook, "models": sorted({r.model for r in rows.values()}),
            "cost_usd": job.cost(), "wrong": wrong, "pass": not wrong}


async def routes(rows: int) -> dict:
    keys = KEYS[: max(1, rows // 2)]
    locales = LOCALES[:2] if rows >= 2 else LOCALES[:1]
    want = len(keys) * len(locales)
    schema = {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "minItems": want, "items": {
        "type": "object", "required": ["locale", "key", "text"],
        "properties": {"locale": {"type": "string"}, "key": {"type": "string"}, "text": {"type": "string", "minLength": 1}}}}}}
    prompt = (f"Translate each UI label into {', '.join(locales)}. Labels (key = English): "
              + ", ".join(f"{k} = {k.capitalize()}" for k in keys)
              + f". Return one row per locale and key ({want} rows) via submit_result.")
    plan = selection.plan("general", tools="none", usable=dispatch._alive, min_context=0)
    mgr = JobManager()
    out = []
    for c in plan["candidates"]:
        res = await agent.ask(mgr.client_for(c["model"]), prompt, model=c["model"], schema=schema,
                              reasoning_effort=c.get("reasoning_effort"), thinking=c.get("thinking"), max_tokens=8000)
        got = len((res.data or {}).get("rows") or []) if isinstance(res.data, dict) else 0
        out.append({"model": c["model"], "status": res.status, "rows": got, "want": want,
                    "error": (res.error or "")[:200], "pass": res.status == "ok" and got >= want})
    await mgr.aclose()
    return {"case": "routes", "routes": out, "pass": bool(out) and all(r["pass"] for r in out)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="case", required=True)
    w = sub.add_parser("width")
    w.add_argument("--n", type=int, default=20)
    w.add_argument("--width", type=int, default=24)
    r = sub.add_parser("routes")
    r.add_argument("--rows", type=int, default=24)
    a = ap.parse_args()
    report = asyncio.run(width(a.n, a.width) if a.case == "width" else routes(a.rows))
    print(json.dumps(report, indent=1, default=str))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
