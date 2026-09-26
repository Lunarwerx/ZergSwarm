"""Published benchmark eligibility and cost ordering, independent of model calls.

Floors below are operational policy, not probabilities of success. CritPt is under review
and intentionally never qualifies a route. Evidence carries exact model/effort identities.
"""
from __future__ import annotations

import json
import math
import time
from functools import lru_cache
from importlib.resources import files

PROFILES = {
    "routine": {"humanitys-last-exam": .19},
    "general": {"humanitys-last-exam": .30, "long-context": .75},
    "code": {"terminalbench-4-0": .30, "scicode": .50},
    "decision": {"gdpval-aa": 1400, "automationbench-aa": .60},
    "research": {"humanitys-last-exam": .45, "long-context": .80},
    "critical": {"terminalbench-4-0": .50, "humanitys-last-exam": .50, "gdpval-aa": 1700},
}


@lru_cache(maxsize=1)
def evidence():
    """The published benchmark scores and costs, by slug."""
    return json.loads(files("zswarm").joinpath("data", "published-models.json").read_text(encoding="utf-8"))


def auto_models():
    """The models AUTO can rank: every registered model whose provider file names a `benchmark_slug`."""
    from . import config

    return [(n, m) for n, m in config.MODELS.items() if m.get("benchmark_slug")]


def priority_rank(model):
    """The model's priority number (config.PRIORITY, starred in `zswarm ui`); unnumbered models tie after every
    numbered one, so with none set the cost order stands."""
    from . import config

    return config.PRIORITY.get(model, math.inf)


def profile_for(role=None, tools="none"):
    from . import config

    if role and role not in ("default", "vision"):
        profiles = {"code": "code", "judge": "decision", "summarize": "general", "search": "research", "review": "code",
                    # grader and doubt judge what they are handed; refute must read the repo to disprove a finding
                    "grader": "decision", "refute": "code", "doubt": "decision"}
        if role in PROFILES:
            return role
        if role not in profiles:
            raise ValueError(f"unknown role {role!r}")
        return profiles[role]
    return "general" if config.is_tool_free(tools) else "code"


# Owner, Michael, 2026-09-25: "When something is out of keys, we always advance to the next model in the list
# instead of just... stop." An evaluated model's same-provider siblings are the last legs of its plan: a provider
# rate-limits each model on its own (Gemini's per-model, per-region quota saturated 3.8 Flash that afternoon while
# 3.7 Flash, 3.5 Flash and 3.5 Flash-Lite all answered), so a task advances onto them instead of dying. They carry
# no benchmark evidence of their own: they come after every evaluated candidate and are marked `unevidenced`, and
# the orchestrator's acceptance check judges their answer. A model lists them as `siblings` in its provider file.


def _last_resort(candidates, *, excluded=(), usable=None, min_context=0, profile="general", tools="none", backend="api", vision=False,
                 strict=False):
    from . import config

    have, extra = {c["model"] for c in candidates}, []

    def unfit(entry) -> bool:  # what the task needs that the model's file does not say it can do
        return ((not config.is_tool_free(tools) and not entry.get("tools")) or (vision and not entry.get("vision"))
                or (min_context and entry.get("ctx", 0) < min_context)
                or (backend == "cc" and not config.PROVIDERS[entry["provider"]].get("anthropic_url")))

    for c in candidates:
        for name in config.MODELS.get(c["model"], {}).get("siblings") or ():
            entry = config.MODELS.get(name)
            if not entry or name in have or name in excluded or entry["provider"] != c["provider"] or name in config.DISABLED_MODELS:
                continue
            if unfit(entry) or (usable is not None and not usable(entry["provider"])):
                continue
            have.add(name)
            extra.append({**c, "model": name, "configuration": f"{name} (unevidenced sibling of {c['configuration']})",
                          "rates": config.price(name), "unevidenced": True})
    # Backups (owner, 2026-09-26: "why do I have to have other AIs making decisions?"): a model its provider file marks
    # `backup = true` runs after every tested leg and sibling, on any provider with a ready key, so a task whose tested
    # routes are all busy or dead moves onto capacity that is up instead of waiting or dying. Only in a live plan (one that
    # knows which keys work), never for a critical task or one that demands an effort or scores (strict), and only where it
    # fits (tools, images, context, a cc worker's endpoint).
    for name, entry in config.MODELS.items():
        if usable is None or strict or profile == "critical" or not entry.get("backup") or name in have or name in excluded or name in config.DISABLED_MODELS:
            continue
        p = entry["provider"]
        if not config.provider_enabled(p) or (usable is not None and not usable(p)):
            continue
        if unfit(entry):
            continue
        have.add(name)
        extra.append({"model": name, "reasoning_effort": None, "thinking": None, "benchmark_slug": "", "score": None,
                      "benchmark_cost_usd": None, "source": "", "scores": {}, "rates": config.price(name), "provider": p,
                      "configuration": f"{name} (backup on {p}, without test scores of its own)", "unevidenced": True, "backup": True})
    return extra


def plan(profile="general", *, tools="none", backend="api", usable=None, reasoning_effort=None,
         thinking=None, min_scores=None, exclude_models=(), min_context=0, vision=False):
    from . import config

    if profile not in PROFILES:
        raise ValueError(f"unknown capability profile {profile!r}; choose {sorted(PROFILES)}")
    if backend not in ("api", "cc"):
        raise ValueError(f"unknown backend {backend!r}")
    if profile == "routine" and not config.is_tool_free(tools):
        raise ValueError("routine is tool-free only; tool work requires a stronger capability profile")
    data = evidence()
    points = {p["slug"]: p for p in data["points"]}
    floors = dict(PROFILES[profile])
    valid = set(next(iter(points.values()))["scores"]) - {"critpt"}
    for key, value in (min_scores or {}).items():
        if key not in valid or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"invalid score requirement {key!r}; CritPt is excluded while under review")
        floors[key] = max(floors.get(key, float("-inf")), value)
    excluded = set(exclude_models or ())
    # Every route that is not a candidate is named in `rejected` with the FIRST filter it failed, in the order
    # below, so "why was X skipped" is answered by the plan (and the job result carrying it), not by a re-run.
    candidates, rejected = [], []
    identity = ("benchmark_slug", "api_id", "provider", "default_reasoning_effort", "default_thinking")
    for name, entry in auto_models():
        p = points.get(entry["benchmark_slug"])
        # A user's file must not keep a shipped model's evidence while changing the model or effort it runs.
        expected = config.BUILTIN_MODELS.get(name, entry)
        short = [k for k, v in floors.items() if p and (p["scores"].get(k) is None or p["scores"][k] < v)]
        effort = entry.get("default_reasoning_effort")
        if name in config.DISABLED_MODELS or not config.provider_enabled(entry["provider"]):
            why = ("disabled", "switched off in settings")
        elif not p:
            why = ("evidence", f"no published scores for {entry['benchmark_slug']}")
        elif name in excluded or p["slug"] in excluded or entry.get("api_id") in excluded:
            why = ("excluded", "named in exclude_models")
        elif any(entry.get(k) != expected.get(k) for k in identity):
            why = ("identity", "a user file changed model or effort: " + ", ".join(k for k in identity if entry.get(k) != expected.get(k)))
        elif short:
            why = ("floor", "below " + ", ".join(f"{k} {floors[k]}" for k in short))
        elif reasoning_effort is not None and effort != reasoning_effort:
            why = ("effort", f"runs at {effort}, not {reasoning_effort}")
        elif thinking is False and entry.get("default_thinking") is not False:
            why = ("thinking", "cannot run with thinking off")
        elif not config.is_tool_free(tools) and not entry.get("tools"):
            why = ("tools", "no tool calling")
        elif vision and not entry.get("vision"):
            why = ("vision", "cannot read images")
        elif min_context and entry.get("ctx", 0) < min_context:
            why = ("context", f"ctx {entry.get('ctx', 0)} < {min_context}")
        elif backend == "cc" and (not config.PROVIDERS[entry["provider"]].get("anthropic_url") or not entry.get("cc_effort")):
            why = ("backend", "no verified Anthropic endpoint and effort transport for cc")
        elif usable is not None and not usable(entry["provider"]):
            why = ("usable", f"{entry['provider']} has no key ready")
        else:
            why = None
        if why:
            rejected.append({"model": name, "filter": why[0], "reason": why[1]})
            continue
        factor = .5 if entry["provider"] == "deepseek" and not config.is_peak() else 1
        candidates.append({"model": name, "reasoning_effort": effort, "thinking": entry.get("default_thinking"),
                           "benchmark_slug": p["slug"], "score": p["score"], "benchmark_cost_usd": p["cost"]*factor,
                           "source": p["source"], "scores": {k: p["scores"][k] for k in floors},
                           "rates": config.price(name), "provider": entry["provider"], "configuration": p["name"]})
    candidates.sort(key=lambda c: (priority_rank(c["model"]), c["benchmark_cost_usd"], -c["score"], c["model"]))
    candidates += _last_resort(candidates, excluded=excluded, usable=usable, min_context=min_context, profile=profile,
                               tools=tools, backend=backend, vision=vision, strict=reasoning_effort is not None or bool(min_scores))
    return {"profile": profile, "evidence_date": data["as_of_utc"], "candidates": candidates,
            "requirements": floors, "rejected": rejected,
            "explanation": "Cheapest observed benchmark cost among configurations meeting every task score floor; route availability is checked separately.",
            "warnings": ["Floors are operational policy, not a guarantee; the orchestrator verifies task acceptance.",
                         "Benchmark cost is a workload estimate, not the production bill; prices and provider behavior can change.",
                         "CritPt is under review and excluded from eligibility; cc admits only verified effort transport."]}


# ---- load bias: steer the ORDER of capable candidates by live load, never their evidence ----
# The idea is DeepSeek-V3's MoE gate (a per-expert bias picks the top-k, the unbiased score weighs them), written
# fresh here: per provider, this process counts the tasks committed to it and its recent slow/saturated marks.
_INFLIGHT: dict[str, int] = {}
_SLOW: dict[str, list[float]] = {}
_SLOW_ERRORS = ("SlowLeg:", "PoolSaturated")


def claim(provider):
    if provider:
        _INFLIGHT[provider] = _INFLIGHT.get(provider, 0) + 1


def release(provider):
    if provider and _INFLIGHT.get(provider):
        _INFLIGHT[provider] -= 1


def note_result(provider, error, now=None):
    """A leg that crawled or found every key resting counts against its provider for config.SLOW_MARK_S."""
    if provider and error and any(s in error for s in _SLOW_ERRORS):
        _SLOW.setdefault(provider, []).append(time.monotonic() if now is None else now)


def reset_load():
    _INFLIGHT.clear(), _SLOW.clear()


def pressure(provider, capacity, now=None):
    """0..1: committed tasks plus recent slow marks over what the provider's usable keys can carry."""
    from . import config

    now = time.monotonic() if now is None else now
    marks = _SLOW[provider] = [t for t in _SLOW.get(provider, ()) if now - t < config.SLOW_MARK_S]
    return min(1.0, (_INFLIGHT.get(provider, 0) + len(marks)) / max(1, capacity))


def rebias(candidates, pressure_of):
    """Re-rank by benchmark cost x (1 + LOAD_BIAS x pressure). Each keeps its unbiased benchmark_cost_usd and says
    the bias it was ranked with; the sort is stable, so with no load the plan's own order stands. Unevidenced
    siblings (_last_resort) stay behind every evaluated candidate, starred or not: a star orders, it never admits."""
    from . import config

    out = [{**c, "load_bias": round(config.LOAD_BIAS * pressure_of(c["provider"]), 4) if c.get("provider") else 0.0}
           for c in candidates]
    out.sort(key=lambda c: (bool(c.get("unevidenced")), priority_rank(c["model"]),
                            (c.get("benchmark_cost_usd") or 0.0) * (1 + c["load_bias"]), -(c.get("score") or 0)))
    return out
