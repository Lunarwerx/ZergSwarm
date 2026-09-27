"""Async client for any OpenAI-compatible chat-completions provider (DeepSeek and OpenRouter today;
Moonshot, DashScope, Gemini registered in config.PROVIDERS), one instance per provider.

One httpx.AsyncClient, one connection pool, no threads. Retries 429/5xx/transport errors with capped
exponential backoff. Every call returns a ChatResult (usage.py) with the usage split the provider
reports and the USD cost at the live rate - or, where the provider reports what it actually charged
(OpenRouter does, per call), that number instead of ours.

Keys are a POOL (owner ask, Michael, 2026-09-15): every request takes the next key in turn, and a key
that answers 429 is rested for the Retry-After (or 20 s) while the next one carries on at once.

A key that RUNS OUT goes to a DISABLED SLOT and is never handed to a worker again (owner ask,
Michael, 2026-09-17: "when any key runs out of DeepSeek or OpenRouter, have it go to, like, a
disabled slot so it's not constantly retried"). Disabling is STICKY - no timer, no ladder, no
periodic re-try with a real request - and it happens on:
  * a 402 from the chat endpoint (out of credit), whatever the provider; and
  * for a provider whose balance number is its own word on the matter (`balance_authority: "reading"`,
    DeepSeek), a balance probe at or under zero, so the key is out before a `cc` worker takes it for
    a whole run (five tasks died of 402 that way on 2026-09-16).
  * a revoked key (401/403) once it has struck out DEAD_STRIKES times; before that it rests on the
    doubling ladder, because a 401 can also be one bad minute at the provider.
Only a FREE probe of the balance endpoint, or an explicit `zswarm keys enable`, brings a disabled key
back - so a topped-up key still heals itself without any worker paying to discover it. A key disabled
for a 402 that still holds free-tier allowance keeps serving `:free` models (ten of the eighteen
OpenRouter keys were in exactly that state on 2026-09-17).

OpenRouter's own credit number is NOT acted on: measured 2026-09-17, ten keys read a negative balance
from /credits and all ten still served a paid model at HTTP 200. A negative figure is a small overdraft,
not a dead key: under sustained load later that morning the nine still reading negative did go on to
402, and the slot caught each on its first 402. It is reported by `doctor` and `zswarm keys`, and the
402 is what decides.

The shared state is written under a file lock, one read-modify-write at a time, so a disable landed by
one process is never overwritten by a rest in another. Only when every key is resting does the client
wait; when every key is DISABLED it fails loudly instead of hammering dead keys. No key is ever logged
or printed; `fingerprint()` is the only thing that leaves this module.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as dt
import json
import math
import os
import random
import re
import time
from collections.abc import Collection
from pathlib import Path

import httpx

try:
    import msvcrt  # Windows: byte-range locks on the shared state file
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
    import fcntl

from . import config, egress, faults
from .usage import ApiError, ChatResult, Usage, request_body  # noqa: F401 - re-exported

# Seconds the running task's calls spent rate-limited, from a call's first 429 to its reply or its give-up. That is
# waiting on a pool, not work: agent.run_api_task sets a fresh box per run and reports it as Result.rested_s, and a
# dead task's rerun keeps it on its run clock (jobs.remaining). Jobs 20260926-003426-92c1: 13 builders spent ~2,000 s
# of a 2,400 s budget on Gemini 429s and ended with the budget gone.
RATE_WAIT: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("zswarm_rate_wait", default=None)


def _note_rate_wait(limited_since: float | None) -> None:
    box = RATE_WAIT.get()
    if box is not None and limited_since is not None:
        box[0] += time.monotonic() - limited_since

RETRY_STATUS = {429, 500, 502, 503, 504}
DEAD_KEY_STATUS = {401, 402, 403}
# Gemini answers an unusable key with HTTP 400 "Please pass a valid API key", NOT 401. Measured
# 2026-09-20 on the author's PC: a 40-task read-only swarm lost 26 tasks to it in one job (20260920-071139-58a0)
# while `zswarm keys` still read "72 ready, 0 disabled" - because a 400 is neither a dead-key status (so
# the key was never rested and kept being handed out) nor failover-eligible (so the leg dead-ended
# instead of down-routing to the paid fallback). Treat a key-SHAPED 400 as a dead key: rest it, rotate,
# and when every key answers that way raise NoUsableKey, which jobs._UNAVAILABLE does match.
_KEY_SHAPED_400 = re.compile(r"pass a valid API key|API key not valid|invalid[_ ]api[_ ]key|API key is invalid|missing API key", re.I)
# A 400 that means "the MODEL emitted something unparseable", not "your request was wrong". gpt-oss writes
# OpenAI harmony channels (analysis/commentary) and sometimes malformed tool-call JSON, and the provider
# validates tool calls server-side and rejects the whole completion - measured 2026-09-20 on the judgment
# suite, where groq returned `tool_use_failed` ("attempted to call tool 'commentary' which was not in
# request.tools") and `output_parse_failed`, costing gpt-oss-120b more than half the tool-using tasks
# despite it being the best tool-FREE reasoner tested (51/51). That is a bad SAMPLE, so resampling is the
# right response: retry the same leg a bounded number of times instead of failing the task. A genuine bad
# request (max_tokens too large, malformed schema) does not match and still fails immediately.
_MODEL_OUTPUT_400 = re.compile(r"tool_use_failed|output_parse_failed|Tool call validation failed|Failed to parse tool call|generated output that could not be parsed", re.I)
# A 400 that means "this endpoint always reasons, you may not switch it off". OpenRouter answers
# glm-5-3-flash with "Reasoning is mandatory for this endpoint and cannot be disabled" when a schema
# ask sends `reasoning: {enabled: false}`: every schema'd lens of the Connections deep-codebase-audit
# failed on it (2026-09-25). The model is remembered for the life of the process (a registry entry
# may say so up front with `reasoning_mandatory: true`) and the call is re-sent once without the switch.
_REASONING_MANDATORY_400 = re.compile(r"reasoning is mandatory|cannot be disabled", re.I)
_REASONING_MANDATORY: set[str] = set()
MODEL_OUTPUT_RETRIES = 3
# A 429 that is really "out of credit", not "too fast". Zhipu answers an unfunded key with HTTP 429
# "Insufficient balance or no resource pack", which is permanent until someone pays - but a 429 only
# RESTS a key, so the pool re-tried the same dead keys forever. Measured 2026-09-20: 47 of 50 sampled
# zhipu keys are unfunded, so without this the glm leg spends its whole budget rediscovering them. The
# repo's rule is that a key which has run out goes to the disabled slot and is never retried, so route
# these to broke() like a 402. A real rate-limit 429 does not match and still just rests.
_OUT_OF_CREDIT_429 = re.compile(r"[Ii]nsufficient balance|no resource pack|balance is insufficient|arrears|欠费", re.I)
# The same for a 400: groq answers a key whose organisation hit its spend alert with 400 `spend_limit_reached`
# ("Organization has blocked API access because a spend alert threshold was met"). It is per KEY (each key is its own
# organisation), so it disables that key and the next one serves. 2026-09-27: one Odin refresh run met it 20 times on
# groq while 7 calls on its other keys answered, and each meeting failed the whole leg.
_OUT_OF_SPEND_400 = re.compile(r"spend_limit_reached", re.I)
RATE_REST_S = 20.0
DEAD_REST_S = 600.0
# A 429 that means "this key's quota for TODAY is spent", not "too fast". Gemini's free tier caps each key at 20
# generate_content requests a day and answers the 21st with 429 RESOURCE_EXHAUSTED whose QuotaFailure names a
# `...PerDay...` quotaId - and a RetryInfo of seconds, which is a lie for a daily cap. Resting it RATE_REST_S made
# the pool hand the same spent keys to every task again and again. Measured 2026-09-24 (jobs 20260924-154437-ebda,
# 20260924-205816-dd94): 19 and 20 tasks sat on the spent pool to 440-600 s with `failover: []`. A day-spent key now
# rests until the quota resets (midnight Pacific; QUOTA_RESET_UTC_HOUR is the PDT hour, so in winter a key is
# tried an hour early, answers one 429 and rests again), which drops the leg from route_plan once every key is spent.
_DAILY_QUOTA_429 = re.compile(r"PerDay")
QUOTA_RESET_UTC_HOUR = 7
# A 429 whose quota LIMIT is zero is not "too fast" either: the key's project has no quota at all where the call
# landed. Measured 2026-09-26 on the author's PC: every Gemini model on every sampled key (twelve keys, twelve projects)
# answered 429 with quota_limit_value "0" for GenerateContentRequestsPerMinutePerProjectPerRegion in us-south1, and
# rested RATE_REST_S the 760 keys were rotated for 34 minutes by each of 124 builders, all of which died. Rested
# ZERO_QUOTA_REST_S, the shared key state takes the leg out of every plan on the machine until the keys wake.
_ZERO_QUOTA_429 = re.compile(r'"quota_limit_value"\s*:\s*"0"')
ZERO_QUOTA_REST_S = 1800.0


def _zero_limit_header(headers) -> bool:
    """The same zero quota said in a header: Mistral answers a key with no allowance at all with 429 and
    x-ratelimit-limit-req-minute: 0 (three of three sampled keys, 2026-09-26), where a busy key names a real limit."""
    return any(k.lower().startswith("x-ratelimit-limit-req") and str(v).strip() == "0" for k, v in headers.items())
# A 413 that names the ACCOUNT's limit is that key's organisation being too small for the request, not the request
# being too big for the model: groq answers "Request too large for model `qwen/qwen3.8-27b` in organization `org_...`
# service tier `on_demand` on tokens per minute (TPM)", and the same request succeeded on keys of other orgs. Measured
# 2026-09-26 (jobs 20260926-050510-e32e and three siblings): raised at once with no rotation, every such task sat in
# the dead-rerun rest holding its job's slot. So the request goes to the next key it has not tried, at once, and only
# when every key's account refused it does the 413 reach failover. The key is not rested: a smaller request fits it.
# A context-window 413 ("context length exceeded") is the model's and names no account, so it fails over as before.
_ACCOUNT_LIMIT_413 = re.compile(r"organi[sz]ation|service tier|tokens per minute|\bTPM\b|rate.?limit|quota", re.I)


def _until_quota_reset(now: dt.datetime | None = None) -> float:
    """Seconds until the next daily-quota reset (QUOTA_RESET_UTC_HOUR:00 UTC)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    reset = now.replace(hour=QUOTA_RESET_UTC_HOUR, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += dt.timedelta(days=1)
    return (reset - now).total_seconds()

# A per-request READ timeout, well under a task's own budget, so a stalled call errors inside the task
# instead of holding a worker slot until Task.timeout_s (600s default) kills the whole task with no
# retry and no failover (docs/todo/improvements/tooling/a-stalled-model-request-eats-a-whole-zswarm-task.md).
# Measured 2026-09-23 from ~/.zswarm/ledger.jsonl, `api` rows with status ok over the last 7 days,
# per-call latency = seconds / calls (the finding's own metric), cross-checked against api_seconds/calls
# (provider wait only, no local tool time - the two agreed within a few percent everywhere except the
# two rows the bug itself pollutes, noted below):
#   groq-gpt-oss-120b     p50  0.6s  p95   2.0s  p99    4.3s  (n=8060)
#   deepseek-flash        p50  3.6s  p95  13.0s  p99   23.2s  (n=3460)
#   glm-4.5-air           p50 23.4s  p95  62.4s  p99   83.0s  (n=781)
#   mistral-medium-3.5    p50  8.6s  p95  88.3s  p99  148.0s  (n=15878) - the slowest HONEST leg
#   deepseek-flash-or     p50 24.1s  p95 329.0s  p99  575.8s  (n=6645) - tail is the bug measuring itself
#   gemini-3.8-flash      p50 25.3s  p95 412.7s  p99  797.0s  (n=2172) - tail is the bug measuring itself
# The two router legs' own tails are not real model latency: a call that stalled and eventually answered
# still counts "ok" after eating most of TODAY's 600s read timeout on one or more retried attempts inside
# one client.chat() call (`calls` counts logical turns, not HTTP attempts) - which is this bug, caught in
# the act. Every other leg's honest p99 sits under 150s, so READ_TIMEOUT_S sits above mistral's 148s with
# headroom, not above the contaminated router tails.
READ_TIMEOUT_S = 180.0
# The ledger does not log reasoning_effort per row, so a per-tier p99 cannot be read off it directly. A
# deliberate high/max-effort call (Task.EFFORTS) is scaled up from the measured base instead of breaking
# on it, on the same low/high/max ladder Task._normalised already uses (low is the `api` backend's own
# default). Doubling per step keeps `max` (540s) comfortably inside Task.timeout_s's 600s default.
REASONING_EFFORT_READ_SCALE = {"low": 1.0, "high": 2.0, "max": 3.0}
# A stalled read is retried once - no backoff, the wait already spent most of the window - before it is
# raised as a provider error shaped like a 504, so jobs.leg_unavailable fails it over the same way an
# ordinary error already does.
STALL_RETRIES = 1
_body = request_body  # older name, kept for the tests


def _read_timeout_s(reasoning_effort: str | None) -> float:
    """The read timeout for one POST: READ_TIMEOUT_S, scaled up for a deliberately slower
    reasoning_effort (Task.EFFORTS; unset - or a provider that does not carry the field - reads as
    "low", the same default Task._normalised gives the api backend)."""
    return READ_TIMEOUT_S * REASONING_EFFORT_READ_SCALE.get(reasoning_effort or "low", 1.0)


DEAD_REST_BASE_S = 600.0        # first strike: ten minutes
DEAD_REST_CAP_S = 6 * 3600.0    # a key that keeps failing is rested at most six hours before its next chance
DEAD_STRIKES = 3                # a revoked key that has struck out this many times is DISABLED, not rested again
STATE_RECHECK_S = 5.0           # how often a pool re-reads the shared state file
STATE_READ_TRIES = 5            # reads of it tried while another process holds it mid-replace (KeyPool._load)
BROKE_REST_S = DEAD_REST_CAP_S  # kept for anything that still reads it; a key out of credit is now disabled, not timed
BALANCE_FRESH_S = 1800.0        # a balance reading older than this is stale and probed again before a job's first task
DISABLED_RECHECK_S = 6 * 3600.0 # ...but a key in the disabled slot is re-read at most this often (see KeyPool.stale_keys)
NO_CREDIT_RECHECK_S = 24 * 3600.0 # a key that AUTHENTICATES but is out of credit winds down longer still (owner, 2026-09-23:
                                  # "the ones that authenticate with no credit should be on, like, a 24-hour wind-down")
PROBE_WIDTH = 16                # balance GETs in flight at once; a probe of every key used to run them one by one
LOCK_WAIT_S = 2.0               # how long a write waits for the shared state file's lock before going ahead without it

OUT_OF_CREDIT = "out of credit (402)"
REVOKED = "revoked (401/403)"


class NoUsableKey(RuntimeError):
    """Every key in the pool is disabled. Raised instead of sending a request that is certain to fail."""


def _num(v: object) -> float | None:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def balance_reading(body: object, provider: str = config.DEFAULT_PROVIDER) -> tuple[float | None, bool | None]:
    """What a provider's balance body says: (balance in USD or None, available flag or None).

    DeepSeek's shape is {"is_available": bool, "balance_infos": [{"currency": "USD", "total_balance": "39.53"}, ...]};
    the number is the USD row's (or an unlabelled row's, a provider with one balance); a body in another
    currency only reads its flag. OpenRouter's /key is {"data": {"limit_remaining": null|number, ...}} and its
    /credits is {"data": {"total_credits", "total_usage"}}; `openrouter_reading` handles that pair. An unknown
    shape reads as (None, None): a probe that learned nothing, which disables nothing.

    Whether the number may DISABLE a key is the provider's `balance_authority`, not this function's business:
    it only reports what the body said.
    """
    if not isinstance(body, dict):
        return None, None
    if provider == "openrouter" or ("data" in body and isinstance(body.get("data"), dict) and "total_credits" in (body.get("data") or {})):
        return openrouter_reading(body)
    avail = body.get("is_available")
    available = avail if isinstance(avail, bool) else None
    usd: float | None = None
    infos = body.get("balance_infos")
    if isinstance(infos, list):
        rows = [r for r in infos if isinstance(r, dict)]
        pick = next((r for r in rows if str(r.get("currency") or "USD").upper() == "USD"), None)
        if pick is not None:
            usd = _num(pick.get("total_balance"))
    return usd, available


def openrouter_reading(body: object) -> tuple[float | None, bool | None]:
    """OpenRouter's credit picture, as a number only - the flag is always None, because OpenRouter sends no
    word on whether it will serve a key and its number is demonstrably not one (see the module docstring).

    A key with a spending limit set reports `limit_remaining` and that is the honest figure. Without one, the
    account's /credits pair is merged in by `credit_of` and read as total_credits - total_usage.
    """
    if not isinstance(body, dict):
        return None, None
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    rem = _num(data.get("limit_remaining"))
    if rem is not None:
        return rem, None
    credits_, used = _num(data.get("total_credits")), _num(data.get("total_usage"))
    if credits_ is not None and used is not None:
        return credits_ - used, None
    return None, None


def openrouter_free_left(body: object) -> int | None:
    """Free-tier requests left on an OpenRouter key today, from /key's `free_model_daily_requests.remaining`.
    None when the body does not say. A key out of PAID credit but with this left still serves `:free` models."""
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    free = data.get("free_model_daily_requests")
    if not isinstance(free, dict):
        return None
    n = _num(free.get("remaining"))
    return None if n is None else int(n)


def _lock_fd(fd: int) -> None:
    if msvcrt is not None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if msvcrt is not None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(lock: Path):
    """Hold `lock` across one read-modify-write, so two processes cannot each build on the old file and lose the
    other's change. Best effort: a lock not taken within LOCK_WAIT_S is skipped, and a lock file that cannot be
    opened is never fatal; the write goes ahead either way."""
    fd = None
    held = False
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                _lock_fd(fd)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break  # a holder past LOCK_WAIT_S on a small write is a stuck process: go ahead unlocked
                time.sleep(0.005)
    except OSError:
        pass
    try:
        yield
    finally:
        if fd is not None:
            if held:
                try:
                    _unlock_fd(fd)
                except OSError:
                    pass
            os.close(fd)


def key_tier(provider: str, fingerprint: str) -> float:
    """A key's priority tier (settings.set_key_priority): its number, or infinity when it has none, so unnumbered keys
    are the last tier. KeyPool.pick serves by it and settings.key_rows lists by it, so the two cannot drift."""
    return (config.PROVIDERS.get(provider, {}).get("key_priority") or {}).get(fingerprint, math.inf)


def read_key_state() -> dict | None:
    """The shared key-state file (config.KEYS_STATE) parsed: {} when there is none, None when it cannot be read now
    (a torn or foreign file, or Windows refusing every try) and the caller keeps what it had."""
    for attempt in range(STATE_READ_TRIES):
        try:
            data = json.loads(config.KEYS_STATE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except PermissionError:
            # Windows refuses a read while another process os.replace()s the file: 2 of 807 reads on 2026-09-25,
            # with a dozen processes writing it. A NEW pool that kept the empty state it started with read every
            # disabled key as live (4,287 dead OpenRouter keys looked usable), so the read is tried again.
            time.sleep(0.01 * (attempt + 1))
        except (OSError, ValueError):
            return None  # a torn or foreign file is ignored, never fatal
    return None


class KeyPool:
    """Round-robin over the configured keys, skipping any that are resting and any that are DISABLED.

    The rest/strike/disabled state is SHARED across every zswarm process on the machine through
    `config.KEYS_STATE` (keyed by key fingerprint, never the key): one MCP server runs per Claude
    session, and a key found out of credit by one must not be re-tried by the other nine. A revoked
    key rests ten minutes, then twenty, forty, up to six hours, and is DISABLED once it has struck
    out; a key out of credit is disabled on the spot; a 429 rests for its Retry-After and adds no
    strike; the first 200 on a key clears its whole record, so a topped-up key heals itself. Every
    write re-reads the file under its lock first: two processes each writing back their own copy
    erased each other's disables (each re-read at most every five seconds) until 2026-09-16.

    Disabled is the one state with no timer. `pick()` never returns a disabled key; when they are all
    disabled it raises NoUsableKey rather than send a request that is certain to fail. The way back is
    a free balance probe that sees a top-up, or `zswarm keys enable <fingerprint>`.
    """

    def __init__(self, keys: list[str], provider: str = config.DEFAULT_PROVIDER, state: dict | None = None):
        """`state`: the shared file already read (read_key_state), for a caller that builds many pools at once only
        to show them (the console's snapshot read the 1.8 MB file twice per provider, 0.3 s per page)."""
        if not keys:
            raise RuntimeError(config.no_key_message(provider))
        self.provider = provider
        self._keys = list(dict.fromkeys(keys))
        self._fp = {k: config.fingerprint(k) for k in self._keys}
        self._i = 0
        self._state: dict[str, dict] = {}
        self._state_checked = 0.0
        if state is None:
            self._load(force=True)
        else:
            self._state, self._state_checked = state, time.time()

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    # ---- shared state --------------------------------------------------------------

    def _load(self, force: bool = False) -> None:
        """Read the shared file: every STATE_RECHECK_S for a pick, always when `force` (every write, under the
        lock). It is read outright, never trusted to an mtime: two writes inside one clock tick carry the same
        stamp on Windows, and a reader keyed on it kept the older one (caught by the test, 2026-09-16)."""
        now = time.time()
        if not force and now - self._state_checked < STATE_RECHECK_S:
            return
        self._state_checked = now
        if (data := read_key_state()) is not None:
            self._state = data

    def _save(self) -> None:
        p = config.KEYS_STATE
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")  # one per process: a shared name was torn between two
            tmp.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass

    def _locked(self):
        """Hold the state file's lock (`keys.json.lock` beside it) across one read-modify-write (file_lock)."""
        return file_lock(config.KEYS_STATE.with_name(config.KEYS_STATE.name + ".lock"))

    def _parked(self, key: str, now: float) -> bool:
        """Out of credit. Sticky since 2026-09-17: the older `broke` entries carried a six-hour timer and are
        still honoured while theirs runs, so an upgrade mid-flight never puts a dead key back into rotation."""
        e = self._entry(key)
        if e.get("disabled"):
            return True
        return bool(e.get("broke")) and float(e.get("rest_until") or 0.0) > now

    def _disabled(self, key: str) -> bool:
        return bool(self._entry(key).get("disabled"))

    def _free_ok(self, key: str) -> bool:
        """A key disabled for a 402 that still holds free-tier allowance: dead for paid models, alive for `:free`."""
        e = self._entry(key)
        return bool(e.get("disabled")) and bool(e.get("free_ok"))

    def _entry(self, key: str) -> dict:
        return self._state.get(self._fp[key]) or {}

    def _rest_until(self, key: str) -> float:
        return float(self._entry(key).get("rest_until") or 0.0)

    def _usable(self, key: str, free: bool, now: float | None = None) -> bool:
        """Out of credit is out of credit, whether this pool disabled it or an older build parked it on a timer:
        both read through _parked, so the twelve keys a pre-2026-09-17 build left parked report and behave as
        disabled instead of masquerading as merely 'resting'."""
        return (not self._parked(key, now if now is not None else time.time())) or (free and self._free_ok(key))

    # ---- the pool ---------------------------------------------------------------------

    def pick(self, free: bool = False, exclude: Collection[str] = ()) -> str:
        """The next key that is neither disabled nor resting; if all the live ones are resting, the one that
        wakes soonest. `free=True` is a request for a zero-cost model, which a key disabled for a 402 can still
        serve while its free-tier allowance lasts. `exclude` is the keys this request already found too small
        (ChatClient._post's account-limit 413). NoUsableKey when nothing is left: sending the request anyway
        would burn a worker to rediscover what the pool already knows."""
        self._load()
        now = time.time()
        live = [k for k in self._keys if self._usable(k, free, now) and k not in exclude]
        if not live:
            raise NoUsableKey(self._nothing_left_message(free))
        # Priority tiers (settings.set_key_priority): the lowest number with a ready key serves, its keys taking
        # turns; unnumbered keys are the last tier. With no numbers at all this is plain round-robin.
        def tier(k: str) -> float:
            return key_tier(self.provider, self._fp[k])
        ready = [k for k in live if self._rest_until(k) <= now]
        if ready:
            best = min(map(tier, ready))
            for _ in range(len(self._keys)):
                k = self._keys[self._i % len(self._keys)]
                self._i += 1
                if k in ready and tier(k) == best:
                    return k
        return min(live, key=self._rest_until)

    def _nothing_left_message(self, free: bool = False) -> str:
        n = len(self._keys)
        why = sorted({str(self._entry(k).get("disabled_reason") or "disabled") for k in self._keys})
        free_left = sum(1 for k in self._keys if self._entry(k).get("free_ok"))
        tail = f" ({free_left} of them still serve ':free' models)" if free_left and not free else ""
        return (f"every one of the {n} {self.provider} keys is disabled: {', '.join(why)}{tail}. "
                f"Top one up and run `zswarm keys probe --provider {self.provider}`, or re-enable one with "
                f"`zswarm keys enable <fingerprint> --provider {self.provider}`; `zswarm keys` lists them.")

    def rest(self, key: str, seconds: float, status: int | None = None, dead: bool = False) -> None:
        """Rest a key. `dead` (401/403) adds a strike and doubles the rest each time, up to the cap; once it has
        struck out DEAD_STRIKES times the key is DISABLED instead, because a key that has been revoked for six
        hours is not coming back on its own and re-trying it forever is what the disabled slot exists to stop."""
        with self._locked():
            self._load(force=True)
            now = time.time()
            e = dict(self._entry(key))
            if dead:
                e["strikes"] = int(e.get("strikes") or 0) + 1
                # The exponent is capped: a key probed 1,024 times made 2**1023 overflow float and crashed every probe.
                seconds = min(DEAD_REST_CAP_S, DEAD_REST_BASE_S * (2 ** min(e["strikes"] - 1, 30)))
                if e["strikes"] >= DEAD_STRIKES:
                    self._disable_entry(e, REVOKED, status=status, now=now)
            e["rest_until"] = max(float(e.get("rest_until") or 0.0), now + seconds)
            e["status"] = status
            e["last"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            self._state[self._fp[key]] = e
            self._save()

    _READINGS = ("balance_usd", "balance_at", "probed_at", "free_left")

    @staticmethod
    def _disable_entry(e: dict, reason: str, status: int | None = None, now: float | None = None, free_ok: bool | None = None) -> dict:
        """Mark one state entry disabled, in place. `broke` rides along so anything still reading the older flag
        (a `doctor` from another checkout mid-upgrade) sees the key as out; `disabled` is the authority."""
        e["disabled"] = True
        e["disabled_reason"] = reason
        e["disabled_status"] = status
        e["disabled_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        e["broke"] = True
        if free_ok is not None:
            e["free_ok"] = bool(free_ok)
        elif reason == OUT_OF_CREDIT:
            e["free_ok"] = bool(e.get("free_left"))  # last /key probe said there was free-tier allowance left
        return e

    def recover(self, key: str, free: bool = False) -> None:
        """A key that just answered 200 is healthy: its rest, strikes and disabling are cleared (a write only when
        there was something to clear). The last balance reading stays, so a working key is not re-probed for it.

        EXCEPT after a `:free` call on a key that is disabled with free-tier allowance left. That 200 proves the
        free tier works and says nothing about paid credit, so clearing the disable on it would put a key with no
        money back into PAID rotation - where the very next request 402s. Found in review, 2026-09-17, before it
        ever shipped: every free call on such a key silently undid its own disable.
        """
        self._load()  # the copy this process has (a few seconds old at most): a write only when it shows something to clear
        if free and self._free_ok(key):
            return
        e = self._state.get(self._fp[key]) or {}
        if all(k in self._READINGS for k in e):
            return
        with self._locked():
            self._load(force=True)
            e = self._state.get(self._fp[key]) or {}
            kept = {k: e[k] for k in self._READINGS if k in e}
            if kept:
                self._state[self._fp[key]] = kept
            else:
                self._state.pop(self._fp[key], None)
            self._save()

    def broke(self, key: str, status: int | None = 402, balance_usd: float | None = None, free_ok: bool | None = None) -> None:
        """DISABLE a key that is OUT OF CREDIT: a 402 from the chat endpoint, or - for a provider whose number is
        its own word on the matter - a balance probe the provider calls unavailable, or at or under zero when it
        sends no flag. Sticky: no timer, never handed to a worker again (except `:free` models while the key's
        free-tier allowance lasts). A 402 counts as a reading (the balance is empty), so the pool does not pay to
        probe it again inside the freshness window. The next positive probe, or a 200, clears the whole record."""
        with self._locked():
            self._load(force=True)
            now = time.time()
            e = dict(self._entry(key))
            # A strike is a NEW failure. A probe that reads "still empty" on a key already disabled is the same
            # failure seen again, and counting it would turn every disabled key "dead" after three probes.
            if not self._parked(key, now):
                e["strikes"] = int(e.get("strikes") or 0) + 1
            e["status"] = status
            e["balance_usd"] = balance_usd if balance_usd is not None else min(0.0, float(e.get("balance_usd") or 0.0))
            e["balance_at"] = now
            e["last"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            self._disable_entry(e, OUT_OF_CREDIT, status=status, now=now, free_ok=free_ok)
            self._state[self._fp[key]] = e
            self._save()

    def disable(self, key: str, reason: str = "disabled by hand", status: int | None = None) -> None:
        """Put a key in the disabled slot outright (the `zswarm keys disable` path)."""
        with self._locked():
            self._load(force=True)
            e = dict(self._entry(key))
            e["last"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            self._disable_entry(e, reason, status=status, free_ok=False)
            self._state[self._fp[key]] = e
            self._save()

    def enable(self, key_or_fp: str) -> bool:
        """Take one key out of the disabled slot and clear its rest and strikes, addressed by key or by
        fingerprint (the only form a human ever sees). False when no key in this pool matches."""
        target = next((k for k in self._keys if k == key_or_fp or self._fp[k] == key_or_fp), None)
        if target is None:
            return False
        with self._locked():
            self._load(force=True)
            e = dict(self._entry(target))
            kept = {k: e[k] for k in self._READINGS if k in e}
            kept.pop("balance_at", None)  # force a fresh probe rather than trust the reading that disabled it
            if kept:
                self._state[self._fp[target]] = kept
            else:
                self._state.pop(self._fp[target], None)
            self._save()
        return True

    def probation(self, max_age_s: float = NO_CREDIT_RECHECK_S) -> list[str]:
        """The one way back for a provider with NO balance endpoint (Hugging Face): a key disabled for CREDIT
        there is let out once every max_age_s, because there is no free GET that could ever see its allowance
        come back and a monthly one comes back on its own. The next 402 disables it again, so the cost of being
        wrong is one free failed request per window per key, not a retry loop. The default is the 24-hour
        no-credit wind-down (owner, 2026-09-23): an out-of-credit key that will not be topped up should not be
        retried every few hours. A REVOKED key is never let out: that is not a thing that heals. Returns the
        fingerprints let back in."""
        now = time.time()
        out = []
        for k in self._keys:
            e = self._entry(k)
            if not e.get("disabled") or e.get("disabled_reason") != OUT_OF_CREDIT:
                continue
            try:
                since = dt.datetime.fromisoformat(str(e.get("disabled_at"))).timestamp()
            except (TypeError, ValueError):
                since = 0.0
            if now - since > max_age_s and self.enable(k):
                out.append(self._fp[k])
        return out

    def enable_all(self) -> int:
        now = time.time()
        return sum(1 for k in self._keys if self._parked(k, now) and self.enable(k))

    def disabled(self) -> list[str]:
        """Fingerprints currently in the disabled slot, including any an older build left parked on a timer."""
        self._load(force=True)
        now = time.time()
        return [self._fp[k] for k in self._keys if self._parked(k, now)]

    def any_key(self) -> str:
        """Any key, disabled or not. Only for a FREE GET (the balance/models endpoints): probing a disabled key
        costs nothing and is exactly how a topped-up one gets back out of the slot, so `pick`'s refusal to hand
        out a disabled key must not make the pool unrecoverable."""
        try:
            return self.pick()
        except NoUsableKey:
            return self._keys[self._i % len(self._keys)]

    @property
    def balance_authority(self) -> str:
        """"reading": the provider's balance number (or its own availability flag) may disable a key.
        "status": the number is reported and never acted on; only a 402 from the chat endpoint disables.
        A provider that says nothing defaults to "reading", which is DeepSeek's long-standing behaviour."""
        return str(config.PROVIDERS.get(self.provider, {}).get("balance_authority") or "reading")

    def note_balance(self, key: str, usd: float | None, available: bool | None, free_left: int | None = None) -> bool:
        """Record a balance probe. The provider's own flag decides when it sends one (DeepSeek's `is_available`
        is its word on whether calls will be served); the number decides only when there is no flag, at or under
        zero being empty - and only for a provider whose `balance_authority` is "reading". Usable: the key is
        cleared and the reading kept, so a top-up heals a disabled key without a worker paying to find out.
        Not usable: disabled. An unknown reading (None, None) is a probe that learned nothing: it stamps the time
        so the pool does not ask again inside the window, and leaves the key's record, its disabling and its last
        balance alone. Returns whether the key is usable now."""
        usable = available if available is not None else (usd is None or usd > 0.0)
        if not usable and self.balance_authority == "reading":
            self.broke(key, status=402, balance_usd=usd, free_ok=bool(free_left))
            return False
        learned = available is not None or usd is not None
        with self._locked():
            self._load(force=True)
            now = time.time()
            e = dict(self._entry(key))
            if learned and usable:
                e = {"balance_usd": usd, "balance_at": now}  # a positive reading clears rest, strikes and disabling
            else:
                # Either the probe learned nothing, or it read an empty balance for a provider whose number is not
                # authority (OpenRouter). Keep the reading for the report, keep the disabling as it stands.
                e["probed_at"] = now
                if learned:
                    e["balance_usd"], e["balance_at"] = usd, now
            if free_left is not None:
                e["free_left"] = int(free_left)
            self._state[self._fp[key]] = e
            self._save()
        return not self._parked(key, now)

    def stale_keys(self, max_age_s: float = BALANCE_FRESH_S, disabled_age_s: float = DISABLED_RECHECK_S) -> list[str]:
        """The keys whose balance has not been read within their window: never, or too long ago. A probe that
        failed counts (it stamped `probed_at`); without that, a balance endpoint that was down made every task of a
        job probe again, each waiting out the connect timeout per key while holding its slot.

        A key in the disabled slot gets the longer window. The owner does not expect to top the spent keys up
        (Michael, 2026-09-17: "I'll likely never top up any of the keys... You can occasionally check... assume
        no"), so re-reading thirteen dead keys every half hour only delayed jobs. A top-up is still found, just
        within hours instead of minutes; `zswarm keys probe` checks every key at once on demand.

        A key that AUTHENTICATES but is OUT OF CREDIT gets the longest window of all, NO_CREDIT_RECHECK_S
        (24 h, owner 2026-09-23): it is the case least likely to change on its own, so re-reading it even every
        six hours is wasted. A revoked or hand-disabled key still uses disabled_age_s."""
        self._load(force=True)
        now = time.time()

        def age(k: str) -> float:
            e = self._entry(k)
            return now - max(float(e.get("balance_at") or 0.0), float(e.get("probed_at") or 0.0))

        def window(k: str) -> float:
            if not self._disabled(k):
                return max_age_s
            if self._entry(k).get("disabled_reason") == OUT_OF_CREDIT:
                # The 24 h wind-down is the DEFAULT for an out-of-credit key. An explicit window shorter than
                # the normal disabled one (a human `zswarm keys probe`, or disabled_age_s=0) still forces a
                # re-read, so on-demand probing is never blocked by the wind-down.
                return disabled_age_s if disabled_age_s < DISABLED_RECHECK_S else NO_CREDIT_RECHECK_S
            return disabled_age_s

        # At least the window, not more than it: a read stamped in the same clock tick is 0 s old, and Windows ticks
        # coarsely, so with `>` a window of 0 (`zswarm keys probe`, "re-read every key now") skipped keys just read
        # (the Windows legs of CI, 2026-09-27).
        return [k for k in self._keys if age(k) >= window(k)]

    def balance_stale(self, max_age_s: float = BALANCE_FRESH_S, disabled_age_s: float = DISABLED_RECHECK_S) -> bool:
        """True when any key is due a balance read (`stale_keys`)."""
        return bool(self.stale_keys(max_age_s, disabled_age_s))

    def available(self, free: bool = False) -> int:
        """Keys that could take a request right now: not disabled, not resting."""
        self._load()
        now = time.time()
        return sum(1 for k in self._keys if self._usable(k, free, now) and self._rest_until(k) <= now)

    def live_besides(self, exclude: Collection[str], free: bool = False) -> int:
        """Keys outside `exclude` that are not disabled, resting or not: what pick(exclude=...) can still hand out."""
        self._load()
        now = time.time()
        return sum(1 for k in self._keys if k not in exclude and self._usable(k, free, now))

    def soonest_wake(self, free: bool = False) -> float:
        """Seconds until some key can take a request: 0 when one can now, the shortest rest left when every live
        key is resting (a 429, a day-spent quota), inf when every key is disabled."""
        self._load()
        now = time.time()
        rests = [max(0.0, self._rest_until(k) - now) for k in self._keys if self._usable(k, free, now)]
        return min(rests, default=float("inf"))

    def next_with_balance(self, exclude: set[str] | None = None) -> tuple[str | None, float]:
        """For a `cc` run, which takes ONE key for its whole life: the next key in turn that is neither disabled
        nor in `exclude` (the keys this task already died on), and the seconds to wait for it: 0 when it is free
        now, the time until it wakes when every candidate is resting (a 429, a dead strike). (None, 0.0) when
        there is no candidate: every key left is disabled, and nothing should be launched."""
        self._load(force=True)
        now = time.time()
        exclude = exclude or set()
        resting: list[tuple[float, str]] = []
        for _ in range(len(self._keys)):
            k = self._keys[self._i % len(self._keys)]
            self._i += 1
            if k in exclude or self._parked(k, now):
                continue
            until = self._rest_until(k)
            if until <= now:
                return k, 0.0
            resting.append((until, k))
        if not resting:
            return None, 0.0
        until, k = min(resting)
        return k, until - now

    def status(self, reload: bool = True) -> list[dict]:
        """Fingerprints and rest/disabled state only. Never the keys. reload=False for a pool built this moment."""
        if reload:
            self._load(force=True)
        now = time.time()
        out = []
        for k in self._keys:
            e = self._entry(k)
            strikes = int(e.get("strikes") or 0)
            disabled = self._parked(k, now)  # a legacy timed park counts: it means the same thing
            free_ok = self._free_ok(k)
            out.append({
                "fingerprint": self._fp[k], "provider": self.provider,
                "state": ("disabled (:free only)" if disabled and free_ok else "disabled" if disabled
                          else "resting" if self._rest_until(k) > now else "ok"),
                "resting_s": round(max(0.0, self._rest_until(k) - now), 1),
                "strikes": strikes, "status": e.get("status"), "dead": strikes >= DEAD_STRIKES, "last": e.get("last"),
                "disabled": disabled, "disabled_at": e.get("disabled_at"),
                "disabled_reason": e.get("disabled_reason") or (OUT_OF_CREDIT if disabled else None),
                "free_only": free_ok, "free_left": e.get("free_left"),
                "broke": self._parked(k, now),
                "balance_usd": e.get("balance_usd"),
                "balance_age_s": round(now - float(e["balance_at"]), 1) if e.get("balance_at") else None,
            })
        return out


def _call_options(model: str, entry: dict, max_tokens: int | None, thinking: bool | None,
                  reasoning_effort: str | None) -> tuple[int | None, bool | None, str | None]:
    """(max_tokens, thinking, reasoning_effort) for one chat call: capped at the model's ceiling, defaults filled in.
    A typed-only model is refused here, before anything is sent."""
    if entry.get("kind") == "typed":
        # Jev and its kind answer typed questions through their own API, never a chat: zswarm_decide calls them.
        raise ValueError(f"{model} answers typed questions only (pick one, yes/no, rate on a scale); ask it through "
                         "zswarm_decide, and give a task a chat model")
    # A model's own output ceiling (`max_out` in its provider file) caps what a call asks for: Cohere refuses Command
    # A asked for 16,000 tokens with a 400 (its limit is 8,192), which ended the call before it started (2026-09-26).
    if max_tokens and entry.get("max_out"):
        max_tokens = min(int(max_tokens), int(entry["max_out"]))
    if reasoning_effort is None:
        reasoning_effort = entry.get("default_reasoning_effort")
    if thinking is None:
        thinking = entry.get("default_thinking")
    return max_tokens, thinking, reasoning_effort


def _set_reasoning(body: dict, model: str, entry: dict, allowed: set, thinking: bool | None, reasoning_effort: str | None) -> None:
    # `thinking=False` is DeepSeek's own switch; OpenRouter drops it and takes `reasoning: {enabled: false}` instead,
    # and its `reasoning_effort` turns reasoning ON. A schema ask turns thinking off (agent.ask) and OpenRouter never
    # heard it: Dredd's drafter on deepseek-flash-or spent all 4000 output tokens on reasoning, finish "length", no
    # tool call, 0 chars, eight calls running (2026-09-24). The same call with reasoning off: 10.5 s, 0 reasoning
    # tokens, a valid submit_result, a third of the cost (measured on the real prompt the same night).
    mandatory = entry.get("reasoning_mandatory") or model in _REASONING_MANDATORY
    if thinking is False and "thinking" not in allowed and "reasoning" in allowed and not mandatory:
        body["reasoning"] = {"enabled": False}
        body.pop("reasoning_effort", None)
    elif entry.get("benchmark_slug") and "reasoning" in allowed and (thinking is True or reasoning_effort is not None):
        body["reasoning"] = {"enabled": True}
        if reasoning_effort is not None:
            body["reasoning"]["effort"] = reasoning_effort


def _chat_result(r: httpx.Response, attempts: int, model: str, t0: float, upstream_header: str | None) -> ChatResult:
    data = r.json()
    if upstream_header and isinstance(data, dict) and not data.get("provider") and r.headers.get(upstream_header):
        data["provider"] = r.headers[upstream_header]  # a router that names the serving host in a header, not the body
    choice = (data.get("choices") or [{}])[0]
    usage = Usage.from_api(data.get("usage"))
    now = dt.datetime.now(dt.timezone.utc)
    # A provider that bills the call and says what it billed is the authority on its own cost; our price table
    # is the fallback, and None ('- not measured') the fallback's fallback. OpenRouter routes one model id to
    # several upstreams at different rates, so its number is the only honest one.
    u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    # OpenRouter says `cost`, the Hugging Face router `estimated_cost`: both are the provider's own figure.
    reported = _num(u.get("cost")) if u.get("cost") is not None else _num(u.get("estimated_cost"))
    return ChatResult(
        message=choice.get("message") or {}, finish_reason=choice.get("finish_reason") or "", usage=usage, model=model,
        seconds=time.perf_counter() - t0,
        cost_usd=reported if reported is not None else config.cost_usd(model, usage.hit, usage.miss, usage.out, now),
        peak=config.is_peak(now), attempts=attempts, raw=data,
    )


class ChatClient:
    """One provider's chat endpoint. `DeepSeekClient` is the same class under its older name."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None, max_connections: int = 512, timeout_s: float = 600.0, max_attempts: int = 7,
                 api_keys: list[str] | None = None, provider: str = config.DEFAULT_PROVIDER):
        if provider not in config.PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}; known: {sorted(config.PROVIDERS)}")
        self.provider = provider
        self.spec = config.PROVIDERS[provider]
        keys = list(api_keys) if api_keys else ([api_key] if api_key else config.load_api_keys(provider))
        self.pool = KeyPool(keys, provider)
        self._key = self.pool.keys[0]  # older name, kept for anything that still reads it
        self.max_attempts = max(max_attempts, len(self.pool) + 2)
        # One pool sized for the fan-out: 512 keep-alive connections, no pool timeout, so 200 concurrent workers never queue here.
        # The Authorization header is set PER REQUEST, so the key can rotate without a new connection pool.
        # `timeout_s` is the CLIENT-LEVEL default - still what the free balance/models GETs read. The chat
        # POST (`_post`) is the call this finding is about, so it carries its own read timeout per request
        # instead (READ_TIMEOUT_S, scaled by reasoning_effort), well under a task's own budget rather than
        # sharing it with the task.
        self._http = httpx.AsyncClient(
            base_url=base_url or self.spec["base_url"],
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_s, connect=30.0, pool=None),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )
        self.calls = 0
        self.retries = 0
        self.rotations = 0
        self.stalls = 0  # requests that hit the read timeout with no reply at all, counted apart from a transport error
        self.gate = None  # leggate.LegGate, set by the JobManager: a 200 opens it by one, a 429 on a resting pool halves it

    def _auth(self, key: str) -> dict:
        """The per-request headers: the bearer token plus whatever the provider wants for attribution
        (OpenRouter reads HTTP-Referer and X-Title; both are public identifiers, never credentials)."""
        return {"Authorization": "Bearer " + key, **(self.spec.get("headers") or {})}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "ChatClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def get_json(self, path: str, key: str | None = None) -> dict:
        # any_key, not pick: these GETs are free, and a pool where every key is disabled must still be probeable.
        r = await self._http.get(path, headers=self._auth(key or self.pool.any_key()))
        if r.status_code != 200:
            raise ApiError(r.status_code, r.text, self.provider)
        return r.json()

    async def models(self) -> list[str]:
        if not self.spec.get("models_path"):
            return []
        return [m["id"] for m in (await self.get_json(self.spec["models_path"])).get("data", [])]

    async def balance(self) -> dict:
        if not self.spec.get("balance_path"):
            return {"note": f"{self.provider} has no balance endpoint"}
        return await self.get_json(self.spec["balance_path"])

    async def balances(self, keys: list[str] | None = None) -> list[dict]:
        """One row per key (every key in the pool, or just `keys`): fingerprint plus that key's balance (or its
        error). Never the key. The GETs run PROBE_WIDTH at a time: one by one, sixteen DeepSeek keys took 7 s.

        The reading is ACTED on: a key at or under zero is parked and one above zero is cleared. (Until
        2026-09-16 a key that merely ANSWERED the balance endpoint was cleared, so every `doctor` run put the
        empty keys back into rotation and the next worker died of 402 on one of them.)
        """
        wanted = None if keys is None else set(keys)
        pairs = [(k, st) for k, st in zip(self.pool.keys, self.pool.status()) if wanted is None or k in wanted]
        gate = asyncio.Semaphore(PROBE_WIDTH)

        async def one(k: str, st: dict) -> dict:
            async with gate:
                return await self._balance_row(k, st)

        return list(await asyncio.gather(*(one(k, st) for k, st in pairs)))

    async def _balance_row(self, k: str, st: dict) -> dict:
        row: dict = {"fingerprint": config.fingerprint(k), "strikes": st["strikes"], "dead": st["dead"], "resting_s": st["resting_s"],
                     "state": st["state"], "disabled": st["disabled"], "disabled_reason": st["disabled_reason"]}
        if self.spec.get("balance_path"):
            try:
                body = await self.get_json(self.spec["balance_path"], key=k)
                free_left = openrouter_free_left(body) if self.provider == "openrouter" else None
                body = await self._merge_credits(body, k)
                row["balance"] = body
                usd, available = balance_reading(body, self.provider)
                row["credit_usd"], row["free_left"] = usd, free_left
                row["usable"] = self.pool.note_balance(k, usd, available, free_left=free_left)
                if usd is not None and usd <= 0 and self.pool.balance_authority == "status":
                    # Reported, not acted on: this provider's number is not its word on whether it will serve the key.
                    row["note"] = f"credit reads {usd:.4f}, which {self.provider} does not treat as out of credit; only a 402 disables"
            except ApiError as e:
                row["error"] = f"{type(e).__name__}: {e}"[:160]
                if e.status == 402:
                    self.pool.broke(k, status=402)
                    row["usable"] = False
                elif e.status in DEAD_KEY_STATUS:
                    self.pool.rest(k, 0, status=e.status, dead=True)
                    row["usable"] = False
                else:
                    row["usable"] = self.pool.note_balance(k, None, None)  # the endpoint failed: a probe that learned nothing
            except Exception as e:  # noqa: BLE001 - a dead key is reported, not fatal
                row["error"] = f"{type(e).__name__}: {e}"[:160]
                row["usable"] = self.pool.note_balance(k, None, None)
        return row

    async def _merge_credits(self, body: dict, key: str) -> dict:
        """OpenRouter's /key answers about the KEY (its spending limit, its free-tier allowance) and its /credits
        about the ACCOUNT behind it. A key with no limit set reports `limit_remaining: null`, so the account pair
        is the only number there is; fetch it and fold it in. A failure here is not fatal: the /key body stands."""
        path = self.spec.get("credits_path")
        if not path or not isinstance(body, dict):
            return body
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        if data.get("limit_remaining") is not None:
            return body  # the key's own limit is the honest figure; no second call needed
        try:
            credits_ = (await self.get_json(path, key=key)).get("data") or {}
        except Exception:  # noqa: BLE001 - one extra GET, never worth failing the probe for
            return body
        return {**body, "data": {**data, **{k: v for k, v in credits_.items() if k in ("total_credits", "total_usage")}}}

    async def probe_balances(self, max_age_s: float = BALANCE_FRESH_S, disabled_age_s: float = DISABLED_RECHECK_S) -> list[str]:
        """Before a job's first task: probe the keys whose reading is due (`KeyPool.stale_keys`: a live key after
        max_age_s, a disabled one after disabled_age_s), one free GET each, so a key that is out of balance is
        parked before a worker is handed it and a topped-up one comes back. Returns the fingerprints of the probed
        keys that are not usable. Fresh readings probe nothing; a provider with no balance endpoint has nothing
        to probe, and gets the `probation` pass instead."""
        if not self.spec.get("balance_path"):
            # The 24-hour no-credit wind-down is the default, not the 6-hour disabled window. An explicit shorter
            # window (disabled_age_s=0) still lets a credit-disabled key out now: the same rule as stale_keys.
            self.pool.probation(disabled_age_s if disabled_age_s < DISABLED_RECHECK_S else NO_CREDIT_RECHECK_S)
            return []
        due = self.pool.stale_keys(max_age_s, disabled_age_s)
        if not due:
            return []
        rows = await self.balances(due)
        return [r["fingerprint"] for r in rows if r.get("usable") is False]

    def _rotate_free(self, free: bool, attempt: int) -> bool:
        """Whether another key can take this request right now (and count the rotation if so)."""
        if len(self.pool) > 1 and self.pool.available(free=free) and attempt < self.max_attempts:
            self.rotations += 1
            return True
        return False

    def _dead_key(self, r: httpx.Response, key: str, free: bool, attempt: int) -> str:
        """Rest or disable the key the provider just rejected; "continue" means another key takes it at once."""
        if r.status_code == 402:
            self.pool.broke(key, status=402)  # out of credit: disabled until a probe sees a top-up
        else:
            self.pool.rest(key, 0, status=r.status_code, dead=True)
        if self._rotate_free(free, attempt):
            return "continue"  # another key takes it; the disabled one is reported by `zswarm keys`
        if r.status_code == 400:
            # Every key answered "not a valid API key". That is the LEG being unusable, not this
            # request being malformed, so say so in the language failover understands.
            raise NoUsableKey(f"NoUsableKey: every {self.provider} key was rejected as invalid ({r.text[:120]})")
        raise ApiError(r.status_code, r.text, self.provider)

    async def _after_response(self, r: httpx.Response, key: str, free: bool, attempt: int, resamples: int) -> tuple[str, int]:
        """A non-200 reply: rest/rotate/disable the key and say what the caller does next - "continue" (retry
        at once), "sleep" (back off first) - or raise. Returns the resample count it kept."""
        if (r.status_code == 429 and _OUT_OF_CREDIT_429.search(r.text or "")) or (
                r.status_code == 400 and _OUT_OF_SPEND_400.search(r.text or "")):
            # "429" (or groq's "400") but it means out of credit: disable, do not rest-and-retry forever.
            self.pool.broke(key, status=402)
            if self._rotate_free(free, attempt):
                return "continue", resamples
            raise NoUsableKey(f"NoUsableKey: every {self.provider} key is out of credit ({r.text[:100]})")
        if r.status_code == 429 and _DAILY_QUOTA_429.search(r.text or ""):
            self.pool.rest(key, _until_quota_reset(), status=429)  # spent for today: no retry on it until the reset
        elif r.status_code == 429 and (_ZERO_QUOTA_429.search(r.text or "") or _zero_limit_header(r.headers)):
            self.pool.rest(key, ZERO_QUOTA_REST_S, status=429)  # no quota where the call landed: waiting 20 s changes nothing
        elif r.status_code == 429:
            self.pool.rest(key, self._retry_after(r.headers.get("retry-after"), RATE_REST_S), status=429)
        elif r.status_code in DEAD_KEY_STATUS or (r.status_code == 400 and _KEY_SHAPED_400.search(r.text or "")):
            return self._dead_key(r, key, free, attempt), resamples
        elif r.status_code == 400 and _MODEL_OUTPUT_400.search(r.text or ""):
            # The model, not the request, produced something the provider refused. Resample.
            resamples += 1
            if resamples <= MODEL_OUTPUT_RETRIES:
                self.retries += 1
                await asyncio.sleep(self._backoff(resamples))
                return "continue", resamples
            raise ApiError(r.status_code, r.text, self.provider)
        if r.status_code not in RETRY_STATUS or attempt >= self.max_attempts:
            raise ApiError(r.status_code, r.text, self.provider)
        self.retries += 1
        if r.status_code == 429 and len(self.pool) > 1 and self.pool.available(free=free):
            self.rotations += 1
            return "continue", resamples  # a fresh key, no wait
        return "sleep", resamples

    async def _post(self, body: dict, free: bool = False, rest_budget_s: float | None = None, last_leg: bool = False) -> tuple[httpx.Response, int]:
        """POST with retries on transport errors and retryable statuses; returns (200 response, attempts).

        Each attempt takes the next key from the pool. A 429 rests that key and, when another key is free, the
        retry goes out immediately on it. A 402 DISABLES the key - it is out of credit, and no amount of retrying
        changes that - and the retry goes out on the next key at once; 401/403 rest it on the doubling ladder and
        disable it once it has struck out. `free` is a zero-cost model, which a key disabled for a 402 can still
        serve. The backoff sleep only happens when no other key can take the request. A 413 naming the key's account
        limit (_ACCOUNT_LIMIT_413) goes to a key this request has not tried yet, at once, and is raised only when
        every live key has refused it.

        A stalled read (no reply inside READ_TIMEOUT_S, scaled for reasoning_effort) is retried once at once, no
        backoff - the wait already spent most of the window - then raised as a provider error shaped like a 504,
        so jobs.leg_unavailable fails it over the same way an ordinary error already does, instead of the task
        holding its worker slot until its own budget runs out with nothing retried and nothing failed over.

        `rest_budget_s` is how long this call may sleep on 429s while EVERY key is rate-limited, and it is set only
        when the task has another leg to go to. Past it the 429 is raised at once and jobs.leg_unavailable fails the
        task over. Without it a saturated free pool held each task for its whole retry ladder, turn after turn:
        measured 2026-09-23 on a 74-task verify, tasks spent 25-33 minutes on the resting Gemini pool, then finished
        in 2-4 minutes on the next leg.

        The budget is WALL-CLOCK time since the first 429 of this call, rotations included (2026-09-24). Counting
        only sleeps missed the commoner case on a big free pool: a 429 while another key is free rotates at once
        with no sleep, so a task could make len(pool) + 2 round trips (74 on 72 Gemini keys) before erroring; job
        20260924-152821-54f1 lost 43 tasks at 600 s with zero replies that way. `last_leg` is a call with nowhere
        to fail over to: its budget (config.SATURATED_REST_S, set by the agent) ends in a `PoolSaturated` error.
        """
        attempt = 0
        rested = 0.0  # seconds slept on 429s while no key could take the request
        limited_since: float | None = None  # monotonic time of this call's first 429
        resamples = 0  # bounded re-rolls when the MODEL's output was refused (see _MODEL_OUTPUT_400)
        stalls = 0  # bounded retries of a read that never came back (see STALL_RETRIES)
        too_small: set[str] = set()  # keys whose account refused this request as over its limit (_ACCOUNT_LIMIT_413)
        read_s = _read_timeout_s(body.get("reasoning_effort"))
        req_timeout = httpx.Timeout(read_s, connect=30.0, pool=None)
        # Serialised once, here, so the egress receipt hashes the exact bytes that go on the wire.
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        sink = f"{self.provider}:{self._http.base_url.host}"
        headers_json = {"Content-Type": "application/json"}
        while True:
            attempt += 1
            self.calls += 1
            key = self.pool.pick(free=free, exclude=too_small)  # NoUsableKey when every key is disabled: fail loudly, do not hammer
            # One receipt per attempt: every retry is another copy of the bytes leaving. Fail-closed raises here, unsent.
            egress.record(sink, payload, provider=self.provider, model=str(body.get("model") or ""))
            try:
                r = await self._http.post("/chat/completions", content=payload, headers={**headers_json, **self._auth(key)}, timeout=req_timeout)
            except httpx.ReadTimeout:
                self.stalls += 1
                stalls += 1
                if stalls > STALL_RETRIES:
                    raise ApiError(504, f"stalled: no reply within {read_s:.0f}s (read timeout, retried once)", self.provider)
                self.retries += 1
                continue  # one immediate retry, whatever key pick() hands out next
            except httpx.TransportError:
                if attempt >= self.max_attempts:
                    raise
                self.retries += 1
                await asyncio.sleep(self._backoff(attempt))
                continue
            if r.status_code == 200:
                self.pool.recover(key, free=free)  # a :free 200 must not re-enable a key that is out of PAID credit
                if self.gate is not None:
                    self.gate.ok()
                _note_rate_wait(limited_since)
                return r, attempt
            if r.status_code == 413 and _ACCOUNT_LIMIT_413.search(r.text or ""):
                too_small.add(key)
                if attempt < self.max_attempts and self.pool.live_besides(too_small, free=free):
                    self.rotations += 1
                    continue
                raise ApiError(r.status_code, r.text, self.provider)
            if r.status_code == 429 and limited_since is None:
                limited_since = time.monotonic()
            action, resamples = await self._after_response(r, key, free, attempt, resamples)
            if r.status_code == 429 and self.gate is not None and not self.pool.available(free=free):
                self.gate.saturated()
            delay = self._retry_delay(attempt, r.headers.get("retry-after")) if action != "continue" else 0.0
            if r.status_code == 429 and rest_budget_s is not None:
                spent = time.monotonic() - (limited_since or time.monotonic())
                if max(spent, rested) + delay > rest_budget_s:
                    _note_rate_wait(limited_since)
                    raise ApiError(429, self._rate_limited_message(spent, rested, rest_budget_s, last_leg, r.text), self.provider)
            if action != "continue":
                if r.status_code == 429:
                    rested += delay
                await asyncio.sleep(delay)

    def _rate_limited_message(self, spent: float, rested: float, budget: float, last_leg: bool, text: str | None) -> str:
        waited = max(spent, rested)
        if last_leg:
            return (f"PoolSaturated: every {self.provider} key is rate-limited and the task has no other leg; waited {waited:.0f}s "
                    f"of the {budget:.0f}s a last-leg call may wait. Run fewer tasks at once (concurrency), or later "
                    f"({(text or '')[:200]})")
        return (f"every key is rate-limited; waited {waited:.0f}s of the {budget:.0f}s a call "
                f"with another leg may wait ({(text or '')[:200]})")

    @staticmethod
    def _retry_after(retry_after: str | None, default: float) -> float:
        try:
            return max(default, float(retry_after)) if retry_after else default
        except ValueError:
            return default

    async def chat(self, messages: list[dict], model: str = config.DEFAULT_MODEL, tools: list[dict] | None = None, tool_choice: str | dict | None = None, max_tokens: int | None = None, thinking: bool | None = None, reasoning_effort: str | None = None, response_format: dict | None = None, temperature: float | None = None, user: str | None = None, stop: list[str] | None = None, rest_budget_s: float | None = None, last_leg: bool = False) -> ChatResult:
        model = config.resolve_model(model)
        owner = config.provider_of(model)
        entry = config.MODELS[model]
        max_tokens, thinking, reasoning_effort = _call_options(model, entry, max_tokens, thinking, reasoning_effort)
        if owner != self.provider:
            raise ValueError(f"model {model} belongs to provider {owner!r}; this client talks to {self.provider!r} (use JobManager.client_for)")
        # An armed fault (ZSWARM_FAULTS, faults.arm) fails this call here, before any request or spend, so a
        # test or a drill can make the Nth call to a leg unavailable and watch the route fail over.
        faults.check(self.provider, model)
        api_id = config.api_model_id(model)  # `or:deepseek/deepseek-chat-v3.1` goes out as `deepseek/deepseek-chat-v3.1`
        body = self._chat_body(model, entry, api_id, messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens, thinking=thinking,
                               reasoning_effort=reasoning_effort, response_format=response_format, temperature=temperature, user=user, stop=stop)
        t0 = time.perf_counter()
        r, attempts = await self._post_chat(body, model, api_id.endswith(":free"), rest_budget_s, last_leg)
        return _chat_result(r, attempts, model, t0, self.spec.get("upstream_header"))

    def _chat_body(self, model: str, entry: dict, api_id: str, messages: list[dict], *, tools: list[dict] | None, tool_choice: str | dict | None,
                   max_tokens: int | None, thinking: bool | None, reasoning_effort: str | None, response_format: dict | None,
                   temperature: float | None, user: str | None, stop: list[str] | None) -> dict:
        """The request body, with only the fields this provider's endpoint accepts."""
        allowed = set(self.spec.get("options") or ())
        # Some OpenAI-compatible endpoints reject unknown/extra top-level fields with a 422 rather than
        # ignoring them. Mistral is strict about `user` ("extra_forbidden: body.user"), so a provider can
        # name standard fields to omit. Measured 2026-09-20: without this, magistral scored 0/24 on a 422.
        omit = set(self.spec.get("omit") or ())
        # Provider-specific fields go only where the endpoint accepts them; a Gemini or Kimi call never sees DeepSeek's `thinking`.
        body = request_body(api_id, messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens,
                            thinking=thinking if "thinking" in allowed else None, reasoning_effort=reasoning_effort if "reasoning_effort" in allowed else None,
                            response_format=response_format, user=None if "user" in omit else user, stop=stop)
        _set_reasoning(body, model, entry, allowed, thinking, reasoning_effort)
        if temperature is not None:
            body["temperature"] = temperature  # 0.0 is a real value here, unlike the other options
        if "usage" in allowed:
            body["usage"] = {"include": True}  # OpenRouter then reports what it ACTUALLY charged, per call
        # A registry entry may carry request fields of its own - the one that matters today is pinning an
        # OpenRouter model to a single upstream, so a leg the router calls "the same model" really is.
        # Filtered by the provider's allowed options, like every other non-standard field.
        for k, v in (entry.get("extra") or {}).items():
            if k in allowed:
                body[k] = v
        return body

    async def _post_chat(self, body: dict, model: str, free: bool, rest_budget_s: float | None, last_leg: bool) -> tuple[httpx.Response, int]:
        """_post, sent once more without the reasoning switch when the model refuses to have reasoning turned off."""
        try:
            return await self._post(body, free=free, rest_budget_s=rest_budget_s, last_leg=last_leg)
        except ApiError as err:
            if not (err.status == 400 and (body.get("reasoning") or {}).get("enabled") is False
                    and _REASONING_MANDATORY_400.search(err.body or "")):
                raise
            _REASONING_MANDATORY.add(model)  # see _REASONING_MANDATORY_400: never send the switch to it again
            body.pop("reasoning", None)
            return await self._post(body, free=free, rest_budget_s=rest_budget_s, last_leg=last_leg)

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        """Honour a Retry-After header when it is longer than our own backoff."""
        delay = self._backoff(attempt)
        try:
            return max(delay, float(retry_after)) if retry_after else delay
        except ValueError:
            return delay

    @staticmethod
    def _backoff(attempt: int) -> float:
        # Capped exponential with jitter, so 200 workers hitting one 429 do not retry in lockstep.
        return min(30.0, 0.5 * (2 ** (attempt - 1))) * (0.7 + 0.6 * random.random())


DeepSeekClient = ChatClient
