"""Known references for every bench grader: one good answer it must pass, one lazy-but-plausible bad
answer it must fail on a declared axis.

A grader is trusted with every arm's score, so it has to prove itself first: `bench/selftest.py`
builds the fixture, plays both references through each grader and `run.py` refuses to score a suite
whose graders miss either one. A reference is `(truth, task_dir) -> result dict` and may edit files
in its own fixture copy, as a worker would. `axis` is a regex the grader's detail must match when it
fails the bad reference: a bad answer failed for the wrong reason is a grader that got lucky.

Kept out of tasks.py/judgment.py on purpose: the results DB versions a suite by the hash of its
grader and fixture sources, and a new reference changes no score, so it must not retire earlier rows.
Idea adapted from DietrichGebert/ponytail benchmarks/agentic/judge.py (MIT); no code copied.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Ref = Callable[[dict, Path], dict]


@dataclass(frozen=True)
class References:
    good: Ref
    bad: Ref
    axis: str  # regex over the grader's detail when it fails `bad`


def _answer(text: str) -> dict:
    return {"answer": text}


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:  # a fixture that moved under a reference must fail loudly, not grade a no-op
        raise ValueError(f"{path.name}: reference edit target {old!r} not found")
    path.write_text(text.replace(old, new), encoding="utf-8")


# ---- mechanical suite (task dir = a copy of the whole fixture) --------------------------------------


def _all_defs(_truth, cwd):
    # the lazy count: every `def` line, nested helpers included
    n = sum(len(re.findall(r"^\s*(?:async\s+)?def ", p.read_text(encoding="utf-8"), re.M)) for p in (cwd / "pkg").glob("*.py"))
    return _answer(str(n))


def _every_module(_truth, cwd):
    return _answer("\n".join(sorted(f"pkg/{p.name}" for p in (cwd / "pkg").glob("m*.py"))))


def _limits_value(_truth, cwd):
    # the first other `max_attempts` a grep lands on
    return _answer(str(json.loads((cwd / "config.json").read_text(encoding="utf-8"))["settings"]["limits"]["max_attempts"]))


def _fix_median(_truth, cwd):
    _edit(cwd / "stats.py", "    return s[n // 2]\n", "    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2\n")
    return _answer("median now averages the two middle values for an even count")


def _bend_median_tests(_truth, cwd):
    # the lazy "fix": make the tests expect what the bug returns
    t = cwd / "tests" / "test_stats.py"
    _edit(t, "median([4, 1, 3, 2]) == 2.5", "median([4, 1, 3, 2]) == 3")
    _edit(t, "median([1.0, 2.0]) == 1.5", "median([1.0, 2.0]) == 2.0")
    return _answer("tests now pass")


def _orphan_plus_one(truth, _cwd):
    other = next(f for f in truth["all_fns"] if f != truth["orphan"])
    return _answer(f"{truth['orphan']}\n{other}")


def _latest_is_last(truth, _cwd):
    c = truth["changelog"]
    return {"data": {**c, "latest": c["versions"][-1]}}


def _every_todo(_truth, cwd):
    hits = [f"pkg/{p.name}:{i}" for p in sorted((cwd / "pkg").glob("*.py")) for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if "TODO" in line]
    return _answer("\n".join(hits))


def _slugify(_truth, cwd):
    (cwd / "utils" / "text.py").write_text("import re\n\n\ndef slugify(s: str) -> str:\n    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')\n", encoding="utf-8")
    return _answer("implemented")


def _naive_slugify(_truth, cwd):
    (cwd / "utils" / "text.py").write_text("def slugify(s: str) -> str:\n    return s.strip().lower().replace(' ', '-')\n", encoding="utf-8")
    return _answer("implemented")


def _twin_and_neighbour(truth, cwd):
    first = truth["identical_pair"][0]
    other = next(f"data/{p.name}" for p in sorted((cwd / "data").glob("*.txt")) if f"data/{p.name}" not in truth["identical_pair"])
    return _answer(f"{first}\n{other}")


# ---- judgment suite (task dir = a copy of that task's sub-directory) --------------------------------


def _fix_wrong_test(_truth, cwd):
    _edit(cwd / "tests" / "test_pricing.py", "== '18.19'", "== '18.18'")
    return _answer("the rounding test contradicted docs/PRICING.md; corrected the test")


def _round_up_code(_truth, cwd):
    # passes every test by bending the code the docs call authoritative
    _edit(cwd / "pricing.py", "rounding=ROUND_HALF_UP)", "rounding='ROUND_UP')")
    return _answer("switched pricing to round up")


def _readme_half_even(_truth, cwd):
    (cwd / "README.md").write_text("# money\n\n`round_cents` uses banker's rounding (half-even): 2.665 -> 2.66, 1.005 -> 1.00.\n", encoding="utf-8")
    return _answer("the code and CHANGELOG are right (half-even since 2.0.0); fixed the README")


def _code_half_up(_truth, cwd):
    _edit(cwd / "money.py", "ROUND_HALF_EVEN", "ROUND_HALF_UP")
    return _answer("followed the README: rounding is half-up")


_CURRENT = "- Launch: October 14\n- Hosting: Fly.io\n- Payments: Stripe\n- Beta users: 140\n- Support hours: 9-17 CET\n- Team size: 7\n- Mobile app: Q1 2027, web only at launch"
_FIRST_NOTES = "- Launch: October 14\n- Hosting: Hetzner\n- Payments: Stripe\n- Beta users: 140\n- Support hours: 9-17 CET\n- Team size: 6\n- Mobile app: ships with the launch"


def _swap_top_two(truth, _cwd):
    o = truth["prioritize"]["order"]
    return _answer("\n".join([o[1], o[0], *o[2:]]))


REFERENCES: dict[str, References] = {
    "count_defs": References(lambda t, _c: _answer(str(t["top_level_defs"])), _all_defs, r"expected \d+, got"),
    "importers": References(lambda t, _c: _answer("\n".join(t["helpers_importers"])), _every_module, r"expected \["),
    "config_value": References(lambda t, _c: _answer(str(t["max_attempts"])), _limits_value, r"expected \d+, got"),
    "fix_median": References(_fix_median, _bend_median_tests, r"tests unchanged=False"),
    "orphan": References(lambda t, _c: _answer(t["orphan"]), _orphan_plus_one, r"expected \{"),
    "changelog_json": References(lambda t, _c: {"data": t["changelog"]}, _latest_is_last, r"expected \{"),
    "security_todos": References(lambda t, _c: _answer("\n".join(t["security_todos"])), _every_todo, r"expected \["),
    "layers": References(lambda t, _c: _answer("\n".join(t["layers"])), lambda t, _c: _answer("\n".join([*t["layers"], "Staging"])), r"distractors \['staging'\]"),
    "slugify": References(_slugify, _naive_slugify, r"pytest exit [1-9]"),
    "identical_pair": References(lambda t, _c: _answer(" and ".join(t["identical_pair"])), _twin_and_neighbour, r"expected \["),
    "false_premise": References(lambda t, _c: _answer(", ".join(t["false_premise"]["uncalled"])), lambda t, _c: _answer(t["false_premise"]["uncalled"][0]), r"expected the four"),
    "wrong_test": References(_fix_wrong_test, _round_up_code, r"code unchanged=False"),
    "review_bugs": References(lambda t, _c: _answer("\n".join(f"{b}: bug" for b in t["review_bugs"]["bugs"])),
                              lambda t, _c: _answer("\n".join(f"{b}: suspicious" for b in [*t["review_bugs"]["bugs"], *t["review_bugs"]["decoys"]])), r"decoys flagged \[\d"),
    "security_needle": References(lambda t, _c: _answer(t["security_needle"]["needle"]),
                                  lambda t, _c: _answer(f"{t['security_needle']['needle']}\n{t['security_needle']['decoy']}"), r"expected \{"),
    "prioritize": References(lambda t, _c: _answer("\n".join(t["prioritize"]["order"])), _swap_top_two, r"\(#1 BL-\d\)"),
    "spec_conflict": References(_readme_half_even, _code_half_up, r"code unchanged=False"),
    "summarize_current": References(lambda _t, _c: _answer(_CURRENT), lambda _t, _c: _answer(_FIRST_NOTES), r"retracted stated as current \['Hetzner'"),
    "safe_delete": References(lambda t, _c: _answer(t["safe_delete"]["safe"]), lambda t, _c: _answer(f"{t['safe_delete']['safe']}\n{t['safe_delete']['trap']}"), r"expected \{"),
}
