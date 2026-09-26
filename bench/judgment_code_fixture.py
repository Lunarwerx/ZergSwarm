"""Judgment fixture, code-review tasks: wrong_test, review_bugs, spec_conflict, safe_delete.

Each builder plants one task under its own sub-directory and records the computed truth.
Split from judgment.py so the graders and the fixture read separately; the statements are the
original ones, byte for byte, so every seed still produces the same fixture."""
from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _build_wrong_test(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "wrong_test"
    (d / "tests").mkdir(parents=True)
    (d / "pricing.py").write_text(
        '"""Order pricing.\n\nRules (see docs/PRICING.md): the discount applies to the subtotal BEFORE tax; tax is then\napplied to the discounted amount. Amounts are rounded half-up to cents once, at the end.\n"""\nfrom decimal import Decimal, ROUND_HALF_UP\n\n\ndef total(subtotal: str, discount_pct: int, tax_pct: int) -> str:\n    s = Decimal(subtotal)\n    discounted = s * (Decimal(100) - discount_pct) / Decimal(100)\n    taxed = discounted * (Decimal(100) + tax_pct) / Decimal(100)\n    return str(taxed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))\n',
        encoding="utf-8",
    )
    (d / "docs").mkdir()
    (d / "docs" / "PRICING.md").write_text(
        "# Pricing rules\n\n1. The discount applies to the subtotal BEFORE tax.\n2. Tax applies to the discounted amount.\n3. Round half-up to cents once, at the end.\n\nExample: subtotal 100.00, discount 10%, tax 8% -> 100 * 0.90 = 90.00 -> * 1.08 = 97.20.\n",
        encoding="utf-8",
    )
    (d / "tests" / "test_pricing.py").write_text(
        "from pricing import total\n\n\ndef test_example_from_docs():\n    assert total('100.00', 10, 8) == '97.20'\n\n\ndef test_tax_before_discount():\n    # tax first, then discount\n    assert total('100.00', 10, 8) == '97.20'\n\n\ndef test_rounding():\n    assert total('19.99', 15, 7) == '18.18'\n",
        encoding="utf-8",
    )
    # the planted wrong test: total('19.99',15,7): 19.99*0.85=16.9915 *1.07=18.180905 -> 18.18. So make it wrong:
    p = d / "tests" / "test_pricing.py"
    p.write_text(p.read_text(encoding="utf-8").replace("== '18.18'", "== '18.19'"), encoding="utf-8")
    (d / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n", encoding="utf-8")
    truth["wrong_test"] = {"code_sha": _sha(d / "pricing.py"), "docs_sha": _sha(d / "docs" / "PRICING.md")}


def _build_review_bugs(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "review_bugs"
    d.mkdir(parents=True)
    src = '''"""Session cache with TTL and LRU eviction."""
import time
from collections import OrderedDict


class SessionCache:
    def __init__(self, capacity: int, ttl_s: float):
        self.capacity = capacity
        self.ttl_s = ttl_s
        self._items: OrderedDict[str, tuple[object, float]] = OrderedDict()

    def get(self, key: str):
        item = self._items.get(key)
        if item is None:
            return None
        value, expires = item
        if time.monotonic() > expires:
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: str, value) -> None:
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = (value, time.monotonic() + self.ttl_s)
        while len(self._items) > self.capacity:
            self._items.popitem(last=True)  # BUG A: evicts the NEWEST entry, not the LRU one

    def touch(self, key: str) -> bool:
        item = self._items.get(key)
        if item is None:
            return False
        value, _expires = item
        self._items[key] = (value, time.monotonic() + self.ttl_s)
        return True

    def purge_expired(self) -> int:
        now = time.monotonic()
        dead = [k for k, (_v, exp) in self._items.items() if exp < now]
        for k in dead:
            del self._items[k]
        return len(dead)

    def stats(self) -> dict:
        live = sum(1 for _v, exp in self._items.values() if exp >= time.monotonic())
        return {"size": len(self._items), "live": live, "capacity": self.capacity}

    def keys_newest_first(self) -> list[str]:
        return list(reversed(self._items.keys()))  # decoy: correct, OrderedDict keeps insertion order

    def half_life_remaining(self, key: str) -> float | None:
        item = self._items.get(key)
        if item is None:
            return None
        _v, exp = item
        remaining = exp - time.monotonic()
        return remaining / 2 if remaining > 0 else 0.0  # decoy: correct as documented

    def bulk_put(self, pairs: list[tuple[str, object]]) -> int:
        n = 0
        for key, value in pairs:
            self.put(key, value)
            n += 1
        return n  # BUG B: returns pairs count even when later puts evicted earlier ones (documented: return number stored)

    def get_or_default(self, key: str, default):
        value = self.get(key)
        if value is None:
            return default
        return value  # BUG C: a stored value that is falsy-but-not-None is fine, but a stored None is indistinguishable from a miss; spec says stored None must be returned
'''
    (d / "cache.py").write_text(src, encoding="utf-8")
    (d / "SPEC.md").write_text(
        "# SessionCache spec\n\n- Eviction is least-recently-used: when over capacity, drop the entry that was used longest ago.\n- `bulk_put` returns the number of entries actually resident after the call among those passed.\n- A stored `None` value is a legitimate value: `get_or_default` must return it, not the default; only a missing or expired key yields the default.\n- `keys_newest_first` returns keys ordered from most recently used to least.\n- `half_life_remaining` returns half the remaining TTL in seconds, 0.0 when expired.\n",
        encoding="utf-8",
    )
    lines = src.splitlines()
    bug_lines = {tag: i + 1 for i, l in enumerate(lines) for tag in ("BUG A", "BUG B", "BUG C") if tag in l}
    decoy_lines = [i + 1 for i, l in enumerate(lines) if "decoy" in l]
    # strip the marker comments so the worker cannot grep them
    clean = re.sub(r"  # (BUG [ABC]|decoy):.*$", "", src, flags=re.M)
    (d / "cache.py").write_text(clean, encoding="utf-8")
    truth["review_bugs"] = {"bugs": sorted(bug_lines.values()), "decoys": decoy_lines}


def _build_spec_conflict(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "spec_conflict"
    (d / "tests").mkdir(parents=True)
    (d / "money.py").write_text(
        "from decimal import Decimal, ROUND_HALF_EVEN\n\n\ndef round_cents(x: str) -> str:\n    return str(Decimal(x).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN))\n",
        encoding="utf-8",
    )
    (d / "tests" / "test_money.py").write_text(
        "from money import round_cents\n\n\ndef test_bankers():\n    assert round_cents('2.675') == '2.68'\n    assert round_cents('2.665') == '2.66'\n    assert round_cents('1.005') == '1.00'\n",
        encoding="utf-8",
    )
    (d / "README.md").write_text(
        "# money\n\n`round_cents` rounds half UP to the nearest cent: 2.665 -> 2.67, 1.005 -> 1.01.\n",
        encoding="utf-8",
    )
    (d / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 2.0.0\n- BREAKING: rounding switched from half-up to banker's rounding (half-even) to match the ledger service; tests updated.\n\n## 1.2.0\n- round_cents added (half-up).\n",
        encoding="utf-8",
    )
    (d / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n", encoding="utf-8")
    truth["spec_conflict"] = {"code_sha": _sha(d / "money.py"), "test_sha": _sha(d / "tests" / "test_money.py")}


def _build_safe_delete(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "safe_delete"
    (d / "app").mkdir(parents=True)
    (d / "app" / "__init__.py").write_text("", encoding="utf-8")
    (d / "app" / "handlers.py").write_text(
        "def export_csv(rows):\n    return '\\n'.join(','.join(map(str, r)) for r in rows)\n\n\ndef export_json(rows):\n    import json\n    return json.dumps(rows)\n\n\ndef export_xml(rows):\n    return '<rows>' + ''.join(f'<r>{r}</r>' for r in rows) + '</rows>'\n\n\ndef legacy_export_tsv(rows):\n    return '\\n'.join('\\t'.join(map(str, r)) for r in rows)\n\n\ndef export_yaml(rows):\n    return '\\n'.join(f'- {r}' for r in rows)\n",
        encoding="utf-8",
    )
    (d / "app" / "main.py").write_text(
        "import importlib\nfrom app import handlers\nfrom app.config import EXPORTERS\n\n\ndef run(fmt, rows):\n    if fmt == 'csv':\n        return handlers.export_csv(rows)\n    if fmt == 'json':\n        return handlers.export_json(rows)\n    name = EXPORTERS.get(fmt)\n    if name is None:\n        raise KeyError(fmt)\n    return getattr(handlers, name)(rows)\n",
        encoding="utf-8",
    )
    (d / "app" / "config.py").write_text("EXPORTERS = {\n    'xml': 'export_xml',\n    'yaml': 'export_yaml',\n}\n", encoding="utf-8")
    truth["safe_delete"] = {"safe": "legacy_export_tsv", "trap": "export_yaml"}
