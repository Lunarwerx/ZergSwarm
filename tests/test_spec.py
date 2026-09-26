"""Offline: task parsing, prompt building, result truncation, the shared helpers. No network."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zswarm.agent import build_messages, submit_result_spec  # noqa: E402
from zswarm.spec import Result, Task, inline_files, now_iso  # noqa: E402


def test_task_from_dict_defaults(tmp_path):
    t = Task.from_dict({"prompt": "hi"}, {"cwd": str(tmp_path), "model": "flash"}, 0)
    assert t.id == "t1" and t.model == "deepseek-flash" and t.backend == "api" and t.cwd == str(tmp_path.resolve())
    assert t.reasoning_effort == "low"  # the runaway guard: api tasks default to low effort
    with pytest.raises(ValueError):
        Task.from_dict({"prompt": "", "cwd": str(tmp_path)}, {}, 1)
    with pytest.raises(ValueError):
        Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "backend": "warp"}, {}, 2)
    with pytest.raises(ValueError):
        Task.from_dict({"prompt": "x", "cwd": str(tmp_path), "bogus": 1}, {}, 3)


def test_writable_is_normalised_told_and_refused_on_cc(tmp_path):
    # A single glob becomes a list, the worker is told what it may write, and cc (which cannot enforce it) refuses the field.
    t = Task.from_dict({"prompt": "fix", "cwd": str(tmp_path), "model": "flash", "tools": "edit", "writable": "src/**"}, {}, 0)
    assert t.writable == ["src/**"] and "src/**" in build_messages(t)[0]["content"]
    assert "read-only" not in build_messages(Task.from_dict({"prompt": "fix", "cwd": str(tmp_path), "model": "flash"}, {}, 1))[0]["content"]
    with pytest.raises(ValueError, match="writable"):
        Task.from_dict({"prompt": "fix", "cwd": str(tmp_path), "backend": "cc", "model": "flash", "writable": ["src/**"]}, {}, 2)


def test_build_messages_inlines_files(tmp_path):
    (tmp_path / "notes.md").write_text("alpha beta")
    t = Task.from_dict({"prompt": "summarize", "cwd": str(tmp_path), "files": ["notes.md"], "schema": {"type": "object", "properties": {"n": {"type": "integer"}}}}, {}, 0)
    msgs = build_messages(t)
    assert msgs[0]["role"] == "system" and "submit_result" in msgs[0]["content"]
    assert "alpha beta" in msgs[1]["content"] and "--- file:" in msgs[1]["content"]
    spec = submit_result_spec(t.schema)
    assert spec["function"]["name"] == "submit_result" and spec["function"]["parameters"]["type"] == "object"


def test_inline_files_and_shared_helpers(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    t = Task.from_dict({"prompt": "p", "cwd": str(tmp_path), "files": ["a.txt", "missing.txt"]}, {}, 0)
    out = inline_files(t, "p")
    assert "--- file:" in out and "alpha" in out and "<unreadable" in out  # a missing file is reported inline, never fatal
    assert now_iso().endswith("+00:00")


def test_result_truncation():
    r = Result(id="a", answer="x" * 100)
    d = r.as_dict(10)
    assert d["answer"].startswith("xxxxxxxxxx") and d["answer_truncated"] is True
    assert "answer_truncated" not in r.as_dict(1000)


def test_a_batch_result_carries_autos_pick_not_its_whole_record():
    # Regression (2026-09-26): the batch path never asked for the brief form, so every zswarm_run task carried every
    # candidate's scores and every rejection's reason, about 5,000 tokens a task in the calling agent's context.
    from types import SimpleNamespace

    from zswarm.results import _split
    sel = {"profile": "code", "candidates": [{"model": "a", "scores": {"x": 1}}],
           "rejected": [{"model": "b", "filter": "usable", "reason": "no key"}],
           "selected": {"model": "a", "configuration": "A (high)", "scores": {"x": 1}, "rates": {"hit": 1}}}
    results, _, _ = _split([SimpleNamespace(id="t1")], {"t1": Result(id="t1", status="ok", selection=sel)}, None, None, {}, 1000)
    assert results[0]["selection"] == {"profile": "code", "candidates": ["a"], "rejected": {"usable": ["b"]},
                                       "selected": {"model": "a", "configuration": "A (high)"}}
