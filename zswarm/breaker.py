"""Per-leg circuit breaker: a leg whose host keeps erroring or timing out is tried LAST for a while.

Failover (jobs.leg_unavailable) already moves a task off a dead leg - but only after that task has paid
for finding out: a stalled read costs READ_TIMEOUT_S twice, a SlowLeg three slow turns. At 64 concurrent
workers every one of them rediscovered the same outage before it fell back, and so did every later job.
The breaker remembers: after THRESHOLD consecutive host failures (a 5xx, a stall, a connection that never
formed, a SlowLeg) the leg is OPEN for OPEN_S, and routed tasks and asks get it moved to the back of their
plan, so they go straight to the fallback leg. It is never dropped: when every other leg is also down the
open leg is still the last resort. Once OPEN_S has passed it is HALF-OPEN: exactly one task gets it in its
usual place as the probe; a probe that is served closes the breaker, one that fails re-opens it.

Only host failures count. A 402 or a spent key pool is the key pool's business (keys.py rests and disables
keys), a 413 is the request's size, and a worker's FAILED or a 400 means the leg served and the work failed,
which closes the breaker like any answer. Process-wide and in memory, like jobs._TRIPS: a restart forgets.
The idea follows Shopify Semian's error threshold / error timeout / half-open timeout (ideas only).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .spec import Result

THRESHOLD = 3  # consecutive host failures that open a leg's breaker
OPEN_S = 120.0  # how long an open leg is tried last before one task probes it again

# A host failure: any 5xx (the client already retried it), a stalled read (raised as a 504 "stalled:"),
# a connection that never formed or broke mid-reply, or a leg answering too slowly to finish (SlowLeg).
# `API 503` is this client's rendering of a status; `API Error: 503` is Claude Code's, from a cc worker.
_HOST_FAILURE = re.compile(
    r"API (?:Error:? )?5\d\d\b|stalled:|ConnectError|ConnectTimeout|ReadTimeout|RemoteProtocolError|PoolTimeout|^SlowLeg:",
    re.I,
)


@dataclass
class _Leg:
    fails: int = 0  # consecutive host failures
    opened: float = 0.0  # time.monotonic() the breaker opened; 0 while closed
    probe: float = 0.0  # time.monotonic() a half-open probe was handed out; 0 when none is out


_LEGS: dict[str, _Leg] = {}


def host_failure(res: Result) -> bool:
    return res.status == "error" and bool(_HOST_FAILURE.search(res.error or ""))


def state(leg: str, now: float | None = None) -> str:
    """The breaker on `leg`: "closed", "open" (tried last) or "half-open" (due a probe)."""
    b = _LEGS.get(leg)
    if b is None or not b.opened:
        return "closed"
    now = time.monotonic() if now is None else now
    return "open" if now - b.opened < OPEN_S else "half-open"


def order(legs: list[str]) -> list[str]:
    """`legs` with every open leg moved to the back, keeping each group's order. A half-open leg keeps its place
    for one task, the probe; the rest treat it as open until the probe comes back (or OPEN_S passes without one)."""
    front, back = split(legs)
    return front + back


def split(legs: list[str]) -> tuple[list[str], list[str]]:
    """order()'s two groups: the legs to try in their usual order, and the demoted (open) legs behind them. A caller
    that times its legs by position needs the split: the route's real last leg is the last of `front`, not of the list."""
    now = time.monotonic()
    front, back = [], []
    for leg in legs:
        s = state(leg, now)
        if s == "half-open":
            b = _LEGS[leg]
            if not b.probe or now - b.probe >= OPEN_S:
                b.probe = now
                front.append(leg)
                continue
        (back if s != "closed" else front).append(leg)
    return front, back


def record(leg: str, res: Result, unavailable: bool) -> None:
    """Count what one call on `leg` came back with. `unavailable` is jobs.leg_unavailable(res): a result that is
    unavailable but no host failure (a spent key pool, a 413) neither opens nor closes the breaker. Nor does a task
    that ran out of its own time ("timeout") or was cancelled: that says nothing about whether the host answers."""
    b = _LEGS.setdefault(leg, _Leg())
    if host_failure(res):
        b.fails += 1
        if b.fails >= THRESHOLD:
            b.opened, b.probe = time.monotonic(), 0.0  # open, or re-open after a failed probe
    elif res.status == "ok" or (res.status == "error" and not unavailable):
        _LEGS.pop(leg, None)  # the leg served (an answer, a worker's FAILED, a 400): closed, count reset
    else:
        b.probe = 0.0  # the probe learned nothing about the host: the next task may probe


def report() -> dict:
    """The breakers that are not closed, for `doctor`: leg -> state, consecutive failures, seconds until a probe."""
    now = time.monotonic()
    return {leg: {"state": state(leg, now), "fails": b.fails, "probe_in_s": max(0, round(b.opened + OPEN_S - now))}
            for leg, b in _LEGS.items() if b.opened}
