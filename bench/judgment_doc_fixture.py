"""Judgment fixture, reading-and-deciding tasks: false_premise, security_needle, prioritize, summarize_current.

Each builder plants one task under its own sub-directory and records the computed truth.
Split from judgment.py so the graders and the fixture read separately; the statements are the
original ones, byte for byte, so every seed still produces the same fixture."""
from __future__ import annotations

import random
from pathlib import Path

WORDS = "amber basil cedar dune ember fjord grove harbor iris jade karst lagoon marsh nectar ochre pine quartz reef slate tundra umber vale willow yarrow zenith".split()


def _build_false_premise(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "false_premise" / "pkg"
    d.mkdir(parents=True)
    fns = [f"fn_{w}" for w in rng.sample(WORDS, 12)]
    uncalled = set(rng.sample(fns, 4))
    for i in range(4):
        lines = [f"# module m{i}", ""]
        for fn in fns[i * 3 : (i + 1) * 3]:
            lines.append(f"def {fn}(x):")
            callees = [c for c in rng.sample(fns, 2) if c not in uncalled and c != fn]
            for c in callees:
                lines.append(f"    x = {c}(x)")
            lines.append("    return x")
            lines.append("")
        (d / f"m{i}.py").write_text("\n".join(lines), encoding="utf-8")
    (d / "__init__.py").write_text("", encoding="utf-8")
    text = "\n".join(p.read_text() for p in d.glob("*.py"))
    for fn in fns:
        n = text.count(fn + "(") - 1
        assert (n == 0) == (fn in uncalled), fn
    truth["false_premise"] = {"uncalled": sorted(uncalled), "all": fns}


def _build_security_needle(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "security_needle"
    d.mkdir(parents=True)
    handlers = rng.sample(["orders", "users", "reports", "invoices", "tickets", "audit"], 6)
    needle = rng.choice(handlers)
    decoy = rng.choice([h for h in handlers if h != needle])
    parts = ["import sqlite3\n\nDB = 'app.db'\nTABLE_PREFIX = 'app_'\n\n"]
    for h in handlers:
        if h == needle:
            body = f"def get_{h}(request):\n    q = request.args.get('q', '')\n    sql = \"SELECT * FROM {h} WHERE name = '\" + q + \"'\"\n    return sqlite3.connect(DB).execute(sql).fetchall()\n"
        elif h == decoy:
            body = f"def get_{h}(request):\n    q = request.args.get('q', '')\n    sql = 'SELECT * FROM ' + TABLE_PREFIX + '{h}' + ' WHERE name = ?'\n    return sqlite3.connect(DB).execute(sql, (q,)).fetchall()\n"
        else:
            body = f"def get_{h}(request):\n    q = request.args.get('q', '')\n    return sqlite3.connect(DB).execute('SELECT * FROM {h} WHERE name = ?', (q,)).fetchall()\n"
        parts.append(body + "\n\n")
    (d / "handlers.py").write_text("".join(parts), encoding="utf-8")
    truth["security_needle"] = {"needle": f"get_{needle}", "decoy": f"get_{decoy}"}


def _build_prioritize(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "prioritize"
    d.mkdir(parents=True)
    items = [
        ("BL-1", "Nightly job deletes customer invoices when two runs overlap; happened twice this week, no backups of the deleted rows", 1),
        ("BL-2", "Checkout returns HTTP 500 for ~3% of customers since yesterday's deploy", 2),
        ("BL-3", "Password reset emails go out with a link that expires after 5 minutes instead of 60; support tickets rising", 3),
        ("BL-4", "Admin dashboard chart colours do not match the brand guide", 8),
        ("BL-5", "Typo on the pricing page ('recieve')", 7),
        ("BL-6", "Add dark mode to the settings page (requested by 4 users)", 6),
        ("BL-7", "Upgrade the logging library; current version is two majors behind but has no known CVE", 5),
        ("BL-8", "Search results page loads in 4 s on large accounts; was 2 s last month", 4),
    ]
    rng.shuffle(items)
    (d / "BACKLOG.md").write_text("# Backlog (unordered)\n\n" + "\n".join(f"- {i}: {t}" for i, t, _ in items) + "\n", encoding="utf-8")
    order = [i for i, _t, r in sorted(items, key=lambda x: x[2])]
    truth["prioritize"] = {"order": order}


def _build_summarize_current(root: Path, rng: random.Random, truth: dict) -> None:
    d = root / "summarize_current"
    d.mkdir(parents=True)
    doc = """# Project Kestrel: status notes (running log, newest at the bottom)

2026-08-02: Launch target is October 14. Hosting is on Hetzner. The mobile app ships with the launch.
2026-08-09: Payment provider chosen: Stripe. Team size is 6.
2026-08-20: Hosting decision reversed: we are moving to Fly.io because of the EU data residency requirement; Hetzner is out.
2026-08-28: Beta has 140 users. Support hours are 9-17 CET.
2026-09-03: Correction to the 08-02 note: the mobile app will NOT ship with the launch; it moves to Q1 2027. Web only at launch.
2026-09-10: Launch target confirmed October 14. Team size now 7 after hiring a designer.
"""
    (d / "STATUS.md").write_text(doc, encoding="utf-8")
    truth["summarize_current"] = {
        "current": ["October 14", "Fly.io", "Stripe", "140", "9-17", "7", "Q1 2027"],
        "retracted": ["Hetzner", "ships with the launch"],
        "min_current": 5,
    }
