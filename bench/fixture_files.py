"""The mechanical fixture's file templates and its document generators (changelog, architecture
note, data files). fixture.py assembles the package under test; this module holds what it writes."""
from __future__ import annotations

import random
from pathlib import Path

from bench.words import LAYER_POOL, WORDS

STATS_PY = (
    '"""Tiny statistics helpers."""\n\n\ndef mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef median(xs):\n'
    '    """Return the median of a non-empty list of numbers."""\n    s = sorted(xs)\n    n = len(s)\n'
    "    return s[n // 2]\n"
)
TEST_STATS_PY = (
    "from stats import mean, median\n\n\ndef test_mean():\n    assert mean([1, 2, 3, 4]) == 2.5\n\n\n"
    "def test_median_odd():\n    assert median([3, 1, 2]) == 2\n\n\ndef test_median_even():\n    assert median([4, 1, 3, 2]) == 2.5\n\n\n"
    "def test_median_even_floats():\n    assert median([1.0, 2.0]) == 1.5\n"
)
TEXT_PY = (
    'def slugify(s: str) -> str:\n    """Lower-case, replace every run of non-alphanumeric characters with a single "-",\n'
    '    strip leading/trailing "-". Empty input returns "".\n    """\n    raise NotImplementedError\n'
)
TEST_TEXT_PY = (
    "from utils.text import slugify\n\n\ndef test_basic():\n    assert slugify('Hello World') == 'hello-world'\n\n\n"
    "def test_runs():\n    assert slugify('  A--b__c!!d ') == 'a-b-c-d'\n\n\ndef test_empty():\n    assert slugify('') == ''\n\n\n"
    "def test_unicode_digits():\n    assert slugify('Item 42 / v2.0') == 'item-42-v2-0'\n"
)
HELPERS_PY = "def tidy(s: str) -> str:\n    return ' '.join(s.split())\n\n\ndef _private():\n    pass\n"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _changelog(rng: random.Random, modules: list[str]) -> tuple[str, list[str], int]:
    versions = []
    major, minor, patch = rng.randint(1, 3), rng.randint(0, 5), rng.randint(0, 9)
    for _ in range(rng.randint(5, 8)):
        versions.append(f"{major}.{minor}.{patch}")
        bump = rng.random()
        if bump < 0.15:
            major -= 1 if major > 1 else 0
            minor = rng.randint(0, 9)
        elif bump < 0.5 and minor > 0:
            minor -= 1
        elif patch > 0:
            patch -= 1
        else:
            minor = max(0, minor - 1)
            patch = rng.randint(1, 9)
    breaking = 0
    cl = ["# Changelog", ""]
    for v in versions:
        cl.append(f"## {v} - 2026-0{rng.randint(1, 9)}-{rng.randint(10, 28)}")
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.3:
                cl.append(f"- BREAKING: removed the {rng.choice(WORDS)} option")
                breaking += 1
            else:
                cl.append(f"- Fixed {rng.choice(WORDS)} handling in {rng.choice(modules)}")
        cl.append("")
    return "\n".join(cl), versions, breaking


def _arch_doc(rng: random.Random) -> tuple[str, list[str]]:
    layers = rng.sample(LAYER_POOL, 5)
    arch = ["# Architecture", "", "The system is built from five layers, listed here from the outside in:", ""]
    arch += [f"{i}. **{l} layer** - handles {rng.choice(WORDS)} and {rng.choice(WORDS)}." for i, l in enumerate(layers, 1)]
    arch += ["", "Historically there was also a Staging layer, removed in 2025, and a proposed Mirror layer that was never built.", ""]
    return "\n".join(arch), layers


def _data_files(rng: random.Random, root: Path) -> list[str]:
    names = [f"{w}.txt" for w in rng.sample(WORDS, 6)]
    contents = [" ".join(rng.sample(WORDS, 12)) + "\n" for _ in names]
    dup_a, dup_b = rng.sample(range(6), 2)
    contents[dup_b] = contents[dup_a]
    for n, c in zip(names, contents):
        _write(root, f"data/{n}", c)
    return sorted([f"data/{names[dup_a]}", f"data/{names[dup_b]}"])
