"""Benchmark tasks with mechanical graders. Each grader returns (passed: bool, detail: str)."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from zswarm.trace import KnownGap, ToolExpect

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class BenchTask:
    id: str
    prompt: str
    tools: str
    grade: Callable[[dict, dict, Path], tuple[bool, str]]
    schema: dict | None = None
    max_turns: int = 24
    kind: str = "read"
    subdir: str | None = None  # judgment suite: each task has its own fixture sub-directory
    # WHAT the worker must do, scored from its tool trace beside the answer grade (zswarm/trace.py): an answer
    # can be right by luck, and a "fix" can be an edited test. Reported as its own column, never folded into pass.
    expect: ToolExpect | None = None
    # A model gap this task is skipped for, with the evidence and the re-enable condition (bench --include-known-gaps).
    known_gap: KnownGap | None = None


def _ints(s: str) -> list[int]:
    # a dash after a digit is a range ("30-36"), not a minus sign
    return [int(x) for x in re.findall(r"(?<![\d-])-?\d+", s or "")]


def _norm_path(p: str) -> str:
    p = p.strip().strip("`'\"*-• ").replace("\\", "/")
    p = re.sub(r"^\./", "", p)
    return p


def _paths_in(answer: str, suffix: str) -> set[str]:
    return {_norm_path(m) for m in re.findall(r"[\w./\\-]+" + re.escape(suffix), answer or "")}


def g_count_defs(res, truth, _cwd):
    ints = _ints(res.get("answer", ""))
    ok = bool(ints) and ints[-1] == truth["top_level_defs"] if len(ints) == 1 else (bool(ints) and ints[0] == truth["top_level_defs"])
    return ok, f"expected {truth['top_level_defs']}, got {ints[:3]}"


def g_importers(res, truth, _cwd):
    got = {p for p in _paths_in(res.get("answer", ""), ".py") if "helpers" not in p}
    got = {p if p.startswith("pkg/") else "pkg/" + p.split("/")[-1] for p in got}
    exp = set(truth["helpers_importers"])
    return got == exp, f"expected {sorted(exp)}, got {sorted(got)}"


def g_config(res, truth, _cwd):
    ints = _ints(res.get("answer", ""))
    return bool(ints) and ints[0] == truth["max_attempts"], f"expected {truth['max_attempts']}, got {ints[:3]}"


def _pytest(cwd: Path, target: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
            cwd=str(cwd), capture_output=True, text=True, timeout=120, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, "pytest timed out"
    tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
    return p.returncode == 0, f"pytest exit {p.returncode}: {tail[0][:160]}"


def _tests_unchanged(truth: dict, cwd: Path, rel: str) -> bool:
    # a truth.json from before test_sha was planted cannot tell, so it does not fail the worker
    want = (truth.get("test_sha") or {}).get(rel)
    return want is None or hashlib.sha256((cwd / rel).read_bytes()).hexdigest() == want


def _pytest_untouched(truth: dict, cwd: Path, rel: str) -> tuple[bool, str]:
    """The prompt forbids editing the tests; passing them by rewriting them is not a fix (caught by bench/selftest.py)."""
    ok, detail = _pytest(cwd, rel)
    same = _tests_unchanged(truth, cwd, rel)
    return ok and same, f"{detail}; tests unchanged={same}"


def g_fix_median(res, truth, cwd):
    return _pytest_untouched(truth, cwd, "tests/test_stats.py")


def g_slugify(res, truth, cwd):
    return _pytest_untouched(truth, cwd, "tests/test_text.py")


def g_orphan(res, truth, _cwd):
    names = set(re.findall(r"fn_[a-z]+", res.get("answer", "")))
    return names == {truth["orphan"]}, f"expected {{{truth['orphan']}}}, got {sorted(names)}"


def g_changelog(res, truth, _cwd):
    d = res.get("data")
    if not isinstance(d, dict):
        try:
            d = json.loads(res.get("answer", ""))
        except ValueError:
            return False, "no structured data"
    exp = truth["changelog"]
    got = {"versions": list(d.get("versions") or []), "latest": d.get("latest"), "breaking_changes_count": d.get("breaking_changes_count")}
    return got == exp, f"expected {exp}, got {got}"


def g_security_todos(res, truth, _cwd):
    got = set()
    for m in re.findall(r"([\w./\\-]+\.py):(\d+)", res.get("answer", "")):
        p = _norm_path(m[0])
        if not p.startswith("pkg/"):
            p = "pkg/" + p.split("/")[-1]
        got.add(f"{p}:{int(m[1])}")
    exp = set(truth["security_todos"])
    return got == exp, f"expected {sorted(exp)}, got {sorted(got)}"


def g_layers(res, truth, _cwd):
    a = (res.get("answer") or "").lower()
    pos = [a.find(l.lower()) for l in truth["layers"]]
    ok = all(p >= 0 for p in pos) and pos == sorted(pos)
    extra = [x for x in ("staging", "mirror") if x in a]
    return ok and not extra, f"expected order {truth['layers']}, positions {pos}, distractors {extra}"


def g_identical(res, truth, _cwd):
    got = {p if p.startswith("data/") else "data/" + p.split("/")[-1] for p in _paths_in(res.get("answer", ""), ".txt")}
    exp = set(truth["identical_pair"])
    return got == exp, f"expected {sorted(exp)}, got {sorted(got)}"


# Read-only questions: a worker that writes to the fixture while answering one has done something it was not asked.
READ_ONLY = ("write_file", "edit_file", "bash")
# The edit tasks: read the code before changing it, never touch the tests, run them to prove the fix.
EDIT_FLOW = ToolExpect(required=("bash(command~pytest)",), forbidden=("edit_file(path~tests/)", "write_file(path~tests/)"), workflow=("read_file", "edit_file|write_file"))
# gpt-oss speaks OpenAI "harmony" tool calls this api loop does not: its bash `pytest` call is the one that 400s.
HARMONY_GAP = KnownGap(
    observed="groq gpt-oss 400s its own tool calls (`Failed to parse tool call arguments as JSON` on `pytest -q`): ~46% on the first Stage-2 run, "
             "54% after refused samples were resampled; gpt-oss stays out of every tool-using chain",
    run="docs/BENCH-2026-09-20-providers.md Axis 2, first Stage-2 run and the later resample fix", date="2026-09-20",
    reenable="the api tool loop parses gpt-oss harmony-format tool calls (filed under 'Open / filed' in docs/BENCH-2026-09-20-providers.md); "
             "re-run with --include-known-gaps", models=("gpt-oss",))

TASKS: list[BenchTask] = [
    BenchTask("count_defs", "How many top-level function definitions (a `def` or `async def` starting at column 0) are there across every .py file under pkg/? Count only pkg/. Reply with the integer only.", "read", g_count_defs),
    BenchTask("importers", "Which files under pkg/ import `tidy` from pkg.helpers? Reply with the relative paths only, one per line.", "read", g_importers,
              expect=ToolExpect(required=("grep(pattern~helpers)|grep(pattern~tidy)",), forbidden=READ_ONLY)),
    BenchTask("config_value", "In config.json, what is the value of settings.retry.max_attempts? Reply with the integer only.", "read", g_config,
              expect=ToolExpect(required=("read_file(path~config.json)",), forbidden=READ_ONLY)),
    BenchTask("fix_median", "tests/test_stats.py fails. Fix the bug in stats.py (do not edit the tests) so that `python -m pytest tests/test_stats.py -q` passes. Reply with one line describing the fix.", "all", g_fix_median, kind="edit",
              expect=EDIT_FLOW, known_gap=HARMONY_GAP),
    BenchTask("orphan", "Exactly one function defined in the pkg/ modules (names start with fn_) is never called from anywhere in the repository. Which one? Reply with the function name only.", "read", g_orphan),
    BenchTask(
        "changelog_json", "Read CHANGELOG.md. Return the list of versions in the order they appear, the latest version, and how many bullet entries are marked BREAKING.", "read", g_changelog,
        schema={"type": "object", "properties": {"versions": {"type": "array", "items": {"type": "string"}}, "latest": {"type": "string"}, "breaking_changes_count": {"type": "integer"}}, "required": ["versions", "latest", "breaking_changes_count"]},
    ),
    BenchTask("security_todos", "List every TODO comment under pkg/ whose text mentions security. Reply with `path:line` per match, one per line, nothing else.", "read", g_security_todos,
              expect=ToolExpect(required=("grep",), forbidden=READ_ONLY)),
    BenchTask("layers", "docs/ARCH.md describes the system's layers. List the CURRENT layer names in the documented order, one per line, nothing else.", "read", g_layers),
    BenchTask("slugify", "Implement utils/text.py::slugify per its docstring so that `python -m pytest tests/test_text.py -q` passes. Do not edit the tests. Reply with one line when done.", "all", g_slugify, kind="edit",
              expect=EDIT_FLOW, known_gap=HARMONY_GAP),
    BenchTask("identical_pair", "Exactly two files under data/ have byte-identical contents. Which two? Reply with the two relative paths only.", "read", g_identical),
]

BY_ID = {t.id: t for t in TASKS}
