"""Generate a deterministic synthetic repo with COMPUTED ground truth.

Every benchmark question has a right answer that this generator knows because it planted
it. `truth.json` is written next to the fixture; graders read it, workers never see it.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
from pathlib import Path

from bench.fixture_files import HELPERS_PY, STATS_PY, TEST_STATS_PY, TEST_TEXT_PY, TEXT_PY, _arch_doc, _changelog, _data_files, _write
from bench.words import WORDS


def _module_source(rng: random.Random, m: str, fns: list[str], all_fns: list[str], orphan: str, imports_helper: bool, security_todos: list[str]) -> tuple[str, int]:
    """One pkg module: its functions, decoy nested defs, TODO comments, and calls into other functions."""
    lines: list[str] = ["# module " + m]
    if imports_helper:
        lines.append("from pkg.helpers import tidy")
    lines.append("")
    defs = 0
    for fn in fns:
        callees = [c for c in rng.sample(all_fns, 2) if c != orphan and c != fn]
        lines.append(("async def " if rng.random() < 0.2 else "def ") + fn + "(x):")
        defs += 1
        if rng.random() < 0.3:
            todo = "security" if rng.random() < 0.5 else "cleanup"
            lines.append(f"    # TODO: {todo} review of input handling")
            if todo == "security":
                security_todos.append(f"pkg/{m}:{len(lines)}")
        if rng.random() < 0.35:
            lines += ["    def inner(y):", "        return y + 1", "    x = inner(x)"]
        for c in callees:
            lines.append(f"    x = {c}(x) if callable(globals().get({c!r})) else x")
        lines += ["    return x", "", ""]
    return "\n".join(lines).rstrip() + "\n", defs


def _pkg_text(root: Path, modules: list[str]) -> str:
    return "\n".join((root / "pkg" / m).read_text(encoding="utf-8") for m in modules)


def _wire_uncalled(rng: random.Random, root: Path, modules: list[str], per_module: dict[str, list[str]], all_fns: list[str], orphan: str) -> int:
    """Make "exactly one uncalled function" TRUE: every non-orphan function gets a caller.
    (The first generator did not guarantee it; six functions were uncalled and both Sonnet and
    Opus named a valid one the grader rejected. Fixed 2026-09-15.) Returns defs added."""
    alltext = _pkg_text(root, modules)
    uncalled = [f for f in all_fns if f != orphan and (alltext.count(f + "(") - 1) <= 0]
    for fn in uncalled:
        host = root / "pkg" / rng.choice([m for m in modules if fn not in per_module[m]])
        host.write_text(host.read_text(encoding="utf-8").rstrip("\n") + f"\n\n\ndef _wire_{fn[3:]}(x):\n    return {fn}(x) if callable(globals().get({fn!r})) else x\n", encoding="utf-8")
    alltext = _pkg_text(root, modules)
    for f in all_fns:
        n_calls = alltext.count(f + "(") - 1
        assert (n_calls == 0) == (f == orphan), f"{f}: {n_calls} calls (orphan={orphan})"
    return len(uncalled)


def build(root: Path, seed: int = 7) -> dict:
    rng = random.Random(seed)
    if root.exists():
        shutil.rmtree(root)
    for rel in ("pkg/__init__.py", "utils/__init__.py", "tests/__init__.py"):
        _write(root, rel, "")
    (root / "docs").mkdir()
    (root / "data").mkdir()

    fn_names = [f"fn_{w}" for w in rng.sample(WORDS, 40)]
    modules = [f"m{i:02d}.py" for i in range(1, 9)]
    per_module: dict[str, list[str]] = {}
    idx = 0
    for m in modules:
        k = rng.randint(2, 6)
        per_module[m] = fn_names[idx : idx + k]
        idx += k
    all_fns = [f for fs in per_module.values() for f in fs]
    orphan = rng.choice(all_fns)
    importers = set(rng.sample(modules, 3))
    security_todos: list[str] = []

    _write(root, "pkg/helpers.py", HELPERS_PY)
    top_level_defs = 2
    for m in modules:
        src, defs = _module_source(rng, m, per_module[m], all_fns, orphan, m in importers, security_todos)
        _write(root, f"pkg/{m}", src)
        top_level_defs += defs
    top_level_defs += _wire_uncalled(rng, root, modules, per_module, all_fns, orphan)

    max_attempts = rng.randint(3, 9)
    config = {
        "name": "fixture",
        "settings": {"retry": {"max_attempts": max_attempts, "backoff_ms": rng.choice([100, 250, 500])}, "features": rng.sample(WORDS, 4), "limits": {"max_attempts": max_attempts + 10}},
        "legacy": {"retry": {"max_attempts": max_attempts + 3}},
    }
    _write(root, "config.json", json.dumps(config, indent=2))
    _write(root, "stats.py", STATS_PY)
    _write(root, "tests/test_stats.py", TEST_STATS_PY)
    _write(root, "utils/text.py", TEXT_PY)
    _write(root, "tests/test_text.py", TEST_TEXT_PY)
    _write(root, "conftest.py", "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")

    changelog, versions, breaking = _changelog(rng, modules)
    _write(root, "CHANGELOG.md", changelog)
    arch, layers = _arch_doc(rng)
    _write(root, "docs/ARCH.md", arch)
    identical_pair = _data_files(rng, root)

    truth = {
        "top_level_defs": top_level_defs,
        "helpers_importers": sorted(f"pkg/{m}" for m in importers),
        "max_attempts": max_attempts,
        "orphan": orphan,
        "all_fns": all_fns,
        "changelog": {"versions": versions, "latest": versions[0], "breaking_changes_count": breaking},
        "security_todos": sorted(security_todos),
        "layers": layers,
        "identical_pair": identical_pair,
        # the edit tasks say "do not edit the tests": without these a worker that bent the tests to the bug passed
        "test_sha": {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in ("tests/test_stats.py", "tests/test_text.py")},
    }
    (root.parent / f"{root.name}.truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")
    return truth


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "bench/out/fixture")
    print(json.dumps(build(out), indent=1))
