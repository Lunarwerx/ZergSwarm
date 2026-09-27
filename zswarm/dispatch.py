"""Execute published-evidence plans without resetting a task's budgets or completed tools."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import time

from . import breaker, config, keys, selection
from .client import NoUsableKey
from .spec import Result, add_spend, now_iso


def _wake(provider):
    """Seconds until the provider's pool can take a request (inf: every key disabled), or None with no key. Read from
    the pool's cached state, the same view `pick` uses: `disabled()` re-reads the whole machine-wide key file, and a
    plan asks once per candidate route, which made one plan cost 0.56 s and put minutes of a 395-task job's event
    loop into planning (measured 2026-09-25, 4,287 OpenRouter keys)."""
    pool = keys.pool_for(provider)
    return None if pool is None else pool.soonest_wake()


def _usable(provider, wake=_wake):
    w = wake(provider)
    return w is not None and w <= config.SLOW_LEG_TURN_S


def _capacity(provider, gates=None):
    # What the provider can carry at once. Its LegGate's live width when one exists: that ramps up from
    # LEG_RAMP_START and halves on 429s, so the key ceiling alone understates load exactly when a leg is ramping
    # or throttled. Before any gate exists, the ceiling that gate would be capped at (jobs._gate_for).
    pool = keys.pool_for(provider)
    per_key = int(config.PROVIDERS.get(provider, {}).get("live_per_key") or config.LIVE_PER_KEY)
    ceiling = (len(pool) - len(pool.disabled())) * per_key if pool else 0
    gate = (gates or {}).get(provider)
    return min(gate.limit, ceiling) if gate is not None and ceiling else ceiling


def _pressure(provider, gates=None):
    return selection.pressure(provider, _capacity(provider, gates))


def _alive(provider, wake=_wake):
    """A key that is not disabled: the pool may be resting on 429s, but a queued task can still be served."""
    w = wake(provider)
    return w is not None and w < float("inf")


def _memo_wake():
    """One pool read per provider for one planning pass, however many of its routes the plan weighs."""
    seen = {}

    def wake(provider):
        if provider not in seen:
            seen[provider] = _wake(provider)
        return seen[provider]

    return wake


def _why_not(provider, wake):
    w = wake(provider)
    if w is None:
        return "no key on this machine"
    if w == float("inf"):
        return f"all {len(keys.pool_for(provider))} keys disabled"
    return f"every key resting, the soonest free in {w:.0f}s"


def _dead(pool):
    """Every key of the leg's pool is disabled (out of credit, revoked), read from the machine-wide key state at
    the moment the leg would start. Job 20260925-143635-e3f6: 23 tasks planned DeepSeek before any of them had
    learnt its keys were out of credit, so each paid a try per dead leg; a leg another task has already found
    dead now costs zero seconds. A test's fake client has no pool, and is never dead."""
    return pool is not None and hasattr(pool, "disabled") and len(pool) <= len(pool.disabled())


def _plan_here(profile, *, min_context, wake=_wake, **kw):
    """The evaluated plan over pools that can answer now; when nothing can (every candidate pool is resting on
    429s), the plan over pools that are merely busy, so the task queues on its gate instead of being refused
    as NoCapableSwarmRoute at width (the `cc` smoke of 2026-09-25)."""
    plan = selection.plan(profile, usable=lambda p: _usable(p, wake), min_context=min_context, **kw)
    if not plan["candidates"]:
        busy = selection.plan(profile, usable=lambda p: _alive(p, wake), min_context=min_context, **kw)
        if busy["candidates"]:
            return busy
    return plan


# Owner, Michael, 2026-09-25: "When something is out of keys, we always advance to the next model in the list
# instead of just... stop." A profile whose every evaluated route is out of keys (decision and code that day:
# DeepSeek spent, every OpenRouter key 402) steps down to the next profile whose models can still answer,
# instead of NoCapableSwarmRoute. The plan keeps the profile asked for, names the one it ran at in `below_floor`
# and says so in its warnings; the orchestrator's acceptance check stays the judge of the answer.
_STEP_DOWN = {"critical": "code", "code": "general", "decision": "general", "research": "general", "general": "routine"}


def _plan(profile, *, min_context, explain=False, **kw):
    """`explain` adds `unavailable`: each route the asked profile's evidence admits that its pool keeps out of the
    plan, with why (all keys disabled, every key resting, no key here) - what zswarm_select shows and what a
    submit refusal names."""
    wake = _memo_wake()
    plan, at = _plan_here(profile, min_context=min_context, wake=wake, **kw), profile
    while not plan["candidates"] and at in _STEP_DOWN:
        at = _STEP_DOWN[at]
        if at == "routine" and not config.is_tool_free(kw.get("tools", "none")):
            break  # routine is tool-free only; tool work has nowhere lower to go
        lower = _plan_here(at, min_context=min_context, wake=wake, **kw)
        if lower["candidates"]:
            plan = dict(lower, profile=profile, below_floor=at, requirements_asked=plan.get("requirements"),
                        warnings=[f"No {profile} route had a usable key, so this ran on the {at} profile's models, "
                                  f"below the {profile} floors: verify the answer before accepting it."] + lower.get("warnings", []))
            break
    if explain:
        chosen = {c["model"] for c in plan["candidates"]}
        plan["unavailable"] = [{"model": c["model"], "provider": c["provider"], "why": _why_not(c["provider"], wake)}
                               for c in selection.plan(profile, min_context=min_context, **kw)["candidates"]
                               if c["model"] not in chosen]
    return plan


def plan_for(task, explain=False):
    """The plan `run_selected` runs for an evaluated-profile task. Parsing pins the task's first leg from it too, so
    a job's record names the route it will take: jobs 20260925-184445-98ac and -184659-a701 read
    `rank:gpt-6-luna-high` (OpenRouter, 4,287 of 4,287 keys disabled) on every pending task while their plan could
    only ever run Gemini."""
    return _plan(task.profile, tools=task.tools, backend=task.backend, reasoning_effort=task.reasoning_effort,
                 thinking=task.thinking, min_scores=task.min_scores, exclude_models=task.exclude_models,
                 vision=task.role == "vision", explain=explain,
                 min_context=(len(task.prompt) + len(task.system or "")) // 3 + task.max_tokens)


def first_choice(profile, *, tools="none", backend="api", vision=False):
    """The model AUTO runs first for this profile on this machine now: the head of the plan over the pools that can
    serve (stepping down like a task does), else the evidence's own head, so a caller with no key names what it
    would need. Every model pick that is not a batch task - a role, the panel, the doctor's routes, a bare `auto` -
    comes through here, so it lands on a provider this machine has a key for. Before 2026-09-26 they took the
    evidence's head alone: a machine with only a Groq key had its judge, doubt and panel pinned to OpenRouter and
    DeepSeek models it could not call."""
    live = _plan(profile, tools=tools, backend=backend, vision=vision, min_context=0)["candidates"]
    if live:
        return live[0]["model"]
    listed = selection.plan(profile, tools=tools, backend=backend, vision=vision)["candidates"]
    if not listed:
        raise ValueError(f"NoCapableSwarmRoute: no evaluated model supports profile {profile} (tools {tools}, backend {backend})")
    return listed[0]["model"]


def default_panel(size=2):
    """The blind panel when settings.toml names none: the first `size` tool-free picks this machine can serve, general
    first and then routine (a Groq key alone has one general model and a second only at routine), one per model maker
    where the plans offer several (a panel of one maker's training agrees with itself), then any distinct model.
    Fewer than two is an error naming what to add."""
    by_slug = {p["slug"]: p for p in selection.evidence()["points"]}
    candidates = [c for profile in ("general", "routine") for c in _plan(profile, tools="none", min_context=0)["candidates"]]

    def maker(c):
        return by_slug.get(c.get("benchmark_slug"), {}).get("creator") or c["model"]

    out, makers = [], set()
    for c in candidates:
        if len(out) < size and maker(c) not in makers:
            out.append(c["model"])
            makers.add(maker(c))
    for c in candidates:
        if len(out) < size and c["model"] not in out:
            out.append(c["model"])
    if len(out) < 2:
        raise ValueError("a panel needs two models this machine can call, and the keys here reach "
                         f"{len(out)}: add a key for another provider (zswarm ui), or name the panel with `panel = [...]` "
                         f"in {config.SETTINGS_FILE}")
    return out


def unreachable(task):
    """Why no evaluated route, stepped down or not, can take this task, or None. Only when the machine HAS keys for
    routes the profile admits and every one of them is disabled: a route with no key here is the run's
    NoCapableSwarmRoute to report, and a resting pool queues (_plan_here), so neither refuses a job."""
    plan = plan_for(task, explain=True)
    if plan["candidates"]:
        return None
    dead: dict[str, list[str]] = {}
    for u in plan["unavailable"]:
        if u["why"].startswith("all "):
            dead.setdefault(f"{u['provider']} {u['why']}", []).append(u["model"])
    if not dead:
        return None
    return (f"profile {task.profile} (tools {task.tools}, backend {task.backend}): "
            + "; ".join(f"{p} ({', '.join(m[:3])}{f' and {len(m) - 3} more' if len(m) > 3 else ''})" for p, m in dead.items()))


def route_outlook(tasks, gates=None):
    """What `zswarm_run` says in its FIRST response about the routes its evaluated-profile tasks can take: a
    profile none can serve, a profile with ONE provider left whose pool is resting or whose keys are all
    disabled, and a width wider than that provider's live-call cap (the tasks queue at its gate; a queued task's
    clock has not started). Empty when every profile has a live choice. Job 20260925-143635-e3f6 learnt it
    after a ten-minute timeout; the orchestrator should know in seconds."""
    notes, seen = [], set()
    width: dict[tuple, int] = {}
    for t in tasks:
        if getattr(t, "profile", None):
            key = (t.profile, t.tools, t.backend)
            width[key] = width.get(key, 0) + 1
    for t in tasks:
        if not getattr(t, "profile", None):
            continue
        key = (t.profile, t.tools, t.backend)
        if key in seen:
            continue
        seen.add(key)
        kw = dict(tools=t.tools, backend=t.backend, reasoning_effort=t.reasoning_effort, thinking=t.thinking,
                  min_scores=t.min_scores, exclude_models=t.exclude_models, vision=t.role == "vision")
        label = f"profile {t.profile} (tools {t.tools}, backend {t.backend})"
        wake = _memo_wake()
        plan = selection.plan(t.profile, usable=lambda p: _alive(p, wake), min_context=0, **kw)
        providers = sorted({config.provider_of(c["model"]) for c in plan["candidates"]})
        if not providers:
            notes.append(f"{label}: no evaluated route has a usable key; its tasks will fail NoCapableSwarmRoute")
            continue
        if len(providers) > 1:
            continue
        p = providers[0]
        pool = keys.pool_for(p)
        ready = pool.available() if pool is not None else 0
        note = f"{label}: {p} is the only provider left ({len(plan['candidates'])} route(s))"
        if ready == 0 and pool is not None:  # a provider with no key pool here has no keys to rest
            note += (f"; every {p} key is resting on a rate limit, so tasks queue on its gate "
                     f"(the soonest key wakes in {pool.soonest_wake():.0f} s)")
        gate = (gates or {}).get(p)
        cap = gate.cap if gate is not None else len(pool or ()) * int(config.PROVIDERS.get(p, {}).get("live_per_key") or config.LIVE_PER_KEY)
        if width[key] > cap:
            note += f"; {width[key]} tasks exceed its live-call cap of {cap}, the rest wait at the gate without spending their timeout"
        notes.append(note)
    return notes


def _failure(task_id, backend, model, message, plan):
    return Result(id=task_id, backend=backend, model=model, status="error", error=message,
                  finished=now_iso(), selection=plan)


def _fold(result, attempts, plan):
    previous = attempts[:-1]
    add_spend(result, previous, poison=True)  # unknown billing anywhere makes the total unknown
    for old in previous:
        result.tool_calls += old.tool_calls
        result.files_changed = sorted(set(result.files_changed + old.files_changed))
        result.upstream = list(dict.fromkeys(old.upstream + result.upstream))
    result.failover = list(dict.fromkeys(r.model for r in previous if r.model != result.model))
    if previous:  # a later leg served (taint F), and a dead leg's own letters stay on the answer: taints are sticky
        result.add_taint("F", *(r.taint for r in previous))
    result.selection = {**plan, "attempts": [{"model": r.model, "status": r.status, "error": r.error} for r in attempts]}
    result.selection["selected"] = next((c for c in plan["candidates"] if c["model"] == result.model), None)
    return result


def _resume(messages, provider):
    # Cross-provider handoff keeps executed tool calls/results. Vendor-specific reasoning payloads
    # cannot be transplanted. Never ask a new worker to repeat the completed filesystem operations.
    saved = [{k: copy.deepcopy(v) for k, v in m.items() if k not in ("reasoning_content", "reasoning", "reasoning_details")}
             for m in messages]
    if provider == "deepseek":
        for message in saved:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                message["reasoning_content"] = ""  # required carrier, never fabricate another model's reasoning
    if provider == "gemini":
        # Gemini 400s ("Function call is missing a thought_signature") on any earlier tool call it did not make
        # itself, so a task that failed over INTO Gemini died on its first turn (12 of 12 SUE donors,
        # 2026-09-25, after Cerebras 429 and Groq 413). Google's documented value for calls injected from
        # another model is skip_thought_signature_validator; a call that already carries a signature keeps it.
        for message in saved:
            for call in message.get("tool_calls") or ():
                google = call.setdefault("extra_content", {}).setdefault("google", {})
                google.setdefault("thought_signature", "skip_thought_signature_validator")
    return saved


class _Tripped(RuntimeError):
    """The leg's provider is known saturated from outside this job (LegGate.trip), so the leg never starts."""


async def _unless_tripped(gate, run):
    """Await `run` unless the provider was found saturated while this task queued at its gate."""
    if gate is not None and (why := gate.tripped()):
        run.close()
        raise _Tripped(why)
    return await run


@dataclasses.dataclass
class _Run:
    """One evaluated-profile task's walk down its legs: what every leg reads, and what it leaves the next one. `gate`,
    `served` and `tripped` are the current leg's, kept here so the requeue check reads them even when the leg raised."""
    mgr: object
    job: object
    task: object
    warm: object
    is_pilot: bool
    plan: dict
    candidates: list
    terminal: int
    budget: object = None
    attempts: list = dataclasses.field(default_factory=list)
    messages: list | None = None
    transcripts: list = dataclasses.field(default_factory=list)
    requeues: int = 0
    gate: object = None
    served: object = None
    tripped: bool = False


def _legs(plan, task):
    return plan["candidates"] if task.route else plan["candidates"][:1]


def _leg_key(task, model):
    # cc speaks a provider's Messages endpoint, so its breakers are its own ("cc:<leg>").
    return f"cc:{model}" if task.backend == "cc" else model


def _ordered(plan, task, gates):
    """(plan, candidates, terminal): the legs rebiased by provider pressure, then ordered by the circuit breaker as it
    does a pinned route's (jobs._run_legs): an open leg goes last, and the real last leg is the last one no closed
    leg follows, so a healthy fallback moved ahead of it runs untimed."""
    plan = {**plan, "candidates": selection.rebias(plan["candidates"], lambda p: _pressure(p, gates))}
    candidates = _legs(plan, task)
    terminal = len(candidates) - 1
    if task.route:
        front, back = breaker.split([_leg_key(task, c["model"]) for c in candidates])
        rank = {k: n for n, k in enumerate(front + back)}
        candidates = sorted(candidates, key=lambda c: rank[_leg_key(task, c["model"])])
        plan = {**plan, "candidates": candidates}
        terminal = len(front) - 1 if front else terminal
    return plan, candidates, terminal


def _used(task, attempts):
    """(spent, ran, turns, remaining) over the legs run so far."""
    spent = sum(r.cost_usd or 0 for r in attempts)
    ran = sum((r.seconds or 0) - (r.rested_s or 0) for r in attempts)  # 429 waits are queue time too
    turns = sum(r.turns for r in attempts)
    remaining = task.max_cost_usd - spent if task.max_cost_usd else 0
    return spent, ran, turns, remaining


def _budget_stop(run, used):
    """The failure that ends the walk before the next leg when the task's or the job's budget is spent, else None."""
    task, job = run.task, run.job
    spent, ran, turns, remaining = used
    if ran >= task.timeout_s or turns >= task.max_turns or (task.max_cost_usd and remaining <= 0):
        return _failure(task.id, task.backend, task.model, "task budget exhausted across Swarm routes", run.plan)
    if job.budget_usd is not None and job.cost() + spent >= job.budget_usd:
        return _failure(task.id, task.backend, task.model, "job budget exhausted before Swarm escalation", run.plan)
    return None


async def _start_leg(run, child, leg, timed):
    """Run one leg to its (result, transcript). Raises what the leg raised, with run.gate/served set as far as it got."""
    from .agent import run_api_task

    mgr, job = run.mgr, run.job
    client = mgr.client_for(leg)
    if _dead(getattr(client, "pool", None)):
        # Another task already found every key of this leg disabled: zero seconds, straight to the next leg.
        raise NoUsableKey(f"every {client.pool.provider} key is disabled; leg skipped before it started")
    await mgr._park_broke_keys(client)
    if run.task.backend == "cc":
        if run.attempts:
            child.prompt += "\n\nContinue only unfinished work. Prior attempts may have edited files; inspect current files first.\n" + (run.attempts[-1].answer or "")
        return await mgr._run_cc_with_rotation(child, client.pool)
    run.gate = mgr._gate_for(leg, client)
    run.served = run.gate.served if run.gate is not None else None
    res, transcript = await mgr._gated(job, run.gate, _unless_tripped(run.gate, run_api_task(
        client, child, warm=run.warm, is_pilot=run.is_pilot, user_tag=job.id,
        slow_turn_s=config.SLOW_LEG_TURN_S if timed else None,
        resume_messages=_resume(run.messages, config.provider_of(leg)) if run.messages else None,
        **({"job_budget": run.budget} if run.budget is not None else {}))))
    run.messages = transcript
    return res, transcript


async def _try_leg(run, candidate, timed, used):
    """One pass over `candidate`: claim its provider, run it, record the outcome. Returns the leg's result."""
    from .jobs import leg_unavailable

    task, plan = run.task, run.plan
    _spent, ran, turns, remaining = used
    leg = candidate["model"]
    child = dataclasses.replace(task, model=leg, profile=None,
                                reasoning_effort=candidate.get("reasoning_effort"), thinking=candidate.get("thinking"),
                                max_turns=task.max_turns-turns, max_cost_usd=remaining, timeout_s=max(.01, task.timeout_s-ran))
    run.job.results[task.id].model = leg
    run.gate, run.served, run.tripped = None, None, False
    # Claimed on every pass, the PoolSaturated requeue included, and released in the finally: no claim leaks.
    selection.claim(candidate.get("provider"))
    try:
        res, transcript = await _start_leg(run, child, leg, timed)
    except _Tripped as exc:
        res, transcript, run.tripped = _failure(task.id, task.backend, leg, f"PoolSaturated: {exc}", plan), None, True
    except RuntimeError as exc:
        res = _failure(task.id, task.backend, leg, f"NoUsableKey: {exc}", plan)
        transcript = None
    finally:
        selection.release(candidate.get("provider"))
    selection.note_result(candidate.get("provider"), res.error)
    selection.note_speed(leg, res)
    if task.route and not run.tripped:
        breaker.record(_leg_key(task, leg), res, leg_unavailable(res))
    run.attempts.append(res)
    run.transcripts.append({"model": leg, "transcript": transcript})
    run.is_pilot = False
    return res


def _requeue(run, res, leg, last):
    """True when the task goes back through the last leg's gate; when that gate served nothing during the wait, trips
    it and says so on `res` instead."""
    task = run.task
    if not (last and task.backend != "cc" and not run.tripped and "PoolSaturated" in (res.error or "")
            and run.requeues < config.SATURATED_REQUEUES):
        return False
    if run.gate is None or run.gate.served > run.served:
        # The last leg's pool is serving, just not at this width: its 429s halved the provider's gate, so the
        # task goes back through that gate with its transcript kept, instead of failing. Width is the caller's
        # ceiling, not a promise the provider can keep (job 20260925-145240-b39d: 19 of 23 died PoolSaturated).
        run.requeues += 1
        return True
    # Nothing this process sent the provider was answered for the whole wait: the pool is saturated from
    # outside the job, and a requeue only waits again. Jobs 20260925-184445-98ac and -184659-a701 (Gemini,
    # 429 "request limit per minute for a region" on all 760 keys) requeued every task six times, 14 minutes
    # each with nothing to show, while their status read `pending`. The trip fails the next tasks at once.
    provider = config.provider_of(leg)
    run.gate.trip(f"no {provider} call from this process was answered during task {task.id}'s "
                  f"{config.SATURATED_REST_S:.0f} s wait, so its pool is saturated from outside this job; its tasks "
                  f"fail at once until a call succeeds or {config.SATURATED_REST_S:.0f} s pass", config.SATURATED_REST_S)
    res.error = (f"{res.error} [not requeued: no {provider} call from this process was answered during the wait, "
                 f"so the pool is saturated from outside this job; evaluated routes: "
                 f"{', '.join(c['model'] for c in run.candidates)}]")
    return False


async def _walk_legs(run):
    """Run the legs in order until one serves, fails for a reason another leg cannot fix, or a budget is spent.
    The task's clock is its RUN time: the seconds its legs actually ran (Result.seconds). Waiting on a
    provider's gate and the key-balance probe are queue time and spend none of it (leggate.py's contract).
    Job 20260925-143635-e3f6: 23 tasks at width 24 spent their whole 600 s queued behind dead DeepSeek legs
    and a ramping Gemini gate, and ended `task timeout exhausted` with zero turns."""
    from .jobs import leg_unavailable

    res, i = None, 0
    while i < len(run.candidates):
        if any(r.cost_usd is None for r in run.attempts):
            break  # unknown billing cannot safely authorize another paid attempt
        used = _used(run.task, run.attempts)
        stop = _budget_stop(run, used)
        if stop is not None:
            res = stop
            run.attempts.append(res)
            break
        candidate, last = run.candidates[i], i == len(run.candidates) - 1
        # a crawling leg fails over only while a closed leg follows it
        res = await _try_leg(run, candidate, i < run.terminal, used)
        if _requeue(run, res, candidate["model"], last):
            continue
        if not leg_unavailable(res):
            break  # semantic/task failures need the orchestrator's acceptance check, never blind edit replay
        i += 1
    return res


async def run_selected(mgr, job, task, warm, is_pilot):
    from .jobs import settle_too_large

    plan = plan_for(task)
    candidates = _legs(plan, task)
    if not candidates:
        return _failure(task.id, task.backend, task.model,
                        "NoCapableSwarmRoute: no available evaluated route meets the task requirements; "
                        "desktop fallback requires an explicit access/capability reason", plan), None
    job.results[task.id].model = candidates[0]["model"]  # before any wait: a status read names the leg it will take
    if warm is not None and not is_pilot:
        await warm.wait()
    # Ranked AFTER the pilot gate and with no await before the first leg's claim, so every task of a batch sees the
    # claims of the tasks ahead of it and a near-equal leg takes the overflow before the cheapest one saturates.
    plan, candidates, terminal = _ordered(plan, task, getattr(mgr, "_gates", None))
    run = _Run(mgr=mgr, job=job, task=task, warm=warm, is_pilot=is_pilot, plan=plan, candidates=candidates,
               terminal=terminal,
               budget=getattr(mgr, "_budgets", {}).get(job.id))  # the job's budget_usd ceiling, reserved against before every api turn
    res = _fold(await _walk_legs(run), run.attempts, run.plan)
    settle_too_large(res, [(r.model, r.error or "") for r in run.attempts], task.tools == "none")
    return res, {"routes": run.transcripts}


async def ask_selected(mgr, prompt, *, profile="general", route=True, **kw):
    from .agent import ask, image_part
    from .jobs import leg_unavailable

    min_scores = kw.pop("min_scores", None)
    exclude = kw.pop("exclude_models", ())
    max_cost = float(kw.pop("max_cost_usd", .25))
    timeout = float(kw.pop("timeout_s", 120))
    plan = _plan(profile, tools="none",
                 reasoning_effort=kw.get("reasoning_effort"), thinking=kw.get("thinking"),
                 min_scores=min_scores, exclude_models=exclude, vision=bool(kw.get("images")),
                 min_context=(len(prompt)+len(kw.get("system") or ""))//3 + kw.get("max_tokens", 16000))
    gates = getattr(mgr, "_gates", None)
    plan = {**plan, "candidates": selection.rebias(plan["candidates"], lambda p: _pressure(p, gates))}
    candidates = plan["candidates"] if route else plan["candidates"][:1]
    if route:  # an open leg goes last (breaker.py), as on ask_routed's pinned route
        rank = {m: n for n, m in enumerate(breaker.order([c["model"] for c in candidates]))}
        candidates = sorted(candidates, key=lambda c: rank[c["model"]])
        plan = {**plan, "candidates": candidates}
    if not candidates:
        return _failure("ask", "api", config.AUTO, "NoCapableSwarmRoute: no available evaluated model meets profile " + profile, plan)
    if kw.get("images"):
        kw["images"] = [image_part(i)["image_url"]["url"] for i in kw["images"]]
    start, attempts = time.monotonic(), []
    for n, candidate in enumerate(candidates):
        if (max_cost and sum(r.cost_usd or 0 for r in attempts) >= max_cost) or time.monotonic()-start >= timeout:
            res = _failure("ask", "api", candidate["model"], "ask budget exhausted across Swarm routes", plan)
            attempts.append(res)
            break
        leg_kw = dict(kw, reasoning_effort=candidate.get("reasoning_effort"), thinking=candidate.get("thinking"))
        # A leg with another behind it gets an even share of what is left over the legs still to try, and never more
        # than config.SLOW_LEG_TURN_S, the run path's crawl bar for one turn (all an ask is): every leg of the route
        # gets its turn inside the budget, and one that is alive but crawling fails over. On 2026-09-27 GLM 5.3 Flash
        # on NVIDIA took every Dredd boardroom seat's whole 120 s; on that seat's 13k-character prompt the five free
        # NVIDIA legs took 22 s to over 240 s and Groq's and Gemini's 4 to 9 s, so 30 s a leg spent the budget on
        # the free legs alone, and halving what was left gave the sixth leg (Groq, 4 s) 2 s.
        left = timeout - (time.monotonic() - start)
        cap = left if n == len(candidates) - 1 else min(left / (len(candidates) - n), config.SLOW_LEG_TURN_S)
        selection.claim(candidate.get("provider"))
        try:
            client = mgr.client_for(candidate["model"])
            if _dead(getattr(client, "pool", None)):
                raise NoUsableKey(f"every {client.pool.provider} key is disabled; leg skipped before it started")
            res = await asyncio.wait_for(ask(client, prompt, model=candidate["model"], **leg_kw), max(.01, cap))
        except asyncio.TimeoutError:
            res = _failure("ask", "api", candidate["model"],
                           f"SlowLeg: {candidate['model']} did not answer in {cap:.0f} s; the next leg has {left - cap:.0f} s"
                           if cap < left else "ask timeout exhausted across Swarm routes", plan)
            res.cost_usd = None  # a cancelled HTTP request may still have been billed
        except RuntimeError as exc:
            res = _failure("ask", "api", candidate["model"], f"NoUsableKey: {exc}", plan)
        finally:
            selection.release(candidate.get("provider"))
        selection.note_result(candidate.get("provider"), res.error)
        selection.note_speed(candidate["model"], res)
        if route:
            breaker.record(candidate["model"], res, leg_unavailable(res))
        attempts.append(res)
        if not leg_unavailable(res):
            break
    return _fold(res, attempts, plan)
