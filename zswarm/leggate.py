"""How many calls one provider's key pool gets at once: a ramped, capped gate shared by every job's tasks on it.

Job 20260924-152821-54f1 put 64 tasks (the api default) onto the free Gemini pool in the same second; 60 of
the 61 that finished timed out at 600 s, most without one reply. A backend semaphore sized for "hundreds of
HTTP calls" says nothing about what a free-tier key pool can serve, so the gate sits per PROVIDER, in front of
each leg's run:

- it opens at config.LEG_RAMP_START live tasks and grows by one per successful reply (additive increase),
- it halves when a 429 finds every key in the pool resting (multiplicative decrease),
- it never exceeds the pool's usable keys times its per-key allowance (config.LIVE_PER_KEY, or a provider's
  own `live_per_key`), re-read every time a task asks, so a key disabled mid-job shrinks it.

A task waiting here has not started its own clock (Task.timeout_s wraps the run, not the queue), which is
the point: queued work waits its turn instead of timing out against a pool that could never have served it.

The gate also remembers a TRIP: a last-leg call that waited out config.SATURATED_REST_S while nothing this
process sent to the provider was answered (`served` did not move) found the pool saturated from outside the
job, so the next tasks fail at once instead of each waiting the same two minutes (dispatch.run_selected).
"""
from __future__ import annotations

import asyncio
import time


class LegGate:
    def __init__(self, provider: str, cap: int, start: int):
        self.provider = provider
        self.cap = max(1, int(cap))
        self.limit = max(1, min(int(start), self.cap))
        self.live = 0
        self.waiting = 0
        self.saturations = 0  # times a 429 found every key resting and the limit was halved
        self.served = 0  # replies this process got from the provider: the proof that the pool is serving anyone
        self._trip: tuple[float, str] | None = None  # (monotonic end, why) while the pool is known saturated
        self._cond: asyncio.Condition | None = None

    def _condition(self) -> asyncio.Condition:
        # Built on first use so the gate binds to the loop that uses it (tests run one loop per asyncio.run).
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def acquire(self) -> None:
        cond = self._condition()
        self.waiting += 1
        try:
            async with cond:
                await cond.wait_for(lambda: self.live < self.limit)
                self.live += 1
        finally:
            self.waiting -= 1

    async def release(self) -> None:
        cond = self._condition()
        async with cond:
            self.live = max(0, self.live - 1)
            cond.notify_all()

    async def __aenter__(self) -> "LegGate":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()

    def _wake(self) -> None:
        cond = self._cond
        if cond is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def notify() -> None:
            async with cond:
                cond.notify_all()

        loop.create_task(notify())

    def ok(self) -> None:
        """A reply came back: one more live call is allowed, up to the cap, and any trip is over."""
        self.served += 1
        self._trip = None
        if self.limit < self.cap:
            self.limit += 1
            self._wake()

    def saturated(self) -> None:
        """A 429 found every key resting: halve the live calls allowed (never below one)."""
        self.saturations += 1
        self.limit = max(1, self.limit // 2)

    def set_cap(self, cap: int) -> None:
        cap = max(1, int(cap))
        grew = cap > self.cap
        self.cap = cap
        self.limit = min(self.limit, cap)
        if grew:
            self._wake()

    def trip(self, why: str, seconds: float) -> None:
        self._trip = (time.monotonic() + seconds, why)

    def tripped(self) -> str | None:
        """Why the provider is known saturated right now, or None once the trip has run out (the next task probes)."""
        if self._trip is None or time.monotonic() >= self._trip[0]:
            self._trip = None
            return None
        return self._trip[1]

    def snapshot(self) -> dict:
        snap = {"provider": self.provider, "limit": self.limit, "cap": self.cap, "live": self.live, "waiting": self.waiting,
                "saturations": self.saturations, "served": self.served}
        if why := self.tripped():
            snap["tripped"] = why
        return snap
