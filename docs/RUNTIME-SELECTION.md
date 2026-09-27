# Published evidence in runtime selection

`model: "auto"` uses `zswarm.selection.plan`, shared by batch workers, MCP/CLI asks,
and JEV's generative escalation. `zswarm_select` exposes the same plan without a paid call.
`zswarm/data/published-models.json` ships the dated configuration evidence; each evaluated model is a
`[models."rank:..."]` table in its provider's file (`zswarm/providers/`) with its `benchmark_slug`, exact API id,
supported effort, price and capabilities, verified against the public provider catalogue. A user file that changes
one's identity takes it out of AUTO: unknown model identities never inherit scores.

The following minimums are **operational policy**, not measured probabilities of completing a task.
Fraction-valued scores below use 0–1 units. CritPt is excluded while under review.

| Profile | Required scores |
| --- | --- |
| routine | HLE ≥ .19; tool-free only |
| general | HLE ≥ .30; long-context ≥ .75 |
| code | Terminal-Bench ≥ .30; SciCode ≥ .50 |
| decision | GDPval-AA ≥ 1400 Elo; AutomationBench ≥ .60 |
| research | HLE ≥ .45; long-context ≥ .80 |
| critical | Terminal-Bench ≥ .50; HLE ≥ .50; GDPval-AA ≥ 1700 Elo |

Use `min_scores` to raise requirements and `exclude_models` to remove a failed configuration
by registry name, benchmark slug or API ID. No individual failure restarts completed sibling tasks.
Explicit effort filters configurations instead of assigning max-effort scores to a low-effort call.
An explicit model pin takes precedence over a profile; omit the model to use automatic selection.
Local overrides changing a model or effort cannot inherit the original configuration's benchmark scores.
`thinking: false` with AUTO has no matching evidence and fails clearly; a deliberately unranked
call can name its model explicitly. API price fields and benchmark workload costs are separate:
the latter order candidates, with a half-rate estimate for direct DeepSeek off-peak. Actual
production cost and latency still come from the serving provider, not the benchmark estimate.

```json
{
  "tasks": [{
    "prompt": "Review the specified change and verify every claimed bug against the code.",
    "profile": "critical",
    "tools": "read",
    "cwd": "D:/project",
    "max_cost_usd": 0.50
  }]
}
```

Key availability, cooldowns, context, tools, vision, backend and exact effort constrain every plan.
Unavailable providers advance to another eligible Swarm configuration under the original task
budgets. API handoffs preserve completed tool calls/results; CC handoffs carry the prior report
and explicitly inspect existing work. Semantic failures remain errors for the orchestrator to
verify and retry with stronger requirements, avoiding unverified replay of edits. JEV never
silently promotes a low-confidence answer when its escalation fails.
A task's `timeout_s` is RUN time: provider preflight and gate waits are queue time and spend none of it
(job 20260925-143635-e3f6 lost 23 of 23 tasks to a queue that ate the deadline). A leg whose keys another
task has already found disabled is skipped in zero seconds. A LAST leg that ends `PoolSaturated` goes back
through its provider's gate (halved by the 429s) with its transcript kept, up to `SATURATED_REQUEUES`
times, instead of failing. A `submit_result` that breaks the task's schema (nested `required`, `minItems`,
types) is refused back to the model; a leg that keeps breaking it fails over as `InvalidStructuredAnswer`.
`zswarm_run`'s first response carries `route_outlook` when a profile is down to one provider, its pool is
resting, or the width passes its live-call cap. `bench/structured_width.py` is the live check for both
(width 24 on the only route; a schema'd translation on every route). An attempt with unknown billing stops
automatic paid retries. Cost limits are checked between responses; an in-flight response can exceed
the remaining estimate, and concurrent workers can overshoot a batch limit before cancellation.

Desktop Opus 5.5 plans, verifies and makes final decisions. JEV/Dredd classifies the delegation
profile and provides the `zswarm_run` instruction. Desktop workers require a recorded
`why-not-zswarm` access/capability exception after suitable Swarm routes cannot serve.
The complete plan and attempted models are recorded in results; the job ledger carries profile,
benchmark identity and actual effort. The model selected in the desktop app is not changed by this code.

## Every pick uses your keys

Before 2026-09-26, roles and the panel were pinned to fixed models, so a machine with only a Groq key
had its judge, doubt and panel pointing at OpenRouter and DeepSeek models it could not call.
Picks that are not part of a batch task now come from `dispatch.first_choice`: the head of the
key-aware plan, the pools this machine has keys for, stepping down a profile the way a task does,
else the evidence's own head. It serves the AUTO roles, the auto model and the doctor's `routes_now`;
role asks (the verify judge, the proposal judge, doubt) run an AUTO role through
`dispatch.ask_selected` with failover across every evaluated route with a key. The blind panel
defaults to the first two tool-free general picks this machine can serve, one per model maker.

## Why a model was skipped, and how load spreads a batch

Every plan (`zswarm_select`, and `selection` on each result) carries `rejected`: each evaluated route
that is not a candidate, with the first filter it failed, in this order: `registry`, `evidence`,
`excluded`, `identity`, `floor` (names the scores below the floor), `effort`, `thinking`, `tools`,
`vision`, `context`, `backend`, `usable` (no key ready). A task result as the orchestrator reads it
(`zswarm_results`, `zswarm_ask`) carries the compact form `{filter: [models]}`, unless it failed
`NoCapableSwarmRoute`; `results.jsonl` and `zswarm_select` keep every reason.

Candidates are then ranked with a load bias. A provider's pressure (tasks this process has committed
to it, plus its `SlowLeg`/`PoolSaturated` failures in the last 10 minutes, over the provider's gate width
(usable keys times `LIVE_PER_KEY` until a gate exists; the gate ramps up and halves on 429s), capped at 1) inflates the benchmark cost used for ranking by up to `load_bias`
(default 0.5). A batch therefore spreads over legs within about 50% of each other's cost before the
cheapest one saturates. It never reaches past a candidate that costs much more. The bias
changes order only: floors still filter, and `benchmark_cost_usd` stays unbiased on the candidate and
in the ledger row, beside the `load_bias` it was ranked with. Set `load_bias = 0` in
`~/.zswarm/settings.toml` (or `ZSWARM_LOAD_BIAS=0`) to rank on cost alone.

Two keys come before cost (2026-09-27). **Free first:** a provider whose file says `free_calls = true` (NVIDIA's
trial keys) serves before any paid route that meets the same floors; inside the free group and inside the paid group
the cheapest capable model still goes first, so a free route never means a bigger model than the task needs.
**Crawling last:** per MODEL, not per provider (NVIDIA queues each model on its own), a model whose last call ran
slower than `SLOW_LEG_TURN_S` a turn, or timed out, is tried after the capable models without that mark, still
inside its free or paid group, until the mark ages out (`SLOW_MARK_S`) or one fast call clears it. The full order is
star (`priority`), free before paid, not crawling before crawling, then biased cost and score.

**Moving off a crawling model, mid-task (2026-09-27).** A running task leaves a slow leg for its next one with its
transcript kept, so the next model continues the same conversation and never repeats a finished tool call:

- after `SLOW_LEG_MIN_TURNS` turns averaging over `SLOW_LEG_TURN_S`, or after ONE such turn when the model is
  already marked crawling (another task saw it crawl), so a job's tasks on a crawling model move together;
- when one call has not answered in `SLOW_LEG_CALL_S` (150 s): the call is cut and the next leg sends that turn again;
- when a leg ends with an empty answer after the worker's own nudges, and it changed no file.

The handoff carries only what every chat API accepts (role, content, tool calls and their results), since one host's
extra fields can be another's 400 (NVIDIA's `refusal`, which groq refuses). A pinned model's route (one model on
several hosts) hands over the same way; until 1.2.4 its next leg began again from the prompt.

Only a leg with somewhere to go is timed. So an evaluated task's route ends with **rescue legs**: the routes of the
next profile down (`code` to `general`, as the out-of-keys step-down does), which a task reaches only when its own
profile's routes crawl or fail. A rescue leg that is not crawling goes ahead of the task's own crawling legs, and a
result a rescue leg served carries `selection.below_floor` and a warning to verify it. The crawl marks are shared by
every zswarm process on the machine through `~/.zswarm/crawl.json`, newest reading wins, so a `zswarm run` job sees
what the MCP server saw.

Refresh the two packaged JSON files together when evidence or API identities change, then run the
selection/dispatch regression tests. A missing eligible route is an explicit error, never a reason
to invent a model mapping, lower the capability requirement, or claim that the whole Swarm is dead.
