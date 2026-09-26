"""Decision benchmark: TypeSafe's Jev (typed System One answers) against the swarm's generative models.

Every arm answers the SAME frozen items (bench/decisions/data/*.jsonl, built by bench/decisions/build.py):
Jev natively through POST /v1/systemone, a generative model through the swarm's own tool-free call with the
state, question and options rendered as text and a `FINAL: <option>` line to grade. Rendering, parsing and the
Jev client are zswarm's own (zswarm/decisions.py, zswarm/typesafe.py), so `zswarm_decide` asks exactly what this
measured. Every answer is written to the results DB (bench/results/) as it lands, and an (item, rep) the DB
already holds for this suite version is reused, not re-asked - so a re-run after a crash, or with one new arm,
costs only the gap.

    python bench/decide.py --arms jev,groq-gpt-oss-120b@low,gemini-3.8-flash@low --suites all
    python bench/decide.py --arms jev,jev#b5 --suites triage,banking77 --reps 2   # twice: deterministic? batched?
    python bench/decide.py --report --arms jev,groq-gpt-oss-120b@low            # report from the DB, no calls

Arm spec: `jev` (= jev-latest), `jev-1.13.0`, `jev#b5` (5 items per Jev call), a Simple Jev model on Featherless's
keyless demo (`featherless-ai/Qwen3.8-27B-classifier`; the ids come from its GET /v1/models; 2 requests/second
shared, 2k tokens per question), or any swarm model name with an optional `@effort`.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from zswarm import benchdb as db  # noqa: E402
from zswarm import typesafe  # noqa: E402
from zswarm.decisions import SYSTEM, batched_question, jev_question, options, pack, parse_final, read_answer, render  # noqa: E402,F401

DATA = ROOT / "decisions" / "data"
# Bump when the way an item is rendered for either kind of arm changes: a new rendering is a new test.
RENDER_V = "1"


def load_suite(name: str) -> list[dict]:
    return [json.loads(line) for line in (DATA / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def all_suites() -> list[str]:
    order = ["triage", "commit_match", "bug_seeded", "doc_to_code", "banking77", "boolq", "anli", "hard_choice"]
    have = {p.stem for p in DATA.glob("*.jsonl")}
    return [s for s in order if s in have] + sorted(have - set(order))


def suite_version(items: list[dict]) -> str:
    return db.version_of({"render": RENDER_V, "system": SYSTEM, "items": items})


def gold_key(item: dict) -> str:
    if item["type"] == "noul":
        return "yes" if item["gold"] else "no"
    return str(item["gold"])


# ---------------------------------------------------------------- generative arms

async def ask_llm(mgr, model: str, effort: str | None, item: dict, sem: asyncio.Semaphore) -> dict:
    keys = [k for k, _ in options(item)]
    last = {"status": "error", "error": ""}
    for attempt in range(6):
        async with sem:
            try:
                r = await mgr.ask_routed(render(item), model, route=False, system=SYSTEM, reasoning_effort=effort, max_tokens=8000)
            except Exception as e:  # noqa: BLE001 - a crash is an ungraded row, never a wrong answer
                last = {"status": "error", "error": f"{type(e).__name__}: {e}"[:200]}
                r = None
        if r is not None and r.status == "ok":
            pred = parse_final(r.answer or "", keys)
            u = r.usage or {}
            return {"status": "ok", "pred": pred, "parsed": pred is not None, "answer": (r.answer or "")[-200:], "cost": r.cost_usd or 0.0, "s": r.seconds,
                    "in": (u.get("in_hit", 0) or 0) + (u.get("in_miss", 0) or 0), "out": u.get("out", 0), "upstream": list(r.upstream or [])}
        if r is not None:
            last = {"status": "error", "error": (r.error or r.status or "")[:200]}
        await asyncio.sleep(min(3 * 2 ** attempt, 60))
    return last


# ---------------------------------------------------------------- Jev

async def ask_jev(jev: typesafe.Jev, model: str, item: dict) -> dict:
    res = await jev.ask(item["state"], {"q": jev_question(item)}, model=model)
    if res["status"] != "ok":
        return res
    out = {"status": "ok", "s": round(res["secs"], 3), "in": res["in"], "out": res["out"], "cost": res["cost_usd"], "resolved": res["model"]}
    out.update(read_answer(item, res["answers"]["q"]))
    return out


async def ask_jev_batch(jev: typesafe.Jev, model: str, items: list[dict]) -> list[dict]:
    """Jev's own pattern ("speculative fan-out"): ONE call carries many questions against one state. Here the state
    is the list of the items' states and question i asks about `items[i]`, so N swarm decisions cost one request.
    Cost and latency are split evenly over the batch; `batch_n`/`batch_s` keep the per-call truth."""
    res = await jev.ask({"items": [it["state"] for it in items]}, {f"q{i}": batched_question(it, i) for i, it in enumerate(items)}, model=model)
    if res["status"] != "ok":
        return [dict(res) for _ in items]
    n = len(items)
    out = []
    for i, it in enumerate(items):
        a = res["answers"].get(f"q{i}")
        if a is None:
            out.append({"status": "error", "error": f"no answer for q{i} in a batch of {n}"})
            continue
        r = {"status": "ok", "s": round(res["secs"] / n, 4), "in": res["in"] / n, "out": res["out"] / n, "cost": res["cost_usd"] / n,
             "resolved": res["model"], "batch_n": n, "batch_s": round(res["secs"], 3)}
        r.update(read_answer(it, a))
        out.append(r)
    return out


# ---------------------------------------------------------------- the run

def parse_arm(spec: str) -> tuple[str, str, str | None, bool]:
    """(label, model, effort, is_jev). `jev#b20` is Jev answering up to 20 items in ONE call (see ask_jev_batch)."""
    spec = spec.strip()
    if is_typed(spec):
        model, _, batch = spec.partition("#")
        model = "jev-latest" if model == "jev" else model
        return model + (f"#{batch}" if batch else ""), model, None, True
    model, effort = (spec.split("@", 1) + [None])[:2]
    return spec, model, effort, False


def is_typed(label: str) -> bool:
    """A typed-answer arm (Jev or Simple Jev, with probabilities and a confidence), as opposed to a generative one."""
    return typesafe.is_typed_model(label.partition("#")[0])


def short(label: str) -> str:
    return label.removeprefix(typesafe.SIMPLE_JEV_PREFIX).replace("-classifier", "")


def batch_size(label: str) -> int:
    m = re.search(r"#b(\d+)$", label)
    return int(m.group(1)) if m else 1


def to_row(run: str, suite: str, v: str, label: str, model: str, effort: str | None, item: dict, rep: int, res: dict) -> dict:
    g = gold_key(item)
    ok = res.get("status") == "ok"
    row = {"run": run, "suite": f"decisions.{suite}", "v": v, "arm": label, "model": model, "effort": effort, "item": item["id"], "rep": rep,
           "pass": (res.get("pred") == g) if ok else None, "status": "ok" if ok else "error", "gold": g, "pred": res.get("pred"),
           "parsed": res.get("parsed"), "cost": res.get("cost", 0.0), "s": res.get("s"), "in": res.get("in"), "out": res.get("out")}
    if "probs" in res:
        row["p_gold"] = round(float(res["probs"].get(g, 0.0)), 4)
        row["conf"] = None if res.get("conf") is None else round(float(res["conf"]), 4)
        row["resolved"] = res.get("resolved")
        if res.get("raw") is not None:
            row["raw"] = res["raw"]
        for k in ("batch_n", "batch_s"):
            if k in res:
                row[k] = res[k]
    if not ok:
        row["error"] = res.get("error", "")[:200]
    else:
        row["answer"] = res.get("answer", "")[:200] if "answer" in res else None
        if res.get("upstream"):
            row["upstream"] = res["upstream"]
    if item.get("meta", {}).get("cat"):
        row["cat"] = item["meta"]["cat"]
    return row


async def _ask_arm_head(mgr, jevs, sems, counts, run_id, arm, suite, item, rep, v) -> None:
    label, model, effort, is_jev = arm
    if is_jev:
        res = await ask_jev(jevs[label], model, item)
    else:
        res = await ask_llm(mgr, model, effort, item, sems[label])
    db.record_rows([to_row(run_id, suite, v, label, model, effort, item, rep, res)])
    counts[label][0] += 1
    counts[label][2] += res.get("status") != "ok"


async def _ask_arm_batch(jevs, counts, run_id, arm, suite, group, rep, v) -> None:
    label, model, effort, _ = arm
    results = await ask_jev_batch(jevs[label], model, group)
    db.record_rows([to_row(run_id, suite, v, label, model, effort, it, rep, res) for it, res in zip(group, results)])
    counts[label][0] += len(group)
    counts[label][2] += sum(r.get("status") != "ok" for r in results)


def _todo_jobs(arm, todo, rep, suite, v, mgr, jevs, sems, counts, run_id) -> list:
    size = batch_size(arm[0])
    if arm[3] and size > 1:
        return [functools.partial(_ask_arm_batch, jevs, counts, run_id, arm, suite, g, rep, v) for g in pack(todo, size)]
    return [functools.partial(_ask_arm_head, mgr, jevs, sems, counts, run_id, arm, suite, it, rep, v) for it in todo]


def _arm_suite_jobs(arm, suite, v, items, counts, args, mgr, jevs, sems, run_id) -> list:
    reps = args.reps if (arm[3] or args.llm_reps is None) else args.llm_reps
    have = {} if args.fresh else db.cached(f"decisions.{suite}", v, arm[0], args.max_age_days)
    jobs = []
    for rep in range(reps):
        todo = [it for it in items if (it["id"], rep) not in have]
        counts[arm[0]][1] += len(items) - len(todo)
        jobs += _todo_jobs(arm, todo, rep, suite, v, mgr, jevs, sems, counts, run_id)
    return jobs


def _plan_jobs(arms, suites, counts, args, versions, mgr, jevs, sems, run_id) -> list:
    jobs = []
    for suite in suites:
        items, v = load_suite(suite), versions[suite]
        for arm in arms:
            jobs += _arm_suite_jobs(arm, suite, v, items, counts, args, mgr, jevs, sems, run_id)
    return jobs


def _decide_suites(args) -> list[str]:
    if args.suites == "all":
        return all_suites()
    return [s.strip() for s in args.suites.split(",") if s.strip()]


def _decide_arms(args) -> list[tuple[str, str, str | None, bool]]:
    return [parse_arm(a) for a in args.arms.split(",") if a.strip()]


async def _open_jevs(arms, concurrency: int) -> dict:
    jevs = {a[0]: typesafe.Jev.for_model(a[1], concurrency=concurrency) for a in arms if a[3]}
    for j in jevs.values():
        await j.__aenter__()
    return jevs


async def _close_jevs(jevs: dict, mgr, suites, versions: dict, arms, run_id: str, started: str) -> None:
    for j in jevs.values():
        await j.__aexit__(None, None, None)
    if mgr:
        await mgr.aclose()
    for suite in suites:
        db.record_run(run_id, f"decisions.{suite}", versions[suite], [a[0] for a in arms], started=started, items=len(load_suite(suite)))


def _print_counts(counts: dict) -> None:
    for label, (asked, reused, failed) in counts.items():
        print(f"[decide] {label}: asked {asked}, reused {reused}, ungraded {failed}", file=sys.stderr)


async def run(args) -> str:
    from zswarm.jobs import JobManager

    suites = _decide_suites(args)
    arms = _decide_arms(args)
    run_id, started = db.new_run_id("decisions"), db.now_iso()
    mgr = JobManager() if any(not a[3] for a in arms) else None
    # One Jev client (its own key rotation and concurrency) per Jev arm; a plain semaphore per generative arm.
    jevs = await _open_jevs(arms, args.jev_concurrency)
    sems = {a[0]: asyncio.Semaphore(args.concurrency) for a in arms if not a[3]}
    counts = {a[0]: [0, 0, 0] for a in arms}  # asked, reused, failed
    versions = {s: suite_version(load_suite(s)) for s in suites}
    jobs = _plan_jobs(arms, suites, counts, args, versions, mgr, jevs, sems, run_id)
    print(f"[decide] {len(jobs)} calls to make, {sum(c[1] for c in counts.values())} reused from the results DB", file=sys.stderr, flush=True)
    t0 = time.perf_counter()
    try:
        await asyncio.gather(*(job() for job in jobs))  # jobs are partials: a coroutine is only made when it runs
    finally:
        await _close_jevs(jevs, mgr, suites, versions, arms, run_id, started)
    _print_counts(counts)
    print(f"[decide] wall {time.perf_counter() - t0:.0f}s", file=sys.stderr)
    return run_id


# ---------------------------------------------------------------- the report, always from the DB

def _rows_for(suite: str, arm: str, v: str) -> list[dict]:
    newest: dict[tuple, dict] = {}
    for r in db.rows(f"decisions.{suite}", arm, v):
        k = (r["item"], r.get("rep", 0))
        if r.get("status") == "ok" and (k not in newest or r["ts"] > newest[k]["ts"]):
            newest[k] = r
    return list(newest.values())


def _suite_arm_row(rs: list[dict], n_items: int) -> dict:
    n = len(rs)
    return {"n": n, "of": n_items, "acc": sum(bool(r["pass"]) for r in rs) / n,
            "unparsed": sum(r.get("parsed") is False for r in rs), "cost": sum(r.get("cost") or 0 for r in rs),
            "p50_s": statistics.median([r["s"] for r in rs if r.get("s") is not None] or [0])}


def _suite_table(s: str, arms: list[str]) -> dict:
    items = load_suite(s)
    v = suite_version(items)
    table = {}
    for a in arms:
        rs = [r for r in _rows_for(s, a, v) if r.get("rep", 0) == 0]
        if rs:
            table[a] = _suite_arm_row(rs, len(items))
    return table


def _overall_row(per: list[dict]) -> dict:
    items = sum(x["n"] for x in per)
    cost = sum(x["cost"] for x in per)
    return {"suites": len(per), "macro_acc": statistics.mean(x["acc"] for x in per), "items": items,
            "micro_acc": sum(x["acc"] * x["n"] for x in per) / items, "cost": cost, "cost_per_item": cost / items,
            "p50_s": statistics.median([x["p50_s"] for x in per])}


def _overall_for(a: str, tables: dict) -> dict | None:
    per = [tables[s][a] for s in tables if a in tables[s]]
    if not per:
        return None
    return _overall_row(per)


def report(arm_specs: list[str], suites: list[str]) -> dict:
    arms = [parse_arm(a)[0] for a in arm_specs]
    tables = {s: _suite_table(s, arms) for s in suites}
    overall = {}
    for a in arms:
        row = _overall_for(a, tables)
        if row is not None:
            overall[a] = row
    others = [a for a in arms if not is_typed(a)]
    jev = {a: jev_analysis(a, suites, others) for a in arms if is_typed(a)}
    return {"suites": tables, "overall": overall, "jev": jev}


def _rows_by_suite(jev: str, suites: list[str]) -> dict:
    rows_by_suite = {}
    for s in suites:
        v = suite_version(load_suite(s))
        rows_by_suite[s] = (v, _rows_for(s, jev, v))
    return rows_by_suite


def _conf_deciles(zero: list[dict]) -> dict:
    bins: dict[int, list] = {}
    for r in zero:
        conf = r.get("conf")
        if conf is None:
            continue
        bins.setdefault(min(9, int(conf * 10)), []).append(bool(r["pass"]))
    return {f"{k/10:.1f}-{(k+1)/10:.1f}": {"n": len(v), "acc": round(sum(v) / len(v), 3)} for k, v in sorted(bins.items())}


def _gated(zero: list[dict]) -> dict:
    gated = {}
    for t in (0.0, 0.5, 0.7, 0.8, 0.9, 0.95):
        kept = [r for r in zero if (r.get("conf") or 0) >= t]
        acc = round(sum(bool(r["pass"]) for r in kept) / len(kept), 3) if kept else None
        gated[f">={t}"] = {"coverage": round(len(kept) / len(zero), 3), "acc": acc}
    return gated


def _cascade_pairs(o: str, suites: list[str], zero: list[dict], rows_by_suite: dict) -> list[tuple]:
    orow = {}
    for s in suites:
        v = rows_by_suite[s][0]
        for r in _rows_for(s, o, v):
            if r.get("rep", 0) == 0:
                orow[(s, r["item"])] = r
    pairs = [(r, orow.get((r["suite"].split(".", 1)[1], r["item"]))) for r in zero]
    return [(j, g) for j, g in pairs if g is not None]


def _curve_point(pairs: list[tuple], t: float) -> dict:
    acc = esc = cost = 0.0
    for j, g in pairs:
        cost += j.get("cost") or 0
        if (j.get("conf") or 0) >= t:
            acc += bool(j["pass"])
            continue
        acc += bool(g["pass"])
        esc += 1
        cost += g.get("cost") or 0
    n = len(pairs)
    return {"acc": round(acc / n, 3), "escalated": round(esc / n, 3), "cost": round(cost, 5)}


def _cascade_row(pairs: list[tuple]) -> dict:
    curve = {f">={t}": _curve_point(pairs, t) for t in (0.5, 0.7, 0.8, 0.9, 0.95)}
    return {"n": len(pairs), "jev_alone": round(sum(bool(j["pass"]) for j, _ in pairs) / len(pairs), 3),
            "other_alone": round(sum(bool(g["pass"]) for _, g in pairs) / len(pairs), 3),
            "other_cost": round(sum(g.get("cost") or 0 for _, g in pairs), 5),
            "jev_cost": round(sum(j.get("cost") or 0 for j, _ in pairs), 5), "curve": curve}


def _determinism_pairs(suites: list[str], rows_by_suite: dict) -> list[dict]:
    by_item: dict[tuple, dict] = {}
    for s in suites:
        for r in rows_by_suite[s][1]:
            by_item.setdefault((s, r["item"]), {})[r.get("rep", 0)] = r
    return [d for d in by_item.values() if 0 in d and 1 in d]


def _determinism(two: list[dict]) -> dict:
    return {"pairs": len(two), "same_pred": round(sum(d[0]["pred"] == d[1]["pred"] for d in two) / len(two), 4),
            "max_abs_p_gold_diff": round(max(abs((d[0].get("p_gold") or 0) - (d[1].get("p_gold") or 0)) for d in two), 4)}


def jev_analysis(jev: str, suites: list[str], others: list[str]) -> dict:
    """Calibration, confidence-gated accuracy, a Jev-first cascade against each generative arm, and determinism."""
    res: dict = {"calibration": {}, "gated": {}, "cascade": {}, "determinism": {}}
    rows_by_suite = _rows_by_suite(jev, suites)
    zero = [r for s in suites for r in rows_by_suite[s][1] if r.get("rep", 0) == 0 and r.get("p_gold") is not None]
    if not zero:
        return res
    # Brier on the gold option and a 10-bin reliability over the PREDICTED option's probability.
    res["calibration"]["brier_gold"] = statistics.mean((1 - r["p_gold"]) ** 2 for r in zero)
    res["calibration"]["by_conf_decile"] = _conf_deciles(zero)
    res["gated"] = _gated(zero)
    for o in others:
        pairs = _cascade_pairs(o, suites, zero, rows_by_suite)
        if pairs:
            res["cascade"][o] = _cascade_row(pairs)
    two = _determinism_pairs(suites, rows_by_suite)
    if two:
        res["determinism"] = _determinism(two)
    return res


def print_report(rep: dict, arm_specs: list[str], suites: list[str]) -> None:
    arms = [parse_arm(a)[0] for a in arm_specs]
    w = 12
    print("\nACCURACY by suite (rep 0; n graded / items)")
    print(f"{'suite':14s}" + "".join(f"{short(a)[:w]:>{w + 1}s}" for a in arms))
    for s in suites:
        line = f"{s:14s}"
        for a in arms:
            x = rep["suites"][s].get(a)
            cell = f'{100 * x["acc"]:.0f}% {x["n"]}' if x else "-"
            line += f"{cell:>{w + 1}s}"
        print(line)
    print("-" * (14 + (w + 1) * len(arms)))
    for key, fmt in (("macro_acc", lambda x: f"{100 * x:.1f}%"), ("micro_acc", lambda x: f"{100 * x:.1f}%"), ("cost_per_item", lambda x: f"${x:.6f}"), ("p50_s", lambda x: f"{x:.2f}s")):
        line = f"{key:14s}"
        for a in arms:
            o = rep["overall"].get(a)
            line += f"{(fmt(o[key]) if o else '-'):>{w + 1}s}"
        print(line)
    for j, an in rep["jev"].items():
        print(f"\n{j}: calibration {json.dumps(an.get('calibration'))}")
        print(f"{j}: confidence-gated {json.dumps(an.get('gated'))}")
        for o, c in an.get("cascade", {}).items():
            print(f"{j} -> {o}: {json.dumps(c)}")
        if an.get("determinism"):
            print(f"{j}: determinism {json.dumps(an['determinism'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", required=True)
    ap.add_argument("--suites", default="all")
    ap.add_argument("--reps", type=int, default=1, help="repeats per item (Jev arms, and generative arms unless --llm-reps)")
    ap.add_argument("--llm-reps", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=12, help="per generative arm")
    ap.add_argument("--jev-concurrency", type=int, default=16)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--max-age-days", type=float, default=30.0)
    ap.add_argument("--report", action="store_true", help="only print the report from the results DB")
    ap.add_argument("--json", help="also write the report JSON here")
    a = ap.parse_args(argv)
    suites = all_suites() if a.suites == "all" else [s.strip() for s in a.suites.split(",") if s.strip()]
    arm_specs = [x for x in a.arms.split(",") if x.strip()]
    if not a.report:
        asyncio.run(run(a))
    rep = report(arm_specs, suites)
    print_report(rep, arm_specs, suites)
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
