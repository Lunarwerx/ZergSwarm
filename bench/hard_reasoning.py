"""Hard-reasoning A/B: trap-laden problems with unambiguous answers, tool-free.

These are chosen so a model that pattern-matches instead of reasoning lands on a specific WRONG
answer (the trap). That is the "intelligence on hard tasks" signal, separate from the swarm suites
which top out too easily to see a ceiling. Grader is exact/normalized/numeric match on the FINAL line.

    python bench/hard_reasoning.py --arms deepseek-flash,gemini-3.8-flash@high,gemini-3.8-flash@low,gemini-3.7-flash --repeats 3
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from zswarm.jobs import JobManager  # noqa: E402
from zswarm import benchdb as db  # noqa: E402

# id, prompt, accepted normalized strings, trap (the seductive wrong answer, for reporting)
PROBLEMS = [
    ("bat_ball", "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How many cents does the ball cost?", ["5", "5 cents", "$0.05", "0.05"], "10"),
    ("machines", "If 5 machines take 5 minutes to make 5 widgets, how many minutes do 100 machines take to make 100 widgets?", ["5", "5 minutes"], "100"),
    ("lilypad", "A patch of lily pads doubles in size every day and covers the whole lake on day 48. On which day was the lake exactly half covered?", ["47", "day 47"], "24"),
    ("handshakes", "At a party every person shakes hands exactly once with every other person. There were 66 handshakes. How many people were at the party?", ["12"], "66"),
    ("avg_speed", "A car travels 60 miles in 1.5 hours, then 30 miles in 0.5 hours. What is its average speed in mph over the whole trip?", ["45"], "50"),
    ("two_children", "A family has exactly two children. You are told at least one of them is a boy. What is the probability that both children are boys? Give a reduced fraction.", ["1/3"], "1/2"),
    ("marbles", "A bag has 3 red and 2 blue marbles. You draw 2 at random without replacement. What is the probability both are red? Give a reduced fraction.", ["3/10"], "9/25"),
    ("day_of_week", "Today is Wednesday. What day of the week will it be exactly 100 days from now?", ["friday"], "wednesday"),
    ("syllogism", "All bloops are razzies. All razzies are lazzies. Some lazzies are not bloops. Does it NECESSARILY follow that some razzies are not bloops? Answer yes or no.", ["no"], "yes"),
    ("strawberry_r", "How many times does the letter r appear in the word strawberry?", ["3"], "2"),
    ("clock_angle", "What is the smaller angle, in degrees, between the hour and minute hands of a clock at exactly 3:15? Give the number.", ["7.5", "7.5 degrees"], "0"),
    ("sequence", "What number comes next in this sequence: 2, 6, 12, 20, 30, ?", ["42"], "40"),
    ("painters", "If 3 painters can paint a house in 4 hours, how many hours will 6 painters take to paint the same house at the same rate?", ["2", "2 hours"], "8"),
    ("discount", "A shirt is discounted 20%, and then the reduced price is discounted a further 20%. What is the single overall percentage discount from the original price? Give the number.", ["36", "36%"], "40"),
    ("ages", "Ann is twice as old as Bob was when Ann was as old as Bob is now. Ann is 24. How old is Bob?", ["18"], "12"),
    ("months_28", "How many months in a year have exactly 28 days?", ["12", "all", "all 12"], "1"),
    ("current_speed", "A boat travels 30 km downstream in 2 hours and the same 30 km back upstream in 3 hours. What is the speed of the current in km/h?", ["2.5"], "5"),
]

SYSTEM = ("You are solving a hard reasoning problem. Reason carefully and step by step. "
          "Then end your reply with a line in exactly this format: 'FINAL: <answer>' where <answer> "
          "is only the answer in the form requested, nothing else on that line.")
SUITE = "hard_reasoning"
# The suite is its problems, grader inputs and system prompt: change any of them and old rows stop counting.
VERSION = db.version_of({"problems": [p[:3] for p in PROBLEMS], "system": SYSTEM})


def _extract(ans: str) -> str:
    if not ans:
        return ""
    m = list(re.finditer(r"final\s*[:\-]?\s*(.+)", ans, re.I))
    tail = m[-1].group(1) if m else ans.strip().splitlines()[-1] if ans.strip() else ""
    return tail.strip().strip(".` *").strip()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().strip().strip(".$%` *"))


def _numeq(a: str, accepted: list[str]) -> bool:
    def num(x):
        x = x.replace("$", "").replace("%", "").replace("cents", "").replace("degrees", "").replace("hours", "").replace("minutes", "").strip()
        try:
            return Fraction(x) if "/" in x else Fraction(str(float(x)))
        except (ValueError, ZeroDivisionError):
            return None
    an = num(a)
    if an is None:
        return False
    for acc in accepted:
        bn = num(acc)
        if bn is not None and (an == bn or abs(float(an) - float(bn)) <= 0.02):
            return True
    return False


def grade(raw_answer: str, accepted: list[str]) -> bool:
    got = _extract(raw_answer)
    gn = _norm(got)
    acc_norm = {_norm(a) for a in accepted}
    if gn in acc_norm:
        return True
    # also accept if the accepted token appears as a standalone token in the final line
    for a in acc_norm:
        if re.search(rf"(?<![\w/.]){re.escape(a)}(?![\w/.])", gn):
            return True
    return _numeq(got, accepted)


async def run_arm(mgr, label, model, effort, repeats, sem, run_id, cache=None):
    """Every graded answer is written to the results DB as it lands; an (item, rep) already in `cache`
    (graded, same suite version, young enough) is reused instead of re-asked."""
    cache = cache or {}
    tasks = []
    for rep in range(repeats):
        for pid, prompt, accepted, trap in PROBLEMS:
            tasks.append((rep, pid, prompt, accepted, trap))

    async def one(rep, pid, prompt, accepted, trap):
        hit = cache.get((pid, rep))
        if hit:
            return (pid, rep, bool(hit["pass"]), "cached", hit.get("answer", ""), 0.0, hit.get("s") or 0.0)
        row = await _ask(rep, pid, prompt, accepted, trap)
        db.record_rows([{"run": run_id, "suite": SUITE, "v": VERSION, "arm": label, "model": model, "effort": effort, "item": pid, "rep": rep,
                         "pass": row[2] if row[3] == "ok" else None, "status": "ok" if row[3] == "ok" else "error", "answer": str(row[4])[:200],
                         "gold": accepted[0], "trap_hit": row[3] == "ok" and grade(str(row[4]), [trap]), "cost": row[5], "s": row[6]}])
        return row

    async def _ask(rep, pid, prompt, accepted, trap):
        # Retry non-ok (429/5xx rate-limit) up to 5 times with backoff so EVERY problem gets a real
        # answer; a rate-limit error must never be counted as a wrong answer.
        last_status, last_err = "error", ""
        for attempt in range(6):
            async with sem:
                try:
                    r = await mgr.ask_routed(prompt, model, route=False, system=SYSTEM, reasoning_effort=effort)
                    if r.status == "ok":
                        ok = grade(r.answer or "", accepted)
                        return (pid, rep, ok, "ok", _extract(r.answer or ""), r.cost_usd or 0.0, r.seconds or 0.0)
                    last_status, last_err = r.status, (r.error or "")[:80]
                except Exception as e:  # noqa: BLE001
                    last_status, last_err = f"exc:{type(e).__name__}", str(e)[:80]
            await asyncio.sleep(min(2 ** attempt * 3, 45))
        return (pid, rep, False, last_status, last_err, 0.0, 0.0)

    return await asyncio.gather(*(one(*t) for t in tasks))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", required=True, help="comma list, model or model@effort (off/low/high/max)")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--fresh", action="store_true", help="re-measure even what the results DB already holds")
    ap.add_argument("--max-age-days", type=float, default=30.0, help="a DB row older than this is re-measured")
    args = ap.parse_args()

    arms = []
    for a in args.arms.split(","):
        a = a.strip()
        if not a:
            continue
        model, effort = (a.split("@", 1) + [None])[:2]
        arms.append((a, model, effort))

    sem = asyncio.Semaphore(args.concurrency)
    mgr = JobManager()
    results = {}
    run_id, started = db.new_run_id(SUITE), db.now_iso()
    try:
        for label, model, effort in arms:
            cache = {} if args.fresh else db.cached(SUITE, VERSION, label, args.max_age_days)
            rows = await run_arm(mgr, label, model, effort, args.repeats, sem, run_id, cache)
            results[label] = rows
            npass = sum(1 for r in rows if r[2])
            reused = sum(1 for r in rows if r[3] == "cached")
            print(f"[done] {label}: {npass}/{len(rows)}" + (f" ({reused} reused from the results DB)" if reused else ""), flush=True)
    finally:
        await mgr.aclose()
        db.record_run(run_id, SUITE, VERSION, [a[0] for a in arms], started=started, items=len(PROBLEMS) * args.repeats)

    n = len(PROBLEMS)
    print("\n" + "=" * 92)
    print(f"HARD REASONING  ({n} problems x {args.repeats} repeats = {n*args.repeats} per arm)")
    print("=" * 92)
    header = f"{'problem':<16}" + "".join(f"{lbl.split('@')[0][:9]+('@'+lbl.split('@')[1][:2] if '@' in lbl else ''):>16}" for lbl, *_ in arms)
    print(header)
    per_arm_pass = {lbl: 0 for lbl, *_ in arms}
    per_arm_cost = {lbl: 0.0 for lbl, *_ in arms}
    for lbl, rows in results.items():
        per_arm_cost[lbl] = sum(r[5] for r in rows)
    for pid, *_ in PROBLEMS:
        line = f"{pid:<16}"
        for lbl, *_ in arms:
            rows = [r for r in results[lbl] if r[0] == pid]
            p = sum(1 for r in rows if r[2])
            per_arm_pass[lbl] += p
            line += f"{f'{p}/{len(rows)}':>16}"
        print(line)
    print("-" * 92)
    tot = f"{'TOTAL':<16}"
    for lbl, *_ in arms:
        tot += f"{f'{per_arm_pass[lbl]}/{n*args.repeats}':>16}"
    print(tot)
    rate = f"{'rate':<16}"
    for lbl, *_ in arms:
        rate += f"{f'{100*per_arm_pass[lbl]/(n*args.repeats):.0f}%':>16}"
    print(rate)
    cost = f"{'cost $ (paid)':<16}"
    for lbl, *_ in arms:
        cost += f"{f'{per_arm_cost[lbl]:.4f}':>16}"
    print(cost)

    # error/status summary
    print("\nnon-ok statuses:")
    for lbl, rows in results.items():
        bad = {}
        for r in rows:
            if r[3] not in ("ok", "cached"):
                bad[r[3]] = bad.get(r[3], 0) + 1
        print(f"  {lbl}: {bad or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
