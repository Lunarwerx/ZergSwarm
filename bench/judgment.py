"""Judgment-heavy benchmark: tasks where the right answer requires deciding, not just finding.

Every task still has a mechanically gradable answer, planted by the generator. Each task gets
its own sub-directory under the fixture root (`build(root)` returns truth; `TASKS[i].subdir`
names the directory), so `run.py --suite judgment` and `external.py` copy per task.

Tasks:
  false_premise      the prompt asserts "exactly one" uncalled function; four are. Right answer:
                     say the premise is wrong and name all four.
  wrong_test         a test contradicts the documented behaviour; the code is right. Right
                     answer: fix the test, leave the code alone.
  review_bugs        review a module with three planted real bugs and two decoys that look wrong
                     but are correct. Right answer: the three, not the decoys.
  security_needle    six request handlers; one is exploitable (user input concatenated into SQL),
                     one concatenates a constant (decoy). Right answer: the one.
  prioritize         eight backlog items with a planted severity order. Right answer: the top three,
                     with the data-loss bug first.
  spec_conflict      README, code and tests disagree about rounding; a CHANGELOG entry settles it.
                     Right answer: fix the README, not the code.
  summarize_current  a status doc where two facts are retracted later in the doc. Right answer:
                     a summary that carries the current facts and none of the retracted values.
  safe_delete        five functions; two look unused, but one of those is reached via a string in a
                     config registry. Right answer: exactly one is safe to delete.
"""
from __future__ import annotations

import json
import random
import re
import shutil
from pathlib import Path

from bench.judgment_code_fixture import _build_review_bugs, _build_safe_delete, _build_spec_conflict, _build_wrong_test, _sha
from bench.judgment_doc_fixture import _build_false_premise, _build_prioritize, _build_security_needle, _build_summarize_current
from bench.tasks import BenchTask, _ints, _pytest









def build(root: Path, seed: int = 11) -> dict:
    rng = random.Random(seed)
    if root.exists():
        shutil.rmtree(root)
    truth: dict = {}
    _build_false_premise(root, rng, truth)
    _build_wrong_test(root, rng, truth)
    _build_review_bugs(root, rng, truth)
    _build_security_needle(root, rng, truth)
    _build_prioritize(root, rng, truth)
    _build_spec_conflict(root, rng, truth)
    _build_summarize_current(root, rng, truth)
    _build_safe_delete(root, rng, truth)
    (root.parent / f"{root.name}.truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    return truth



# ---- graders (res, truth, task_dir) -> (pass, detail) --------------------------


_NEG_RX = re.compile(r"never|not\b|no call|zero|uncalled|unused|dead|without|only on its own", re.I)
_POS_RX = re.compile(r"called|referenced|call site|caller|used|appear|invoked", re.I)


def _claimed_uncalled(answer: str) -> tuple[set[str], set[str]]:
    """Names the answer claims are UNCALLED vs CALLED, judged per clause. A correct objection
    ("four are never called: a, b, c, d; the other eight (e..l) are each referenced") names all
    twelve functions, so counting every fn_ token graded the right answer wrong (regrade 2026-09-15)."""
    uncalled, called = set(), set()
    answer = re.sub(r"\b(e\.g|i\.e|etc|vs)\.", r"\1", answer)  # "e.g. `fn_x`" is not a clause boundary
    for clause in re.split(r"\.\s|\n|;", answer):
        names = set(re.findall(r"fn_[a-z]+", clause))
        if not names:
            continue
        if _NEG_RX.search(clause):
            uncalled |= names
        elif _POS_RX.search(clause):
            called |= names
        else:
            uncalled |= names  # a bare list ("fn_a, fn_b, fn_c, fn_d") is the answer itself
    return uncalled, called


def g_false_premise(res, truth, _cwd):
    t = truth["false_premise"]
    uncalled, called = _claimed_uncalled(res.get("answer", ""))
    exp = set(t["uncalled"])
    ok = uncalled == exp and not (called & exp)
    return ok, f"expected the four uncalled {sorted(exp)}, got {sorted(uncalled)}" + (f" (also claimed called: {sorted(called & exp)})" if called & exp else "")


def g_wrong_test(res, truth, cwd):
    t = truth["wrong_test"]
    code_same = _sha(cwd / "pricing.py") == t["code_sha"]
    docs_same = _sha(cwd / "docs" / "PRICING.md") == t["docs_sha"]
    ok, detail = _pytest(cwd, "tests/test_pricing.py")
    return ok and code_same and docs_same, f"{detail}; code unchanged={code_same}; docs unchanged={docs_same}"


def g_review_bugs(res, truth, _cwd):
    t = truth["review_bugs"]
    got = set(_ints(res.get("answer", "")))
    bugs, decoys = set(t["bugs"]), set(t["decoys"])
    # a bug spans a few lines (the `if` and the `return`); accept a citation within 3 lines
    hit = {b for b in bugs if any(abs(b - g) <= 3 for g in got)}
    fp = {d for d in decoys if any(abs(d - g) <= 1 for g in got)}
    return len(hit) == 3 and not fp, f"bugs at {sorted(bugs)}: found {sorted(hit)}; decoys flagged {sorted(fp)}"


def g_security_needle(res, truth, _cwd):
    t = truth["security_needle"]
    names = set(re.findall(r"get_[a-z]+", res.get("answer", "")))
    return names == {t["needle"]}, f"expected {{{t['needle']}}} (decoy {t['decoy']}), got {sorted(names)}"


def g_prioritize(res, truth, _cwd):
    order = truth["prioritize"]["order"]
    got = re.findall(r"BL-\d", res.get("answer", ""))
    seen = []
    for g in got:
        if g not in seen:
            seen.append(g)
    ok = len(seen) >= 3 and seen[0] == order[0] and set(seen[:3]) == set(order[:3])
    return ok, f"expected top3 {order[:3]} (#1 {order[0]}), got {seen[:4]}"


def g_spec_conflict(res, truth, cwd):
    t = truth["spec_conflict"]
    code_same = _sha(cwd / "money.py") == t["code_sha"]
    test_same = _sha(cwd / "tests" / "test_money.py") == t["test_sha"]
    readme = (cwd / "README.md").read_text(encoding="utf-8").lower()
    # the README must now state half-even/banker's; a historical mention of half-up ("switched from half-up") is fine,
    # a definition of the current rule as half-up ("rounds half up") is not
    fixed = ("half-even" in readme or "banker" in readme or "half to even" in readme or "half-to-even" in readme) and not re.search(r"rounds?\s+half[- ]?up\b", readme)
    ok, detail = _pytest(cwd, "tests/test_money.py")
    return ok and code_same and test_same and fixed, f"{detail}; code unchanged={code_same}; tests unchanged={test_same}; readme fixed={fixed}"


def g_summarize_current(res, truth, _cwd):
    t = truth["summarize_current"]
    a = res.get("answer", "")
    cur = [c for c in t["current"] if c.lower() in a.lower()]
    # a retracted value may be mentioned as history ("moved from Hetzner"); it fails only when stated as current
    # "after the 2026-08-20 reversal of the earlier Hetzner decision" is history, not a current claim (regrade 2026-09-15)
    markers = ("moved", "revers", "out", "no longer", "not ", "n't", "instead", "was ", "formerly", "previously", "replaced", "from ", "earlier", "original", "overrid", "before", "switch", "dropped", "abandon", "old ", "retract")
    bad = []
    for r in t["retracted"]:
        for line in a.splitlines():
            if r.lower() in line.lower() and not any(m in line.lower() for m in markers):
                bad.append(r)
                break
    return len(cur) >= t["min_current"] and not bad, f"current facts present {len(cur)}/{len(t['current'])}, retracted stated as current {bad}"


def g_safe_delete(res, truth, _cwd):
    t = truth["safe_delete"]
    names = set(re.findall(r"(?:legacy_)?export_[a-z]+", res.get("answer", "")))
    return names == {t["safe"]}, f"expected {{{t['safe']}}} (trap {t['trap']}), got {sorted(names)}"


TASKS: list[BenchTask] = [
    BenchTask("false_premise", "Exactly one function defined under pkg/ (names start with fn_) is never called from anywhere. Which one? Reply with the function name only.", "read", g_false_premise, subdir="false_premise"),
    BenchTask("wrong_test", "tests/test_pricing.py fails. Make the test suite pass. The documented pricing rules in docs/PRICING.md and the pricing.py docstring are authoritative. Reply with one line saying what you changed and why.", "all", g_wrong_test, kind="edit", subdir="wrong_test"),
    BenchTask("review_bugs", "Review cache.py against SPEC.md. List every real bug as `line: description`, one per line, nothing else. Do not list code that is correct.", "read", g_review_bugs, subdir="review_bugs"),
    BenchTask("security_needle", "handlers.py has six request handlers. Exactly one is exploitable by a user-controlled input. Reply with that function's name only.", "read", g_security_needle, subdir="security_needle"),
    BenchTask("prioritize", "Read BACKLOG.md and order the items by what an engineering lead should fix first. Reply with the item ids in priority order, one per line, most urgent first, nothing else.", "read", g_prioritize, subdir="prioritize"),
    BenchTask("spec_conflict", "README.md, money.py and tests/test_money.py disagree about rounding. Decide which is correct, fix whatever is wrong so the repository is consistent, and keep the tests passing. Reply with one line stating your decision.", "all", g_spec_conflict, kind="edit", subdir="spec_conflict"),
    BenchTask("summarize_current", "Read STATUS.md and summarise the CURRENT state of the project in at most 6 bullets: launch date, hosting, payments, beta users, support hours, team size, mobile app. Nothing else.", "read", g_summarize_current, subdir="summarize_current"),
    BenchTask("safe_delete", "app/handlers.py has five export functions. Which of them can be deleted with no behaviour change anywhere in the repository? Reply with the function name(s) only.", "read", g_safe_delete, subdir="safe_delete"),
]

BY_ID = {t.id: t for t in TASKS}

# Criterion assertions (bench/criterion.py): an LLM judge ANDed with the mechanical grader, formatted over the
# task's truth. summarize_current's grader reads wording markers, so "Hetzner is not the plan" and "hosting:
# not Fly.io, Hetzner" look alike to it; a judge told the planted facts can tell them apart.
CRITERIA: dict[str, str] = {
    "summarize_current": "The summary presents the project's CURRENT state only. None of these retracted values may be "
                         "presented as the current state (naming one as the old or dropped plan is fine): {retracted}. "
                         "Current values it should carry: {current}.",
}
