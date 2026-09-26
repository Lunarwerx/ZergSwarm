"""Job manager: a batch of tasks fanned out concurrently, journaled to disk, costed.

Concurrency is one asyncio Semaphore per backend. The first `api` task of a job is the
pilot: it runs alone until its first response returns, so the shared system-prompt prefix
lands in DeepSeek's cache before the rest of the zswarm fires (cache-hit input is 50x
cheaper than a miss). A job may carry `budget_usd`: every api turn reserves its worst case
against it before it is sent and settles to the exact cost after (budget.py), so a turn that
does not fit is never sent; once the summed cost still crosses it (a cc task, an unpriced
model), every task still pending is cancelled (per-task `max_cost_usd` guards one runaway
worker; the job budget guards a 2,000-task batch that is quietly wrong). Job record: job.py; ledger: ledger.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import blobs, breaker, config, escalation, review, scripted, survival, utilization, verify
from .agent import run_api_task
from .budget import Budget
from .caller import detect as detect_caller
from .caller import ledger_fields
from .cc import out_of_balance, run_cc_task
from . import keys, verdict
from .leggate import LegGate
from .client import DeepSeekClient
from .envelope import admit as admit_envelope
from .envelope import record_spend as record_tree_spend
from .job import Job, new_job_id
from .ledger import batch_argparser, ledger_summary  # noqa: F401 - re-exported for the batch tools and the CLI
from .spec import Result, Task, add_spend, merge_taint, mis_scoped, now_iso
from .worker import classify_liveness

CC_KEY_WAIT_S = 90.0  # a cc task waits this long for a resting key (a 429's Retry-After); a longer rest is a dead key, not a wait


# A leg is UNAVAILABLE when the provider could not serve the call at all - not when the work failed.
# Only these move a routed task to its next leg: no eligible endpoint (an OpenRouter host pin, or an
# account's privacy policy excluding the host), out of credit, every key disabled, the host erroring
# after the client's own retries, or the connection never forming. A worker's FAILED, a turn budget, a
# timeout or a 400 (our request) is the task's own result and is never retried on another provider.
# `API 503` is this client's rendering of a status; `API Error: 503` is Claude Code's, from a cc worker.
_UNAVAILABLE = re.compile(
    r"API (?:Error:? )?(?:402|404|408|429|502|503|504|529)\b|NoUsableKey|no endpoints|endpoints? .{0,40}available"
    r"|ConnectError|ConnectTimeout|RemoteProtocolError|PoolTimeout|keys? is disabled|every one of the \d+ \w+ keys|^SlowLeg:"
    # 429 added 2026-09-20, for the FREE-TIER pools. The client already retries and rotates every key in
    # the pool on a 429 before it ever surfaces one, so a 429 reaching THIS check means the whole pool is
    # rate-limited, not that one key was unlucky - i.e. the leg really is unavailable right now. Gemini's
    # free tier exhausts its per-day quota as a 429 (RESOURCE_EXHAUSTED), and without this the free leg
    # would dead-end instead of down-routing to the paid fallbacks. Keeping it out would make the whole
    # free-first-with-paid-fallback design fail exactly when it is supposed to save the job.
    r"|RESOURCE_EXHAUSTED|Quota exceeded"
    # A leg skipped because its provider is known saturated from outside the job (LegGate.trip) never reached the
    # provider, so it carries no status: its name is the mark.
    r"|^PoolSaturated:"
    # DeepSeek's own host refuses a request on a content filter with HTTP 400 "Content Exists Risk" and no
    # detail; measured 2026-09-19 on two read-only inventory tasks over Graphify-Labs/graphify (a README with
    # CJK translation links). That is the HOST declining the task, not the task failing, so the next leg
    # (the same open-weights model on OpenRouter or Hugging Face, which do not run that filter) gets it.
    r"|Content Exists Risk",
    re.I,
)


# A 413 "Request too large" is the REQUEST not fitting that leg's limit, not the leg failing: groq's free tier caps
# gpt-oss-120b at 8000 tokens per minute, so a 13k-token prompt never fits there, on any key. Measured 2026-09-24:
# 25 of 32 pilot tasks over ~8k tokens died on it with `failover: []`. The client raises it without resting or
# disabling the key (the key is fine), and it fails over like an unavailable leg, because the next host's limit may
# fit it. It never trips a cut-short leg (NO_CREDIT_TRIP_S): the next, smaller prompt still fits that leg.
_TOO_LARGE = re.compile(r"API (?:Error:? )?413\b.{0,200}?too large", re.I | re.S)


def too_large(res: Result) -> bool:
    return res.status == "error" and bool(_TOO_LARGE.search(res.error or ""))


# A leg that could not produce the structure the task's schema asks for (worker.SCHEMA_REJECTS rejected submit_result
# calls, or one invalid tool-free answer) fails over to the next route, never lands as ok (2026-09-25, the Gemini route
# answered 140-row translations with `{"rows": []}`). Only while it changed no file: a replay must not redo edits.
_INVALID_STRUCTURE = re.compile(r"^InvalidStructuredAnswer:")


def leg_unavailable(res: Result) -> bool:
    if res.status == "error" and _INVALID_STRUCTURE.search(res.error or ""):
        return not res.files_changed
    return res.status == "error" and (bool(_UNAVAILABLE.search(res.error or "")) or too_large(res))


def _taint_failover(res: Result, dead: list[dict]) -> None:
    """Taint F when a later leg served, and keep every dead leg's own letters: a taint is sticky across legs too."""
    if dead:
        res.add_taint("F", *(d.get("taint") or "" for d in dead))


def too_large_for_route(tried: list[tuple[str, str]], tool_free: bool) -> str:
    """The error of a task whose last leg answered 413 too large: `tried` is every (leg, error) in the order run."""
    big = [leg for leg, err in tried if _TOO_LARGE.search(err)]
    down = [leg for leg, err in tried if not _TOO_LARGE.search(err)]
    kind = "tool-free leg" if tool_free else "leg"
    return (f"PromptTooLarge: the prompt is too large for every {kind} of its route that could take it ({', '.join(big)}"
            + (f"; {', '.join(down)} could not serve" if down else "") + "), so no retry fits it: shorten or split it, "
            f"or do this work on your own model. {tried[-1][1].strip()[:300]}")


def settle_too_large(res: Result, tried: list[tuple[str, str]], tool_free: bool) -> None:
    """A task whose EVERY leg answered 413 too large (each after trying its own keys, client._ACCOUNT_LIMIT_413) has
    no leg a rerun could fit it on, so it fails now as PromptTooLarge, which needs_other_route never reruns. When a
    leg was only down, the 413 stays as it is and the task reruns: the down leg may take it once it is back (job
    20260925-235531-21ed). Measured 2026-09-26 (job 20260926-050510-e32e): a profile route whose one leg was Groq
    answered 413 and went to the dead-rerun rest for up to three hours instead of failing."""
    if too_large(res) and all(_TOO_LARGE.search(err) for _, err in tried):
        res.error = too_large_for_route(tried, tool_free)


# A route CUT SHORT by missing credit (its other legs dropped by route_plan because every key there is disabled)
# is not a route with "nowhere to go": its work goes back to the caller's own model (config.ROUTES, cheap-only).
# So the leg left is timed like any leg with a next one, and when it cannot serve in time the task errors with
# NoCreditLeft instead of crawling to its own timeout. Measured 2026-09-24, job 20260924-200352-b60d: every
# OpenRouter key out of credit left gemini-3.8-flash as the only leg, untimed; 105 of 140 tasks sat the full
# 600 s (20 of them with 0 turns, queued behind a crawling pilot). The first task to trip stops its siblings
# on that leg at once, and the leg is skipped for NO_CREDIT_TRIP_S so the next job fails in milliseconds.
NO_CREDIT_TRIP_S = 300.0
_TRIPS: dict[str, tuple[float, str]] = {}  # leg -> (time.monotonic() the trip ends, its NoCreditLeft message)


def _tripped(leg: str) -> str | None:
    """The NoCreditLeft message of a live trip on `leg`, or None."""
    until, msg = _TRIPS.get(leg, (0.0, ""))
    return msg if time.monotonic() < until else None


def _spent_legs(model: str, plan: list[str]) -> list[str]:
    """The legs of `model`'s route that route_plan dropped because their provider has no key with credit. A plan
    that is not drawn from that route (an unknown model's `[model]`) has nothing cut short."""
    route = config.routes_for(model)
    if not set(plan) <= set(route):
        return []
    return [leg for leg in route if leg not in plan and not keys.has_credit(_provider(leg))]


def _credit_note(leg: str) -> str:
    prov = _provider(leg)
    pool = keys.pool_for(prov)
    if pool is None:
        return f"{leg} ({prov}: no key on this machine)"
    off, n = len(pool.disabled()), len(pool)
    return f"{leg} ({prov}: " + (f"all {n} keys disabled)" if off == n else f"{off} of {n} keys disabled, the rest rate-limited)")


def no_credit_left(leg: str, spent: list[str], why: str) -> str:
    """The one message every task of a cut-short route gets when its last leg cannot serve in time."""
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=NO_CREDIT_TRIP_S)).strftime("%H:%M UTC")
    return (f"NoCreditLeft: {leg} cannot serve this in time ({why.strip()[:160]}), and the rest of its route is out of "
            f"credit: {', '.join(_credit_note(s) for s in spent)}. Nobody is topping these keys up, so do this work on "
            f"your own model. This zswarm process sends {leg} no more of this work until {until}.")


def _leg_health(leg: str) -> tuple[str, str]:
    """Whether `leg` can take a request within NO_CREDIT_TRIP_S - "ok", "dead" (its keys say no, or a live trip)
    or "nokey" (no key on this machine: that fails in milliseconds with NoUsableKey on its own) - and a note.
    Offline: the shared key-state file and this process's trips only, never a network call."""
    if msg := _tripped(leg):
        return "dead", f"{leg} (tripped: {msg.split('. This zswarm process', 1)[0][:200]})"
    prov = _provider(leg)
    pool = keys.pool_for(prov) if prov else None
    if pool is None:
        return "nokey", f"{leg} ({prov or 'unknown provider'}: no key on this machine)"
    wake = pool.soonest_wake(free=config.api_model_id(leg).endswith(":free"))
    if wake <= NO_CREDIT_TRIP_S:
        return "ok", f"{leg} ({prov}: " + ("ready)" if wake == 0 else f"every key resting, the soonest free in {wake:.0f}s)")
    if wake == float("inf"):
        return "dead", f"{leg} ({prov}: all {len(pool)} keys disabled)"
    return "dead", f"{leg} ({prov}: every key resting or its daily quota spent, the soonest free in {wake / 3600:.1f}h)"


def route_health(model: str) -> dict:
    """Which legs of `model`'s route can serve now: the doctor's `routes_now`, and what submit refuses on. `serves`
    is False only when some leg has keys and none can serve; a route with no key at all is NoUsableKey's to report."""
    legs = [_leg_health(leg) for leg in (config.routes_for(model) if config.PRICE_ROUTING else (config.resolve_model(model),))]
    states = {s for s, _ in legs}
    return {"serves": "ok" in states or "dead" not in states, "legs": [note for _, note in legs]}


# Owner, Michael, 2026-09-25: "ZSwarm should deal with fucking everything. It should deal with which agent to send it
# to, which keys are working ... and then rerun them if they're dead." A pinned model is the caller's PREFERENCE, not
# a reason to fail: a job pinned to a route whose every key was disabled was refused whole (NoCreditLeft), and 15 of
# 67 pinned builders died PoolSaturated after 120 s with "no other leg", while the same work on the code profile ran
# at once. So a pinned task whose route cannot serve, at submit or when its last leg gives out, is routed by its
# profile instead, skipping the legs already found dead. route=False (an A/B arm measuring that exact model) is
# never moved: a benchmark that quietly ran another model would report a difference that is not there.
def unpinned(task: Task, pinned: str, dead: list[str] = ()) -> Task | None:
    """`task` routed by its profile instead of its pinned model, skipping `dead` legs; None when it may not move
    (a profile task, route=False) or no evaluated route is left."""
    if task.profile or not task.route:
        return None
    from .dispatch import plan_for
    from .selection import profile_for

    try:
        profile = profile_for(task.role, task.tools)
    except ValueError:
        profile = profile_for(None, task.tools)
    child = dataclasses.replace(task, model=config.AUTO, profile=profile, unpinned_from=task.unpinned_from or pinned,
                                exclude_models=list(dict.fromkeys([*task.exclude_models, *dead])))
    candidates = plan_for(child)["candidates"]
    if not candidates:
        return None
    child.model = candidates[0]["model"]
    return child


def remaining(task: Task, res: Result) -> Task | None:
    """`task` with what `res`'s attempts spent taken off its run time, turns and cost, told to continue rather than
    redo; None when nothing is left to run on. Time its calls sat rate-limited (Result.rested_s) is waiting, not
    run time, so it stays on the clock."""
    left_s = task.timeout_s - ((res.seconds or 0) - (res.rested_s or 0))
    left_turns = task.max_turns - res.turns
    left_cost = (task.max_cost_usd - (res.cost_usd or 0)) if task.max_cost_usd else task.max_cost_usd
    if left_s <= 0 or left_turns <= 0 or (task.max_cost_usd and left_cost <= 0):
        return None
    child = dataclasses.replace(task, timeout_s=left_s, max_turns=left_turns, max_cost_usd=left_cost)
    if (res.files_changed or res.turns) and "Continue only unfinished work." not in child.prompt:
        child.prompt += ("\n\nContinue only unfinished work. An earlier attempt on another route may have edited "
                         "files; inspect the current files first.\n")
    return child


class _Slot:
    """A task's place in its backend's concurrency semaphore, given back while the task rests between dead reruns.
    Measured 2026-09-26 (jobs 20260926-050510-e32e, -a7f8, -ef64 and 20260926-050505-d332): the rest ran inside
    `async with sem`, so tasks asleep for up to DEAD_RERUN_REST_MAX_S at a time held every slot of their jobs at
    turns 0, nothing live and the CPU idle, and nothing else in those jobs could start for up to three hours."""

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self.sem, self.held = sem, False

    async def __aenter__(self) -> "_Slot":
        await self.sem.acquire()
        self.held = True
        return self

    async def __aexit__(self, *exc) -> None:
        self._give_back()

    def _give_back(self) -> None:
        if self.held:
            self.held = False
            self.sem.release()

    async def rest(self, seconds: float) -> None:
        """Sleep without the slot, then queue for one like any pending task. Cancelled asleep or in that queue,
        the task holds no slot, so the exit releases none."""
        self._give_back()
        await asyncio.sleep(seconds)
        await self.sem.acquire()
        self.held = True


def needs_other_route(res: Result) -> bool:
    """The route could not serve (out of keys, saturated, down, or one host's request-size cap): the work itself did
    not fail. Only a prompt too large for EVERY leg (too_large_for_route's PromptTooLarge) is the caller's to split;
    a 413 from one host is not (job 20260925-235531-21ed: groq's 8k-token cap ended 4 builders whose next leg,
    Gemini, was resting at that moment and could have taken them)."""
    if res.status != "error" or (res.error or "").startswith("PromptTooLarge:"):
        return False
    return leg_unavailable(res) or (res.error or "").startswith(("NoCreditLeft:", "NoUsableKey:"))


def unservable(tasks: list[Task]) -> str | None:
    """The refusal for a job whose routed api tasks have NO leg that can take a request in time, or None. Measured
    2026-09-24: with Gemini's free quota spent and every OpenRouter key out of credit, 19 tasks sat 440-590 s each
    before erroring, then 19 retries on the fallback failed in 0.003 s. A job nothing can serve is refused at once.

    An evaluated-profile task is refused when its whole plan, stepped down included, has keys on this machine and
    every one is disabled (dispatch.unreachable). Asked once per profile shape, of its shortest prompt: the context
    floor only narrows the plan, so when that task has no route, no task of the shape has one."""
    from .dispatch import unreachable

    shortest: dict[tuple, Task] = {}
    for t in tasks:
        if t.profile:
            shape = (t.profile, str(t.tools), t.backend, t.reasoning_effort, t.thinking, t.role == "vision",
                     tuple(sorted(t.min_scores.items())), tuple(t.exclude_models), t.max_tokens)
            if shape not in shortest or len(t.prompt) + len(t.system or "") < len(shortest[shape].prompt) + len(shortest[shape].system or ""):
                shortest[shape] = t
    for t in shortest.values():
        if why := unreachable(t):
            return (f"NoCapableSwarmRoute: no evaluated route can take a request now - {why}. `zswarm keys` shows the "
                    "pools; name a model whose provider has a live key, or do this work on your own model.")
    dead = {}
    for t in tasks:
        if t.profile:
            continue  # evaluated profiles were asked above: a cross-model plan, not the legacy two-leg route
        if t.backend == "api" and t.route and t.model not in dead:
            h = route_health(t.model)
            if not h["serves"]:
                dead[t.model] = h["legs"]
    if not dead:
        return None
    return ("NoCreditLeft: nothing on this job's route can take a request now - "
            + "; ".join(f"{m}: {', '.join(legs)}" for m, legs in dead.items())
            + ". Do this work on your own model, or run it tool-free (tools: 'none', the files inside the prompt), "
              "whose route is separate. `zswarm doctor` shows routes_now.")


def _provider(model: str) -> str:
    try:
        return config.provider_of(model)
    except (ValueError, KeyError):
        return ""  # a model the registry no longer knows still gets its ledger line


# RESUME: a re-run batch pays only for the tasks that changed. The idea (not the code) is from shareAI-lab/
# learn-claude-code's workflow runtime: key each call by a hash of what decides its answer, never by its id or
# its place in the batch, journal the key with the result, and hand a re-run the recorded answer for every key
# it has already paid for. So after a crash, a key pool running dry, or one prompt fixed in a 200-task batch,
# only the changed or unfinished tasks run again. The limits (max_turns, timeout_s, max_cost_usd) stay out of
# the key: they bound a run, they do not change what a finished ok answer means.
_KEY_FIELDS = ("prompt", "system", "cwd", "backend", "model", "tools", "schema", "roots", "thinking", "reasoning_effort", "temperature")


def _file_digest(task: Task, name: str) -> str:
    """The content hash of one inlined file (resolved under cwd, as spec.inline_files does): an edited file is a
    changed prompt, so its task must run again."""
    p = Path(name) if Path(name).is_absolute() else Path(task.cwd) / name
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError as e:
        return f"unreadable: {type(e).__name__}"


def _saved_tasks(doc: dict, folder: Path) -> list[Task]:
    """A job record's tasks as they were saved, each back on the model it was submitted with (JobManager.adopt)."""
    known, models, tasks = {f.name for f in dataclasses.fields(Task)}, doc.get("task_models") or {}, []
    for d in blobs.expand_all(doc.get("tasks") or [], blobs.blob_dir(folder)):
        t = Task(**{k: v for k, v in d.items() if k in known})
        t.model = models.get(t.id) or t.model
        tasks.append(t)
    return tasks


def _journaled(folder: Path) -> dict[str, Result]:
    """The finished answers a job's journal holds. A `cancelled` row counts as unfinished: in a record still marked
    running it was the shutdown that stopped the task."""
    fields, done = {f.name for f in dataclasses.fields(Result)}, {}
    journal = folder / "results.jsonl"
    for line in journal.read_text(encoding="utf-8").splitlines() if journal.exists() else ():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # the line being written when the server stopped
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row.get("status") not in (None, "pending", "running", "cancelled"):
            done[row["id"]] = Result(**{k: v for k, v in row.items() if k in fields})
    return done


def _honour_cancel(job: Job, asked: Path) -> Job:
    """Finish an adopted job as cancelled: its caller asked through the job folder while no server was running it."""
    reason = asked.read_text(encoding="utf-8").strip() or "cancelled"
    for t in job.tasks:
        job.results.setdefault(t.id, Result(id=t.id, status="cancelled", error=reason, backend=t.backend, model=t.model))
    job.state, job.finished = "cancelled", now_iso()
    job.save()
    job._done.set()
    asked.unlink(missing_ok=True)
    return job


def task_key(task: Task) -> str:
    """The stable content hash of a normalised task (taken at submit, before routing moves `model` to a leg)."""
    spec = {k: getattr(task, k) for k in _KEY_FIELDS}
    spec["files"] = [[f, _file_digest(task, f)] for f in task.files]
    if getattr(task, "runtime", ""):  # the same /work in two containers is two trees; a host task's key is unchanged
        spec["runtime"] = task.runtime
    raw = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def resumable(job_id: str) -> dict[str, dict]:
    """key -> the last status=ok result journaled under that key in job `job_id` (its folder or its archive). Only
    an ok answer is reused: an error, timeout or cancel is exactly the work a resume is for. A job journaled before
    keys existed has none to offer; an unknown job id is refused by name rather than resuming from nothing."""
    from . import archive

    # A job id is a folder name under JOBS_DIR: a path-shaped one ("../..") must not read a journal outside it.
    if not re.fullmatch(r"[\w-]+", job_id or ""):
        raise ValueError(f"resume_from_job {job_id!r}: not a job id (zswarm_jobs lists them)")
    raw = archive.read_text(job_id, "results.jsonl")
    if raw is None:
        if archive.read_json(job_id, "job.json") is None:
            raise ValueError(f"resume_from_job {job_id!r}: no such job on this machine (zswarm_jobs lists them)")
        return {}
    out: dict[str, dict] = {}
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a job cut off mid-append leaves a torn last line
        if isinstance(row, dict) and row.get("status") == "ok" and row.get("key"):
            out[row["key"]] = row
    return out


def _cached_result(task: Task, row: dict, job_id: str) -> Result:
    """This job's result for a task answered in an earlier job: the answer and data as recorded, at no cost to this
    job (that spend is already in the ledger once). `cached_from` names the job that ran it, through any chain of
    resumes, because that is where its transcript lives."""
    stamp = now_iso()
    return Result(id=task.id, status="ok", backend=row.get("backend") or task.backend, model=row.get("model") or task.model,
                  answer=row.get("answer") or "", data=row.get("data"), cost_usd=0.0, upstream=list(row.get("upstream") or []),
                  files_changed=list(row.get("files_changed") or []), acceptance=list(row.get("acceptance") or []),
                  cached_from=row.get("cached_from") or job_id,
                  started=stamp, finished=stamp, taint=merge_taint(row.get("taint") or ""))  # a resume never clears a taint
CANCEL_REQUEST = "cancel"  # a file in a job's folder: another process asking the one running the job to cancel it


class JobManager:
    def __init__(self, client: DeepSeekClient | None = None):
        self._client = client  # the default provider's (DeepSeek) client; tests inject a fake
        self._clients: dict[str, DeepSeekClient] = {}  # one client per other provider, built on first use
        self.jobs: dict[str, Job] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._sem_sizes: dict[str, int] = dict(config.DEFAULT_CONCURRENCY)
        self._api_key: str | None = None
        self._probe_lock = asyncio.Lock()
        self.cc_rotations = 0  # cc tasks re-run on another key after a 402
        self.parked_keys: list[str] = []  # fingerprints DISABLED by the last balance probe (kept under its old name)
        self.probe_error: str | None = None  # the last balance probe that raised, for `doctor`; the job went on
        self._exposed: dict[str, dict[str, str]] = {}  # job id -> {task id: leg} for tasks running a cut-short route's last leg
        self._gates: dict[str, LegGate] = {}  # one per provider, shared by every job this process runs (leggate.py)
        self._queued: dict[str, int] = {}  # job id -> its tasks waiting on a gate right now, for pool_report
        self._resuming: dict[str, str] = {}  # earlier job id -> the running job resuming it: two resumes would pay twice for its gaps
        self._budgets: dict[str, Budget] = {}  # job id -> its budget_usd ceiling, reserved against before every api turn
        self.port: int | None = None  # the shared server's port when this manager runs inside it (mcp_server.manager)

    def runner(self) -> dict:
        """Who runs the jobs this manager starts, stamped on each job: the process, and the port when it is the
        shared server. The stamp is what lets the next server on that port carry them on (adopt_orphans)."""
        return {"pid": os.getpid(), **({"port": self.port} if self.port else {})}

    def _trip(self, job: Job, leg: str, msg: str, strict: bool = True) -> None:
        """A cut-short route's last leg could not serve: remember it for NO_CREDIT_TRIP_S and stop every other task
        of this job running on it now, with the same message. Queued tasks meet the trip when they reach the leg.
        Only strictly pinned tasks (route=False) are stopped and only they mark the job: a routed task goes on by its
        profile (unpinned), so its siblings finish their leg and go on the same way."""
        if not _tripped(leg):
            _TRIPS[leg] = (time.monotonic() + NO_CREDIT_TRIP_S, msg)
        if strict:
            job.error = job.error or msg
        strict_ids = {t.id for t in job.tasks if not t.route}
        running = {tid for tid, on in self._exposed.get(job.id, {}).items() if on == leg and tid in strict_ids}
        me = asyncio.current_task()
        for t in job._tasks:
            tid = t.get_name().split(":", 1)[-1]
            if tid in running and t is not me and not t.done():
                job.results[tid].error = msg  # _run_one reads it back when the cancellation lands
                t.cancel()

    @property
    def client(self) -> DeepSeekClient:
        if self._client is None:
            self._client = DeepSeekClient()  # lazy: constructing it needs the key, and tests inject a fake instead
        return self._client

    def client_for(self, model: str) -> DeepSeekClient:
        """The client of the provider that serves `model`: DeepSeek's by default, another built on first use."""
        provider = config.provider_of(model)
        if provider == config.DEFAULT_PROVIDER:
            return self.client
        if provider not in self._clients:
            self._clients[provider] = DeepSeekClient(provider=provider)
        return self._clients[provider]

    @property
    def api_key(self) -> str:
        """The key for a `cc` worker: the next one from the shared pool, so headless runs rotate too."""
        return self.client.pool.pick()

    def route(self, model: str) -> str:
        """The path a task takes right now (owner ask, Michael, 2026-09-17): the cheapest PRIMARY path with
        credit, or - when no primary path has any - the cheapest fallback. Direct DeepSeek is primary at
        every hour; the router legs back it up (config.ROUTES says why). Compared on the measured token
        mix (config.REFERENCE_MIX), never on one rate."""
        return self.route_plan(model)[0]

    def route_plan(self, model: str, backend: str = "api") -> list[str]:
        """The legs to try, in order: the route first, then the failover legs (config.route_plan). `cc` has its
        own order where one is written down, because Claude Code cannot carry an OpenRouter host steer."""
        try:
            return config.route_plan(model, usable=keys.has_credit, backend=backend)
        except (ValueError, KeyError):
            return [model]  # an unknown model is the caller's error to raise, not the router's to swallow

    async def ask_routed(self, prompt: str, model: str, route: bool = True, **kw) -> Result:
        """One tool-free call on the routed path, failing over like a job task does: only when a path is
        unavailable, with the dead attempts' spend kept and named in `failover`.

        An ask that carries images walks only the legs that can see (config.sees). Found 2026-09-22: with Gemini's
        free pool spent, the tool-using chain's paid, blind legs were handed the screenshots and billed for a
        guess. No seeing leg left is an error naming that, never a text-only answer. The pictures are encoded
        once, up front, so a walk over several legs does not re-read and re-encode every file per leg."""
        from .agent import ask, image_part
        from .ledger import over_daily_cap

        if capped := over_daily_cap():
            raise ValueError(capped)
        profile = kw.pop("profile", None)
        if profile or model == config.AUTO:
            from .dispatch import ask_selected
            return await ask_selected(self, prompt, profile=profile or "general", route=route, **kw)

        # A leg whose breaker is open goes last (breaker.py): the ask goes straight to the fallback leg.
        legs = breaker.order(self.route_plan(model)) if route else [config.resolve_model(model)]
        images = kw.get("images")
        if images:
            blind = legs
            legs = [leg for leg in legs if config.sees(leg)]
            if not legs:
                return Result(id="ask", backend="api", model=config.resolve_model(model), status="error", error=self._no_vision_leg(model, blind, len(images)),
                              finished=now_iso())
            kw["images"] = [image_part(i)["image_url"]["url"] for i in images]
        dead: list[Result] = []
        for i, leg in enumerate(legs):
            try:
                res = await ask(self.client_for(leg), prompt, model=leg, **kw)
            except RuntimeError as e:  # no key at all for that provider
                res = Result(id="ask", backend="api", model=leg, status="error", error=f"NoUsableKey: {e}", finished=now_iso())
            if route:
                breaker.record(leg, res, leg_unavailable(res))
            if i == len(legs) - 1 or not leg_unavailable(res):
                break
            dead.append(res)
        add_spend(res, [{"cost_usd": d.cost_usd, "seconds": d.seconds} for d in dead])
        res.failover = [d.model for d in dead]
        _taint_failover(res, [{"taint": d.taint} for d in dead])
        if too_large(res):
            res.error = too_large_for_route([(d.model, d.error or "") for d in dead] + [(res.model, res.error)], tool_free=True)
        return res

    async def _judge(self, prompt: str, system: str) -> Result:
        """A verify task's judge: one tool-free call on the `judge` role's route, returning the typed verdict in
        `data`. Its spend is folded into the task's result, so it is ledgered once, with the task."""
        return await self.ask_routed(prompt, config.resolve_role("judge"), system=system, schema=verify.VERDICT_SCHEMA)

    @staticmethod
    def _no_vision_leg(model: str, legs: list[str], n_images: int) -> str:
        """Why an ask with images was not sent: its route has a leg with eyes but none has credit right now, or
        nothing on it can see at all (a text-only model was named)."""
        seeing = [leg for leg in config.routes_for(model) if config.sees(leg)]
        why = (f"its seeing legs {seeing} have no credit right now" if seeing
               else f"{config.resolve_model(model)} cannot see and nothing on its route can")
        return (f"NoVisionLeg: {n_images} image(s) not sent - {why}; the legs left {legs} are text-only and would guess. "
                f"Name a model with eyes (vision: true in the registry, e.g. {config.DEFAULT_MODEL_VISION}) or wait for its pool.")

    async def _run_legs(self, job: Job, task: Task, warm: asyncio.Event | None, is_pilot: bool) -> tuple[Result, object]:
        """Run a task on its route; when a pinned route runs out of legs that can serve, go on by its profile
        (unpinned) with the budget that is left, the dead attempts' spend and edits carried on the result."""
        pinned = task.model
        res, transcript = await self._run_route(job, task, warm, is_pilot)
        if not needs_other_route(res):
            return res, transcript
        child = unpinned(task, pinned, [*res.failover, res.model])
        child = remaining(child, res) if child is not None else None
        if child is None:
            if (res.error or "").startswith("NoCreditLeft:"):
                job.error = job.error or res.error  # nowhere left to go on: the trip is the job's story too
            return res, transcript
        from .dispatch import run_selected
        new, new_transcript = await run_selected(self, job, child, None, False)
        add_spend(new, [res])
        new.files_changed = sorted(set(new.files_changed + res.files_changed))
        new.failover = list(dict.fromkeys([*res.failover, res.model, *new.failover]))
        new.add_taint("F", res.taint)  # the pinned route could not serve: its letters stay on the answer that did
        new.selection = {**(new.selection or {}), "unpinned_from": pinned, "pinned_error": (res.error or "")[:300]}
        return new, {"pinned": transcript, "unpinned": new_transcript}

    async def _run_route(self, job: Job, task: Task, warm: asyncio.Event | None, is_pilot: bool) -> tuple[Result, object]:
        """Run a task on its route, failing over to the next leg only when a leg is UNAVAILABLE.

        A strictly pinned leg (one OpenRouter host, no fallbacks) is only as available as that host; this is
        what keeps a host outage, a key whose account cannot reach the host, or a pool that ran dry from
        failing every task routed there. The dead attempts' spend is real and rides along on the result
        (cost, usage, turns, seconds), and `failover` names the legs that could not serve.
        cc runs only the legs whose provider has an Anthropic Messages endpoint (Claude Code speaks nothing else),
        each with key rotation (added 2026-09-17, when the DeepSeek keys would not be topped up again); the
        api-only parts are the warm wait, the gate, the slow-leg cut, stall naming and the NoCreditLeft trip."""
        cc = task.backend == "cc"
        if cc:
            warm, is_pilot = None, False
        if task.profile:
            from .dispatch import run_selected
            return await run_selected(self, job, task, warm, is_pilot)
        legs = self.route_plan(task.model, **({"backend": "cc"} if cc else {})) if task.route else [task.model]
        if cc:
            legs = [leg for leg in legs if config.PROVIDERS[config.provider_of(leg)].get("anthropic_url")] or [task.model]
        # A leg whose breaker is open goes last (breaker.py), so a task does not time out on a host its siblings
        # already found down before it falls back. A pinned task (route=False) runs exactly its model, open or not.
        # cc talks to a provider's Messages endpoint, not its chat one, so its breakers are its own ("cc:<leg>").
        if task.route:
            front, back = breaker.split([f"cc:{leg}" if cc else leg for leg in legs])
            front, back = [b.removeprefix("cc:") for b in front], [b.removeprefix("cc:") for b in back]
        else:
            front, back = list(legs), []
        legs = front + back
        spent = _spent_legs(task.model, legs) if task.route and not cc else []
        # The route's real last leg is the last one no closed leg follows, not the last in the list: timed by list
        # position, a healthy fallback put ahead of an open primary would run under SlowLeg and fail over onto the
        # dead leg, and a cut-short route would trip the demoted leg. The open legs behind it are the last resort.
        terminal = len(front) - 1 if front else len(legs) - 1
        dead: list[dict] = []
        res: Result | None = None
        transcript: object = {} if cc else None
        if not is_pilot and warm is not None:
            # Before any gate: a follower holding a gate slot while it waits for the pilot's first reply could
            # starve the pilot of the very slot it needs to give that reply.
            await warm.wait()
        for i, leg in enumerate(legs):
            task.model = job.results[task.id].model = leg
            last = i >= terminal
            cut_short = i == terminal and bool(spent)  # the caller's own model is the next leg (see NO_CREDIT_TRIP_S)
            if cut_short and (msg := _tripped(leg)):
                res, transcript = Result(id=task.id, backend="api", model=leg, status="error", error=msg, started=now_iso(), finished=now_iso()), None
                break
            try:
                client = self.client_for(leg)
                await self._park_broke_keys(client)
                if cc:
                    res, transcript = await self._run_cc_with_rotation(task, client.pool)
                else:
                    # A crawling leg fails over too, but only while there is a next leg to go to (config.SLOW_LEG_TURN_S).
                    slow = config.SLOW_LEG_TURN_S if not last or cut_short else None
                    if cut_short:
                        self._exposed.setdefault(job.id, {})[task.id] = leg
                    gate = self._gate_for(leg, client)
                    ceiling = self._budgets.get(job.id)  # the job's budget_usd, reserved against before every api turn
                    res, transcript = await self._gated(job, gate, run_api_task(client, task, warm=warm, is_pilot=is_pilot, user_tag=job.id, slow_turn_s=slow,
                                                                                **({"job_budget": ceiling} if ceiling is not None else {})))
            except RuntimeError as e:  # no key at all for that provider: the leg cannot serve
                res = Result(id=task.id, backend=task.backend, model=leg, status="error", error=f"NoUsableKey: {e}", started=now_iso(), finished=now_iso())
                transcript = {} if cc else None
            finally:
                self._exposed.get(job.id, {}).pop(task.id, None)
            if task.route:
                breaker.record(f"cc:{leg}" if cc else leg, res, leg_unavailable(res))
            if cut_short and leg_unavailable(res) and not too_large(res):
                res.error = _tripped(leg) or no_credit_left(leg, spent, res.error or "")
                self._trip(job, leg, res.error, strict=not task.route)
            # A cut-short route ends at its terminal leg: past it the caller's own model beats a leg known down.
            if i == len(legs) - 1 or cut_short or not leg_unavailable(res):
                break
            dead.append({"model": leg, "error": (res.error or "")[:300], "cost_usd": res.cost_usd, "usage": dict(res.usage),
                         "turns": res.turns, "seconds": res.seconds, **({"transcript": transcript} if cc else {"api_seconds": res.api_seconds}),
                         "taint": res.taint})
            is_pilot = False  # the pilot already released the others when its first leg ended
        add_spend(res, dead)
        res.failover = [d["model"] for d in dead]
        _taint_failover(res, dead)
        if cc:
            if dead and isinstance(transcript, dict):
                transcript["failover_runs"] = dead
            return res, transcript
        # A leg that failed over because it STALLED (client._post's read-timeout retry, raised as an
        # ApiError 504) is named here too, so a task that recovers on its next leg still shows the stall
        # in the ledger - not only the ones that ran out of legs and ended the whole task in "error".
        res.stalled = [d["model"] for d in dead if "stalled:" in d["error"]]
        if res.status == "error" and "stalled:" in (res.error or ""):
            res.stalled.append(res.model)
        settle_too_large(res, [(d["model"], d["error"]) for d in dead] + [(res.model, res.error)], task.tools == "none")
        return res, transcript

    def _gate_for(self, model: str, client: object) -> LegGate | None:
        """The provider's LegGate, its cap re-read from the pool's usable keys, wired into the client so a 200 opens
        it and a 429 on a resting pool halves it. None for a client with no key pool (a test's fake)."""
        pool = getattr(client, "pool", None)
        if pool is None or not hasattr(pool, "disabled"):
            return None
        provider = getattr(client, "provider", None) or _provider(model) or "?"
        per_key = int(config.PROVIDERS.get(provider, {}).get("live_per_key") or config.LIVE_PER_KEY)
        cap = max(1, (len(pool) - len(pool.disabled())) * per_key)
        gate = self._gates.get(provider)
        if gate is None:
            gate = self._gates[provider] = LegGate(provider, cap=cap, start=config.LEG_RAMP_START)
        else:
            gate.set_cap(cap)
        try:
            client.gate = gate
        except AttributeError:
            pass
        return gate

    async def _gated(self, job: Job, gate: LegGate | None, run):
        """Await `run` inside `gate` (as-is when there is none), counting the job's tasks queued on it meanwhile."""
        if gate is None:
            return await run
        self._queued[job.id] = self._queued.get(job.id, 0) + 1
        try:
            await gate.acquire()
        except BaseException:
            run.close()  # never started: no "coroutine was never awaited" warning on a cancel
            raise
        finally:
            self._queued[job.id] -= 1
        try:
            return await run
        finally:
            await gate.release()

    def pool_report(self, job: Job) -> dict:
        """For zswarm_status: each provider this job's RUNNING tasks sit on - its gate (limit, live, waiting) and its
        pool (keys, ready now) - plus `waiting_on_rate_limited_pool` naming any whose every key is resting, so a
        saturated pool reads as that instead of a silent "running"."""
        providers = {_provider(r.model) for r in job.results.values() if r.status == "running"}
        pools: dict[str, dict] = {}
        waiting: dict[str, str] = {}
        for p in sorted(x for x in providers if x):
            gate = self._gates.get(p)
            client = self._client if p == config.DEFAULT_PROVIDER else self._clients.get(p)
            pool = getattr(client, "pool", None)
            if gate is None and pool is None:
                continue
            row = gate.snapshot() if gate else {"provider": p}
            if pool is not None and hasattr(pool, "available"):
                row["keys"], row["ready"] = len(pool), pool.available()
                if row["ready"] == 0:
                    waiting[p] = (f"every {p} key is rate-limited or disabled; running tasks are waiting on it and fail with "
                                  f"PoolSaturated after {config.SATURATED_REST_S:.0f}s when they have no other leg")
            if row.get("tripped"):
                waiting[p] = row["tripped"]  # keys can read ready while a region-wide quota 429s every one of them
            pools[p] = row
        out: dict = {"queued_on_pool": self._queued.get(job.id, 0), "pools": pools}
        if waiting:
            out["waiting_on_rate_limited_pool"] = waiting
        return out

    def status(self, job: Job) -> dict:
        """What a status read says: the job's summary and, while it runs, the pools its tasks sit on. The one
        composition zswarm_status returns and every checkpoint writes, so a reader in another process sees it too."""
        return {**job.summary(), **(self.pool_report(job) if job.state == "running" else {})}
    async def _escalate(self, job: Job, task: Task, res: Result, transcript: object, warm: asyncio.Event) -> tuple[Result, object]:
        """The worker GAVE UP (escalation.gave_up) on a task that named a stronger `escalate` model: run it once more
        there, with the failed trace behind the UNTRUSTED_TRACE guard. The teacher's answer is kept only when it
        passes the same check, and only then is a skill banked; otherwise the worker's result stands. Either way
        both runs' spend lands on the result that comes back, and `escalation` says what happened.

        Never on a leg that could not serve: that is failover's job, and the route already walked every leg."""
        messages = transcript if isinstance(transcript, list) else []
        why = None if leg_unavailable(res) else escalation.gave_up(res, messages)
        if not why:
            return res, transcript
        trace = escalation.trace(messages, res)
        teacher = escalation.teacher_task(task, why, trace)
        t_res, t_transcript = await self._run_legs(job, teacher, warm, False)
        t_why = "its leg could not serve" if leg_unavailable(t_res) else escalation.gave_up(t_res, t_transcript if isinstance(t_transcript, list) else [])
        record = {"from": res.model, "to": t_res.model, "why": why, "teacher_status": t_res.status, "teacher_cost_usd": t_res.cost_usd,
                  "kept": "worker" if t_why else "teacher", "skill": None}
        if t_why:
            record["teacher_gave_up"] = t_why
            kept, other, kept_transcript = res, t_res, transcript
        else:
            kept, other, kept_transcript = t_res, res, t_transcript  # the teacher's system prompt carries the worker's trace
            record["skill"] = await self._bank_skill(job, teacher, why, trace, t_res, student=res.model)
            if record["skill"] and record["skill"].get("cost_usd") is not None:
                kept.cost_usd = None if kept.cost_usd is None else kept.cost_usd + record["skill"]["cost_usd"]
        if other.cost_usd is not None and kept.cost_usd is not None:
            kept.cost_usd += other.cost_usd
        elif other.cost_usd is None:
            kept.cost_usd = None  # an unpriced run makes the whole task unmeasured, never cheaper
        for k, v in (other.usage or {}).items():
            kept.usage[k] = kept.usage.get(k, 0) + v
        kept.turns += other.turns
        kept.seconds = round(kept.seconds + other.seconds, 3)
        kept.api_seconds = round(kept.api_seconds + other.api_seconds, 3)
        kept.failover = res.failover + [f for f in t_res.failover if f not in res.failover]
        kept.escalation = record
        return kept, kept_transcript

    async def _bank_skill(self, job: Job, teacher: Task, why: str, trace: str, t_res: Result, student: str) -> dict | None:
        """One tool-free call on the teacher's leg that turns its fix into a skill file. Failing here never fails the task."""
        from .agent import ask

        system, prompt = escalation.skill_prompt(teacher, why, trace, t_res.answer or "")
        try:
            r = await ask(self.client_for(t_res.model), prompt, system=system, model=t_res.model, schema=escalation.SKILL_SCHEMA)
        except Exception as e:  # noqa: BLE001 - a missing key or a dead host costs the skill, not the answer
            return {"path": None, "cost_usd": None, "error": f"{type(e).__name__}: {e}"[:300]}
        path = None
        if r.status == "ok":
            try:
                path = escalation.bank_skill(r.data, task=teacher, why=why, student=student, teacher=t_res.model, job_id=job.id)
            except OSError as e:
                return {"path": None, "cost_usd": r.cost_usd, "error": f"{type(e).__name__}: {e}"[:300]}
        return {"path": str(path) if path else None, "cost_usd": r.cost_usd, **({"error": (r.error or "")[:300]} if r.status != "ok" else {})}

    def _sem(self, backend: str, size: int | None) -> asyncio.Semaphore:
        want = self._sem_sizes[backend]
        if size:
            want = max(1, min(int(size), config.MAX_CONCURRENCY[backend]))
        if backend not in self._sems or self._sem_sizes[backend] != want:
            self._sems[backend] = asyncio.Semaphore(want)
            self._sem_sizes[backend] = want
        return self._sems[backend]

    async def aclose(self) -> None:
        for c in ([self._client] if self._client is not None else []) + list(self._clients.values()):
            await c.aclose()

    # ---- submit ---------------------------------------------------------------

    def submit(self, tasks: list[Task], concurrency: dict | int | None = None, label: str = "", budget_usd: float | None = None, caller: dict | None = None,
               resume_from: str | None = None, envelope: dict | None = None) -> Job:
        if not tasks:
            raise ValueError("no tasks")
        from .ledger import over_daily_cap

        if capped := over_daily_cap():
            raise ValueError(capped)
        ids = [t.id for t in tasks]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate task ids: {sorted({i for i in ids if ids.count(i) > 1})}")
        # Two tasks naming one result file race on it and each can publish the other's answer.
        outs = [os.path.normcase(t.result_file) for t in tasks if t.result_file]
        if len(set(outs)) != len(outs):
            raise ValueError(f"duplicate result_file across tasks: {sorted({o for o in outs if outs.count(o) > 1})}")
        keys_of = {t.id: task_key(t) for t in tasks}
        reuse: dict[str, dict] = {}
        if resume_from:
            earlier = self.jobs.get(resume_from)
            if earlier is not None and earlier.state == "running":
                raise ValueError(f"resume_from_job {resume_from}: that job is still running; wait for it (or cancel it) first")
            if (busy := self._resuming.get(resume_from)) and self.jobs[busy].state == "running":
                raise ValueError(f"resume_from_job {resume_from}: job {busy} is already resuming it; a second resume would pay twice for the same tasks")
            recorded = resumable(resume_from)
            reuse = {tid: recorded[k] for tid, k in keys_of.items() if k in recorded}
        # A pinned route nothing can serve now is routed by its profile (see unpinned), not refused with the job.
        tasks = list(tasks)
        for n, t in enumerate(tasks):
            if t.id not in reuse and t.backend == "api" and t.route and not t.profile and not route_health(t.model)["serves"]:
                tasks[n] = unpinned(t, t.model, list(config.routes_for(t.model))) or t
        # Only the tasks that will run can make a job unservable: a fully cached resume needs no leg at all.
        if refusal := unservable([t for t in tasks if t.id not in reuse]):
            raise ValueError(refusal)
        # The spawn tree, checked LAST so a job refused for any other reason never takes a node: a job started under
        # an inherited envelope (a cc worker's own zswarm) or given one here is admitted narrowed, or refused by name.
        admit_envelope(tasks, envelope)
        if isinstance(concurrency, int):
            concurrency = {b: min(concurrency, config.MAX_CONCURRENCY[b]) for b in config.MAX_CONCURRENCY}
        # The caller stamp is read at submit time from this process's environment: the calling Claude session's ids
        # for an MCP-driven job, the argv for a CLI one. It is what lets `usage` say WHO used the swarm.
        job = Job(id=new_job_id(), tasks=tasks, concurrency=dict(concurrency or {}), label=label, budget_usd=budget_usd, caller=caller or detect_caller(label),
                  resumed_from=resume_from or "", task_keys=keys_of, task_models={t.id: t.model for t in tasks}, runner=self.runner())
        config.ensure_dirs()
        job.save()
        self.jobs[job.id] = job
        if resume_from:
            self._resuming[resume_from] = job.id
        if budget_usd is not None:
            self._budgets[job.id] = Budget(budget_usd, "job budget (budget_usd)")
        for t in tasks:
            if t.id in reuse:
                job.results[t.id] = _cached_result(t, reuse[t.id], resume_from)
                self._journal(job, t, job.results[t.id], None)
        self._start(job)
        return job

    def _start(self, job: Job) -> None:
        """Run every task of the job that has no result yet, the first api one as the pilot that warms the prompt
        cache for the rest, and the watcher that checkpoints and finishes the job."""
        warm = asyncio.Event()
        pilot_assigned = False
        for t in job.tasks:
            if t.id in job.results:
                continue
            job.results[t.id] = Result(id=t.id, status="pending", backend=t.backend, model=t.model)
            is_pilot = t.backend == "api" and not pilot_assigned
            pilot_assigned = pilot_assigned or is_pilot
            job._tasks.append(asyncio.create_task(self._run_one(job, t, warm, is_pilot), name=f"{job.id}:{t.id}"))
        if not pilot_assigned:
            warm.set()  # no api task, nothing to warm: cc workers start at once
        asyncio.create_task(self._finish(job), name=f"{job.id}:finish")

    def adopt(self, job_id: str, doc: dict) -> Job:
        """Carry on a job whose server stopped, under its own id: the answers it already journaled stay, every
        other task starts again, and a caller polling the id sees it go on. Its spend so far counts against its
        budget. The tasks were admitted and routed when first submitted, so they are not screened again.

        The saved tasks are rebuilt as they are, never normalised a second time: that would drop an AUTO task's
        profile and prepend a review rubric or scope block twice. Each goes back to the model it was submitted with,
        since a checkpoint saves the failover leg it was on. A task journaled `cancelled` was stopped by the shutdown
        (a job-level cancel marks the record cancelled, which is never adopted), so it runs again. A cancel asked
        through the job folder while no server ran is honoured here instead of running the job again."""
        folder = config.JOBS_DIR / job_id
        tasks, summary, saved_keys = _saved_tasks(doc, folder), doc.get("summary") or {}, doc.get("task_keys") or {}
        job = Job(id=job_id, tasks=tasks, concurrency=dict(doc.get("concurrency") or {}), label=summary.get("label") or "",
                  budget_usd=summary.get("budget_usd"), caller=dict(doc.get("caller") or {}), created=summary.get("created") or now_iso(),
                  resumed_from=summary.get("resumed_from") or "", task_keys={t.id: saved_keys.get(t.id) or task_key(t) for t in tasks},
                  task_models={t.id: t.model for t in tasks}, runner=self.runner())
        done = _journaled(folder)
        job.results.update({t.id: done[t.id] for t in tasks if t.id in done})
        self.jobs[job_id] = job
        if (asked := folder / CANCEL_REQUEST).exists():
            return _honour_cancel(job, asked)
        if job.budget_usd is not None:
            budget = self._budgets[job_id] = Budget(job.budget_usd, "job budget (budget_usd)")
            budget.settled = job.cost()
        if job.resumed_from:
            self._resuming[job.resumed_from] = job_id
        job.save()
        self._start(job)
        return job

    def adopt_orphans(self) -> list[str]:
        """Carry on (adopt) the jobs a previous server on this port left unfinished. Only one process can listen on
        a port, so a running record stamped with this port by another process has lost its runner. One that went quiet
        more than config.ADOPT_WITHIN_S ago is an old crash, left as it is; so is anything a CLI run or a per-chat
        server started (no port on its stamp)."""
        if not self.port or not config.JOBS_DIR.exists():
            return []
        oldest = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).strftime("%Y%m%d-%H%M%S")  # ids sort by start
        adopted = []
        for folder in sorted(config.JOBS_DIR.iterdir(), reverse=True):
            if folder.name < oldest:
                break
            record = folder / "job.json"
            if folder.name in self.jobs or not record.is_file():
                continue
            try:
                with record.open(encoding="utf-8") as f:
                    if '"running"' not in f.read(64):  # the record opens with its state: skip the finished ones unparsed
                        continue
                doc = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            runner, summary = doc.get("runner") or {}, doc.get("summary") or {}
            if doc.get("state") != "running" or summary.get("finished") or runner.get("port") != self.port or runner.get("pid") == os.getpid():
                continue
            try:
                quiet = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(str(summary["checkpoint_at"]))).total_seconds()
            except (KeyError, ValueError):
                continue
            if quiet > config.ADOPT_WITHIN_S:
                continue
            try:
                self.adopt(folder.name, doc)
                adopted.append(folder.name)
            except (OSError, ValueError, TypeError, KeyError) as e:  # a record or task this version cannot read back
                print(f"[zswarm] job {folder.name} could not be carried on after the restart: {type(e).__name__}: {e}", file=sys.stderr)
        return adopted

    async def run_batch(self, tasks: list[Task], concurrency: dict | int | None = None, label: str = "", budget_usd: float | None = None, caller: dict | None = None,
                        resume_from: str | None = None, envelope: dict | None = None) -> Job:
        """submit + wait: the shape every batch script wants."""
        job = self.submit(tasks, concurrency=concurrency, label=label, budget_usd=budget_usd, caller=caller, resume_from=resume_from, envelope=envelope)
        return await self.wait(job.id, None)

    async def _park_broke_keys(self, client: DeepSeekClient) -> None:
        """Once per freshness window, per process: probe every key's balance before any is handed out, so a key
        that is out of credit is disabled first. The lock keeps two hundred concurrent tasks from probing at once;
        the second one in finds the readings fresh and probes nothing. A balance endpoint that is down is noted
        and the job goes on: the pool still disables a key the moment it answers 402."""
        # Non-authoritative balance endpoints cannot disable a key. Probing every OpenRouter
        # account before the first task needlessly stalls large pools outside the task timeout.
        if getattr(client, "spec", {}).get("balance_authority") == "status":
            return
        async with self._probe_lock:
            try:
                parked = await client.probe_balances()
            except Exception as e:  # noqa: BLE001
                self.probe_error = f"{type(e).__name__}: {e}"
                return
            if parked:
                self.parked_keys = parked

    async def _run_cc_with_rotation(self, task: Task, pool=None) -> tuple[Result, dict]:
        """A cc worker takes one key for its whole run, so a 402 mid-run is the run's death. The key is one WITH
        balance (a disabled key is never launched on; one resting for a 429 is waited for, up to CC_KEY_WAIT_S); a
        dead run disables its key and the task goes again on the next one, once per key, and the dead runs' spend
        and transcripts ride along on the result that comes back. A pool with no key left returns the last run's
        result, saying so; a pool with none to begin with runs nothing and says that. Either way the error carries
        `NoUsableKey`, which is what lets the route fail over to the next provider."""
        pool = pool if pool is not None else self.client.pool
        tried: set[str] = set()
        dead: list[dict] = []
        res: Result | None = None
        transcript: dict = {}
        waited = 0.0
        while True:
            key, wait_s = pool.next_with_balance(exclude=tried)
            if key is None or waited + wait_s > CC_KEY_WAIT_S:
                why = ("every key in the pool is disabled" if key is None
                       else f"every key with balance is resting, the soonest wakes in {wait_s:.0f} s")
                if res is None:
                    res = Result(id=task.id, backend="cc", model=task.model, status="error", started=now_iso(), finished=now_iso(),
                                 error=f"NoUsableKey: no key to run on: {why}; top up and run `zswarm keys probe`, or `zswarm keys enable <fingerprint>`")
                    earlier = dead
                else:
                    res.error = f"{res.error or ''} (key {dead[-1]['key']} disabled as out of credit; NoUsableKey: no key with credit left to retry on: {why})".strip()
                    earlier = dead[:-1]  # this result IS the last dead run; its own spend is already in it
                self._fold_dead_runs(res, transcript, earlier)
                return res, transcript
            if wait_s > 0:
                await asyncio.sleep(wait_s)
                waited += wait_s
                continue  # the pool moved while this task slept (another task's 402 may have disabled that key): pick again
            tried.add(key)
            if dead:
                self.cc_rotations += 1
            res, transcript = await run_cc_task(task, key)
            if not out_of_balance(res):
                self._fold_dead_runs(res, transcript, dead)
                return res, transcript
            pool.broke(key, status=402)
            dead.append({"key": config.fingerprint(key), "error": res.error, "cost_usd": res.cost_usd, "usage": dict(res.usage),
                         "turns": res.turns, "seconds": res.seconds, "transcript": transcript, "taint": res.taint})

    @staticmethod
    def _fold_dead_runs(res: Result, transcript: dict, dead: list[dict]) -> None:
        """The runs that died of 402 before this result cost real money (a key runs out MID-run): their spend, usage,
        turns and seconds go into the result the ledger sees, and their transcripts ride along under `dead_runs`."""
        if not dead:
            return
        add_spend(res, dead)
        # The answer came from a run that started over on another key, after a run that may have half-done the work.
        res.add_taint("R", *(d.get("taint") or "" for d in dead))
        if isinstance(transcript, dict):
            transcript["key_rotations"] = len(dead)
            transcript["dead_runs"] = dead

    async def _run_one(self, job: Job, task: Task, warm: asyncio.Event, is_pilot: bool) -> None:
        sem = self._sem(task.backend, job.concurrency.get(task.backend))
        transcript: object = None
        res = job.results[task.id]
        base = ""  # a scripted task's starting tree, which its script is replayed from
        async with _Slot(sem) as slot:
            res.status, res.started = "running", now_iso()
            try:
                if task.scripted:
                    # Known limit: a key-rotation retry re-runs the worker on a cwd a dead leg may already have edited,
                    # while the replay starts from this snapshot, so such a retry can end in a false ScriptMismatch.
                    base = await scripted.snapshot(task.cwd)
                # A task that opted into escalation also reads the skills earlier escalations banked for its kind.
                applied = escalation.apply_skills(task) if task.backend == "api" and task.escalate else []

                async def attempt(t: Task, first: bool) -> tuple[Result, object]:
                    # Routed at DISPATCH, not at submit: peak/off-peak can flip and keys can run out while a
                    # long job is still going, and the Result carries the leg that actually served the call,
                    # so the ledger never claims a route that did not happen.
                    r, tr = await self._run_legs(job, t, warm, is_pilot and first)
                    # Every leg could not serve (keys out, pools saturated from outside, hosts down): rest and run
                    # it again on the whole ladder with the budget left, rather than hand the caller a dead task
                    # (owner, 2026-09-25: "rerun them if they're dead"). A strict pin (route=False) stays one try.
                    # Bounded by time, not a count: four reruns in half an hour did not outlast one evening's Gemini
                    # saturation (job 20260926-003426-92c1), and each came back to the caller as a dead task.
                    reruns, dead_since = 0, time.monotonic()
                    while t.route and needs_other_route(r) and time.monotonic() - dead_since < config.DEAD_RERUN_PATIENCE_S:
                        again = remaining(t, r)
                        if again is None:
                            break
                        reruns += 1
                        if is_pilot:
                            warm.set()  # the siblings do not wait out the pilot's rest; a rerun needs no warm-up
                        rest = min(config.DEAD_RERUN_REST_S * 2 ** (reruns - 1), config.DEAD_RERUN_REST_MAX_S)
                        res.next_action = f"no route could serve it; rerun {reruns} in {rest:.0f} s ({(r.error or '')[:160]})"
                        await slot.rest(rest)  # the job's other tasks run in this slot meanwhile
                        res.next_action = ""
                        r2, tr = await self._run_legs(job, again, None, False)
                        add_spend(r2, [r])
                        r2.files_changed = sorted(set(r2.files_changed + r.files_changed))
                        r2.failover = list(dict.fromkeys([*r.failover, r.model, *r2.failover]))
                        r2.selection = {**(r2.selection or {}), "dead_reruns": reruns}
                        r = r2
                    if t.backend == "api" and t.escalate:
                        r, tr = await self._escalate(job, t, r, tr, warm)
                    return r, tr

                if task.verify:
                    res, transcript = await verify.run_verified(task, attempt, judge=self._judge)
                else:
                    res, transcript = await attempt(task, True)
                res.skills = applied
            except asyncio.CancelledError:
                # Stopped by a sibling's NoCreditLeft trip (JobManager._trip): an error with that message, not a cancel.
                tripped = (res.error or "").startswith("NoCreditLeft:")
                res.status, res.error, res.finished = "error" if tripped else "cancelled", res.error or "cancelled", now_iso()
                job.results[task.id] = res
                self._journal(job, task, res, transcript)
                raise
            except Exception as e:  # noqa: BLE001 - a backend that raises before producing a Result (no key, bad binary) must not leave the task "running" forever
                res.status, res.error, res.finished = "error", f"{type(e).__name__}: {e}", now_iso()
            finally:
                # The agent loop sets `warm` only on the pilot's first SUCCESSFUL reply. A pilot that failed first
                # (an error, no key, every leg down) left every other api task waiting until its own timeout with
                # 0 calls made (the 900 s zero-call timeouts of 2026-09-24). A finished pilot releases them either way.
                if is_pilot:
                    warm.set()
        if task.scripted and base and res.status == "ok":
            # Scripted-diff: an answer is accepted only when its script, replayed on the starting tree, makes the same tree.
            # Held at "running" until the replay returns, so a wait=false poller never reads an unverified ok.
            res.status = "running"
            try:
                await scripted.settle(res, task.cwd, base)
            except Exception as e:  # noqa: BLE001 - an unverified result must not be left "running" or passed as ok
                res.status, res.error = "error", f"ScriptedDiff: {type(e).__name__}: {e}"
            if res.status == "running":
                res.status = "ok"
        if task.inventory:
            # The coverage gate is zswarm's, not the worker's: whatever the backend, an ok result whose receipt does
            # not equal the declared inventory is an IncompleteReview error, never a silent partial "no issues".
            review.enforce_receipt(res, task.inventory)
        res.mis_scoped = mis_scoped(task, res)
        # Labelled once, here, so both backends and every leg get the same reading of "did it move the work".
        res.liveness, res.next_action = classify_liveness(res)
        job.results[task.id] = res
        self._journal(job, task, res, transcript)
        if task.backend != "api" and job.id in self._budgets:
            # A cc worker has no turn loop to reserve in; its spend still counts against what api siblings may reserve.
            self._budgets[job.id].settle(0.0, res.cost_usd or 0.0)
        if job.budget_usd is not None and job.state == "running" and job.cost() > job.budget_usd:
            self.cancel(job.id, reason=f"job budget exceeded: ${job.cost():.4f} > ${job.budget_usd:.4f}")
        elif job.id in self._budgets and job.state == "running" and self._budgets[job.id].name + " reached" in (res.error or ""):
            # WHY: the reservation keeps job.cost() at or under budget_usd, so the check above never fires. A turn
            # refused against SETTLED spend (budget.plan_turn) means the budget is spent: cancel the rest now, rather
            # than start every pending task only to have each refused at turn 0 and recorded as an error.
            self.cancel(job.id, reason=f"job budget spent: ${self._budgets[job.id].settled:.4f} of ${job.budget_usd:.4f}, no room for another turn")

    def _journal(self, job: Job, task: Task, res: Result, transcript: object) -> None:
        """Append the result, its transcript and one ledger line. Disk errors never fail a task."""
        record_tree_spend(task, res.cost_usd)  # a no-op outside a spawn tree
        try:
            job.dir.mkdir(parents=True, exist_ok=True)
            with (job.dir / "results.jsonl").open("a", encoding="utf-8") as f:
                # `key` is the task's content hash: what a later resume_from_job matches this answer by.
                f.write(json.dumps({**res.as_dict(), "key": job.task_keys.get(task.id, "")}, ensure_ascii=False) + "\n")
            if transcript is not None:
                tdir = job.dir / "transcripts"
                tdir.mkdir(exist_ok=True)
                # The worker's own record, with anything long (the shared system prompt above all) kept once
                # per job as a blob: these files were 163 KiB of identical prompt each before this. A list of
                # messages from `api`, a single dict from `cc`, and anything else passes through untouched.
                packed = blobs.pack_any(transcript, blobs.blob_dir(job.dir))
                (tdir / f"{task.id}.json").write_text(json.dumps(packed, indent=1, ensure_ascii=False), encoding="utf-8")
            row = {
                "ts": res.finished or now_iso(), "job": job.id, "task": task.id, "backend": res.backend, "model": res.model, "provider": _provider(res.model),
                "status": res.status, "calls": res.turns, **res.usage, "cost_usd": res.cost_usd, "seconds": res.seconds, "peak": config.is_peak(),
                "upstream": ",".join(res.upstream) or None, "api_seconds": res.api_seconds,
                "failover": ",".join(res.failover) or None,
                "selection_profile": res.selection.get("profile"),
                "benchmark_slug": (res.selection.get("selected") or {}).get("benchmark_slug"),
                "reasoning_effort": (res.selection.get("selected") or {}).get("reasoning_effort", task.reasoning_effort),
                # The UNBIASED benchmark cost the pick is judged on, beside the load bias that only ordered it.
                "benchmark_cost_usd": (res.selection.get("selected") or {}).get("benchmark_cost_usd"),
                "load_bias": (res.selection.get("selected") or {}).get("load_bias"),
                "stalled": ",".join(getattr(res, "stalled", None) or []) or None,
                "loop": res.loop, "loop_warnings": res.loop_warnings or None,
                # How many files the task wrote: the rows ledger_rows later joins a survival score onto.
                "files_changed": len(res.files_changed) or None,
                "cached": res.cached_from or None,  # reused from that job's journal: no call made, no spend
                "escalated": (f"{res.escalation['to']}:{res.escalation['kept']}" if res.escalation else None),
                "liveness": res.liveness or None,
                "taint": res.taint or None,
                **ledger_fields(job.caller),
            }
            if getattr(res, "edit_snapshot", None):
                survival.save(job.id, task.id, res.model, res.finished or now_iso(), res.edit_snapshot)
            with config.LEDGER.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    async def _finish(self, job: Job) -> None:
        # job.json was written at submit and at the end only, so a status read from any OTHER process (the CLI's
        # `status`, another session's MCP) showed every unfinished task `pending` and nothing about why: jobs
        # 20260925-184445-98ac and -184449-a8b9 read `{'pending': N}`, $0, for 13 minutes while the MCP server's
        # own status named the pool. A running job now rewrites its record with the live status every CHECKPOINT_S,
        # and the stamp tells a reader when the process running it has gone quiet (Job.load_from_disk).
        done = asyncio.gather(*job._tasks, return_exceptions=True)
        while not done.done():
            await asyncio.wait({done}, timeout=config.CHECKPOINT_S)
            if not done.done() and job.state == "running":
                try:
                    if (asked := job.dir / CANCEL_REQUEST).exists():  # zswarm_cancel / `zswarm cancel` from another process
                        self.cancel(job.id, reason=asked.read_text(encoding="utf-8").strip() or "cancelled")
                        continue
                    job.save(self.status(job))
                except OSError:
                    pass  # a checkpoint never fails the job it describes
        self._queued.pop(job.id, None)
        if any("NoUsableKey" in (r.error or "") for r in job.results.values()):
            await asyncio.to_thread(verdict.write)  # the pools just said something the routing gate should hear
        self._budgets.pop(job.id, None)
        if job.state == "running":
            job.state = "done"
        job.finished = now_iso()
        if self._resuming.get(job.resumed_from) == job.id:
            del self._resuming[job.resumed_from]
        # One utilization row per job, priced at the caller's model at the time: the running total the owner reads.
        # A resume whose every answer was reused ran nothing, so it displaced nothing and gets no row.
        if any(not r.cached_from for r in job.results.values()):
            job.savings = utilization.compact(await asyncio.to_thread(utilization.record_job, job))
        # Every finished job also scores the earlier edits that reached a survival checkpoint, so the 5 minute
        # and 1 hour readings land while the swarm is in use, not only at the nightly `maintain`.
        await asyncio.to_thread(survival.score_due)
        job.save()
        job._done.set()

    # ---- query ----------------------------------------------------------------

    def get(self, job_id: str) -> Job:
        if job_id in self.jobs:
            return self.jobs[job_id]
        raise KeyError(job_id)

    async def wait(self, job_id: str, timeout_s: float | None) -> Job:
        job = self.get(job_id)
        try:
            await asyncio.wait_for(job._done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass  # the caller polls; a job past its wait window is still running, not failed
        return job

    def cancel(self, job_id: str, reason: str = "cancelled") -> Job:
        job = self.get(job_id)
        job.state = "cancelled"
        me = asyncio.current_task()
        for r in job.results.values():
            if r.status in ("pending", "running"):
                r.status, r.error = "cancelled", reason
        for t in job._tasks:
            if t is not me:  # the budget guard cancels from inside a task; cancelling itself would raise mid-journal
                t.cancel()
        job.save()
        return job

    @staticmethod
    def cancel_on_disk(job_id: str, reason: str = "cancelled") -> dict:
        """Cancel a job this process does not run. Jobs 20260923-080914-3444 and others (their `zswarm.py run` killed)
        read `running` forever, and zswarm_cancel answered KeyError, because only the submitting process knew them.
        A live owner (a running record with a checkpoint) is ASKED through the job folder and stops within
        config.CHECKPOINT_S; an orphaned record, or one from before checkpoints, has nothing left to ask, so its
        unfinished tasks are marked cancelled on disk. KeyError when no such job exists."""
        doc = Job.load_from_disk(job_id)  # the live overlay: finished rows from results.jsonl are kept
        summary = doc["summary"]
        if summary.get("finished"):
            return summary
        folder = config.JOBS_DIR / job_id
        if summary.get("state") == "running" and summary.get("checkpoint_at"):
            (folder / CANCEL_REQUEST).write_text(reason, encoding="utf-8")
            return {**summary, "cancel": f"asked the process running it; it stops within {config.CHECKPOINT_S:.0f}s"}
        results = dict(doc.get("results") or {})
        for task in doc.get("tasks") or []:
            tid = task.get("id") if isinstance(task, dict) else None
            row = results.get(tid) or {"id": tid, "status": "pending"}
            if tid and row.get("status") in ("pending", "running"):
                results[tid] = {**row, "status": "cancelled", "error": reason}
        counts: dict[str, int] = {}
        for row in results.values():
            counts[row.get("status", "pending")] = counts.get(row.get("status", "pending"), 0) + 1
        finished = now_iso()
        summary = {**summary, "state": "cancelled", "finished": finished, "counts": counts, "cancel": reason}
        summary.pop("orphaned", None)
        tmp = folder / "job.json.tmp"
        tmp.write_text(json.dumps({**doc, "state": "cancelled", "finished": finished, "summary": summary, "results": results},
                                  indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, folder / "job.json")
        return summary

    load_from_disk = staticmethod(Job.load_from_disk)
    list_on_disk = staticmethod(Job.list_on_disk)
