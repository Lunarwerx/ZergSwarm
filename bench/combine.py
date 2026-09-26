"""Merge several bench reports into one table per arm: accuracy across every suite, cost, provider
latency and the hosts that served it. Numbers come straight from the report JSONs, so a record written
from this output cannot drift from the runs it describes.

    python bench/combine.py bench/out/<mech>.json bench/out/<judg>.json [...]
    python bench/combine.py --since 20260917-0331          # every report whose run id sorts at or after it
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
RUN_ID = re.compile(r"^\d{8}-\d{6}-")


def load(paths: list[Path]) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def combine(reports: list[dict]) -> dict:
    """{arm: {passed, attempts, cost, api_s, turns, hosts, suites}} summed over every report the arm is in."""
    arms: dict = {}
    for rep in reports:
        for name, a in (rep.get("arms") or {}).items():
            d = arms.setdefault(name, {"passed": 0, "attempts": 0, "cost": 0.0, "api_s": 0.0, "turns": 0, "hosts": {}, "suites": []})
            d["passed"] += a.get("passed_total", a.get("passed", 0))
            d["attempts"] += a.get("attempts", a.get("total", 0))
            d["cost"] += a.get("cost_total_usd", a.get("cost_usd", 0.0)) or 0.0
            d["suites"].append(f"{rep.get('suite', '?')} {a.get('passed_total', a.get('passed', 0))}/{a.get('attempts', a.get('total', 0))}")
            for run in a.get("runs") or [a]:
                for r in run.get("rows") or []:
                    d["api_s"] += r.get("api_seconds") or 0.0
                    d["turns"] += r.get("turns") or 0
                    for u in r.get("upstream") or []:
                        d["hosts"][u] = d["hosts"].get(u, 0) + 1
    return arms


def render(arms: dict) -> str:
    lines = ["| arm | passed | rate | per suite | cost $ | api s/call | served by |", "|---|---|---|---|---|---|---|"]
    for name, d in sorted(arms.items(), key=lambda kv: (-kv[1]["passed"] / max(kv[1]["attempts"], 1), kv[1]["cost"])):
        rate = d["passed"] / d["attempts"] if d["attempts"] else 0.0
        api = f"{d['api_s'] / d['turns']:.2f}" if d["turns"] else "-"
        hosts = ", ".join(f"{k} x{v}" for k, v in sorted(d["hosts"].items(), key=lambda kv: -kv[1])) or "direct"
        lines.append(f"| {name} | {d['passed']}/{d['attempts']} | {rate:.1%} | {'; '.join(d['suites'])} | {d['cost']:.4f} | {api} | {hosts} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reports", nargs="*", type=Path)
    ap.add_argument("--since", help="include every report in bench/out whose run id sorts at or after this prefix")
    a = ap.parse_args(argv)
    paths = list(a.reports)
    if a.since:
        # Only files named like a bench run id: the Claude-arm reports (external.py, regrade.py) are named
        # otherwise, sort AFTER every digit, and a bare `>=` swept them into a table they do not belong in.
        paths += sorted(p for p in OUT.glob("*.json") if RUN_ID.match(p.stem) and p.stem >= a.since)
    if not paths:
        raise SystemExit("bench/combine.py: name report files, or --since <run-id prefix>")
    print(render(combine(load(paths))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
