"""Grader self-test: every grader must pass its good reference and fail its bad one on the declared
axis before any arm is scored. Offline, no API spend; `run.py` runs it ahead of every bench and
refuses to score (or record) a suite that fails it, and `zswarm bench --selftest` runs it alone.

A broken or lax grader used to be trusted silently: the fix_median grader passed an answer that
rewrote the tests to match the bug, and any arm that did so would have carried that error.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from bench.references import REFERENCES


def check(build_fn, tasks) -> list[str]:
    """One line per grader that fails its references; an empty list means every grader proved itself."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zswarm-selftest-") as tmp:
        root = Path(tmp) / "fixture"
        truth = build_fn(root)
        for t in tasks:
            refs = REFERENCES.get(t.id)
            if refs is None:
                failures.append(f"{t.id}: no good/bad reference in bench/references.py")
                continue
            for kind, ref in (("good", refs.good), ("bad", refs.bad)):
                cwd = Path(tmp) / kind / t.id
                shutil.copytree(root / t.subdir if t.subdir else root, cwd)
                try:
                    ok, detail = t.grade(ref(truth, cwd), truth, cwd)
                except Exception as e:  # noqa: BLE001 - a grader or reference that crashes has failed the test
                    failures.append(f"{t.id}: {kind} reference crashed: {type(e).__name__}: {e}")
                    continue
                if kind == "good" and not ok:
                    failures.append(f"{t.id}: grader FAILED its good reference ({detail})")
                elif kind == "bad" and ok:
                    failures.append(f"{t.id}: grader PASSED its bad reference ({detail})")
                elif kind == "bad" and not re.search(refs.axis, detail):
                    failures.append(f"{t.id}: grader failed its bad reference on the wrong axis (want /{refs.axis}/, got: {detail})")
    return failures


def refuse_on_failure(build_fn, tasks, suite: str) -> None:
    failures = check(build_fn, tasks)
    if failures:
        raise SystemExit(f"bench: the {suite} graders failed their self-test, refusing to score:\n  " + "\n  ".join(failures))
