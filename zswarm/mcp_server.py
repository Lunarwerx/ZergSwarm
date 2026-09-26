"""MCP server (stdio) exposing the zswarm to an orchestrating Claude Code session.

Tools: zswarm_run, zswarm_status, zswarm_results, zswarm_apply_proposals (screen and apply a propose job's queued changes; zswarm/proposals.py), zswarm_cancel, zswarm_loop (the frontier loop, zswarm/loop.py), zswarm_jobs, zswarm_ask,
zswarm_cost, zswarm_savings, zswarm_usage, zswarm_doctor, zswarm_bench (the benchmark results DB, zswarm/benchdb.py),
zswarm_decide (typed decisions: TypeSafe Jev first, the tool-free default for what it is unsure of; zswarm/decisions.py),
zswarm_panel (a blind multi-model review with an anonymised rebuttal round; zswarm/panel.py),
zswarm_review and zswarm_doubt (evidence-gated review, refute pass and claim-blind doubt cycle; zswarm/review.py). Results are kept compact by default; fetch full answers or
transcripts with zswarm_results. Doctor and cost bodies live in health.py; result shaping in results.py;
the savings tracker in savings.py; the caller stamp (which session asked) in caller.py; the usage report in ledger.py.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import os
import traceback
from pathlib import Path
from typing import Any

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import archive, blobs, config, dispatch, health, mcp_policy, proposals, review, shared, utilization, verdict
from .caller import detect as detect_caller
from .jobs import JobManager
from .ledger import append_row, ask_row, usage_report
from .results import job_payload, results_from_disk
from .spec import EFFORTS, Task

mcp = FastMCP("zswarm", instructions=(
    "zswarm: fan out many cheap, autonomous worker tasks and read their results as data. "
    "Use zswarm_run for batches (each task gets a cwd, a prompt, a tool preset, optional JSON schema). "
    "Use zswarm_ask for a single tool-free question. "
    "All delegated subtasks belong here, including code, research, review and judgment. Desktop Opus 5.5 "
    "orchestrates and makes the final decision. AUTO selects the cheapest available evaluated configuration "
    "meeting the task profile, preserving its measured reasoning effort. Use zswarm_select to preview "
    "profiles and evidence without a model call. Exhaust suitable Swarm routes before a desktop exception. "
    "Backends: api (fastest, hundreds concurrent, sandboxed tools), "
    "cc (headless Claude Code with a compatible evaluated model, ~6 concurrent; read/none presets run on a "
    "Read/Grep/Glob allowlist, and edit/all bypass permissions only with confirm_write=true as well, or the task is refused). "
    "Verify anything load-bearing by opening the cited file:line yourself or re-running the quoted command, and drop a finding whose quoted line is not there; a second swarm pass is the same guess asked twice, not verification. Workers are cheap, judgment is not."
))

_manager: JobManager | None = None
_adopted = False


def manager() -> JobManager:
    global _manager, _adopted
    if _manager is None:
        _manager = JobManager()  # one manager per server process: jobs stay addressable for the session's lifetime
        _manager.port = shared.SERVING_PORT  # the shared server stamps its jobs with its port
    if not _adopted and _manager.port:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _manager  # asked from a worker thread: the first call on the loop carries the jobs on
        _adopted = True
        # A restart: the jobs the previous server on this port left unfinished carry on here, under their own ids,
        # before this first call looks any of them up.
        _manager.adopt_orphans()
    return _manager


def _parse_tasks(tasks: list[dict], defaults: dict) -> list[Task]:
    return [Task.from_dict({"prompt": t} if isinstance(t, str) else t, defaults, i) for i, t in enumerate(tasks)]


def _returns_errors(fn):
    """⛔ A TOOL THAT FAILS MUST SAY WHY (2026-09-21). An exception escaping a tool reaches the caller as the bare
    "Error executing tool <name>". Measured on Jacob's PC: zswarm_ask failed that way three times in a row while
    `zswarm.py ask` answered the same question in 0.24 s, and the caller sent its fact-check to a Sonnet agent
    instead. Return the exception as data - its type, message and the frames that raised it - so the next
    failure names its own cause. (Only on tools that return a dict: a list-typed tool must keep its shape.)"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - the whole point is to hand the failure back as data
            frames = traceback.extract_tb(e.__traceback__)[-3:]
            return {"error": f"{type(e).__name__}: {e}"[:600], "tool": fn.__name__,
                    "where": [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames]}

    return wrapper


def _served(fn):
    """Register a tool THROUGH the least-authority table (mcp_policy.py): its annotations are derived from its class,
    a tool with no class fails at import instead of being served unclassified, and a class this server does not grant
    (ZSWARM_MCP_CLASSES) is refused as data before the body runs, so nothing is spent or written."""
    name = fn.__name__
    list_typed = str(inspect.signature(fn).return_annotation).startswith("list")

    @functools.wraps(fn)
    async def gated(*args, **kwargs):
        why = mcp_policy.refusal(name)
        if why:
            refused = {"error": why, "tool": name}
            return [refused] if list_typed else refused
        config.refresh()  # a settings change (zswarm ui, a provider file) reaches this long-lived server on its next call
        return await fn(*args, **kwargs)

    mcp.tool(annotations=mcp_policy.annotations_for(name))(gated)
    return gated


@_served
@_returns_errors
async def zswarm_run(
    tasks: list[Any], backend: str = "api", model: str = "auto", cwd: str | None = None, tools: str = "read", system: str | None = None,
    max_turns: int | None = None, timeout_s: int = 600, schema: dict | None = None, concurrency: int | None = None, thinking: bool | None = None,
    reasoning_effort: str | None = None, max_cost_usd: float | None = None, budget_usd: float | None = None, label: str = "",
    wait: bool = True, wait_s: int = 240, max_answer_chars: int = 4000, role: str | None = None, lean: bool | None = None, profile: str | None = None,
    confirm_write: bool | None = None, isolated: bool | None = None, recipe: str | None = None,
    capability: str | dict | None = None, web_hosts: list[str] | None = None, verify: str | dict | None = None,
    resume_from_job: str | None = None, scope: str | None = None, escalate: str | None = None,
    done_when: str | None = None, envelope: dict | None = None, checkpoint_at: float | None = None,
    scripted: bool = False, redact: str | dict | None = None,
) -> dict:
    """Fan a batch of tasks out to swarm workers and return their results.

    tasks: list of {prompt, id?, cwd?, tools?, model?, role?, backend?, system?, max_turns?, schema?, files?, timeout_s?,
    thinking?, reasoning_effort?, max_cost_usd?, roots?, lean?, profile?, min_scores?, exclude_models?, result_file?, confirm_write?, isolated?, recipe?, shell_grants?, capability?, web_hosts?, verify?, green?, context_trigger?, context_clear_at_least?, inventory?, escalate?, writable?, done_when?, done_when_max_blocks?, checkpoint_at?, scripted?, redact?, acceptance?} or prompt strings.
    writable (api backend): path globs (relative to cwd or absolute; ** spans folders) that write_file/edit_file may change;
    everything else under the roots stays readable but frozen, so a worker told to make a test pass cannot edit the test.
    The bash tool is not path-guarded: pair writable with tools "edit" when the judge must not move.
    inventory: the paths a review task must cover; its result must carry `reviewed_paths` equal to them (zswarm adds
    the key to the schema and checks it; on the api backend each listed path that exists must also have been opened
    with read_file), else the task errors `IncompleteReview` instead of passing a partial review.
    context_trigger (default 60000 tokens, 0 = off): past it an api worker's older tool results (all but the last 3) go out as
    fetch_output pointers, at least context_clear_at_least (default 20000) tokens a pass. A capped tool output keeps its head
    and tail and names the fetch_output call that returns the middle; every worker with a sandbox tool gets fetch_output.
    green (per task): targeted_tests | package | workspace | merge_ready - the worker must end with a GREEN line naming the
    receipt of a passing bash run at that level (merge_ready also a base sha a git receipt printed); an ok result without
    it comes back in `unverified` with the `green.missing` list. Needs tools="all" (bash) on the api backend and no schema.
    shell_grants: command prefixes (["git add", "git commit"]) this task's bash may run although the shell policy
    (zswarm/shell_rules.toml) refuses them; never a ./ or absolute-path program, and never the rm -r whole-tree deny. api backend only (cc refuses them).
    runtime (a task key): where an api task's tools run - host (default) | docker-container:<id> | podman-container:<id> (a RUNNING
    container) | ssh:<target> (BatchMode). Non-host: cwd/roots are absolute POSIX paths there, every command runs under `timeout`.
    model: "auto" selects the cheapest available evaluated configuration meeting the task's published score floors.
    profile: routine (tool-free only), general, code, decision, research, critical. Roles/tools supply the default.
    Use zswarm_select to inspect the evidence and candidate ladder without spending on a model call.
    Exhausted credits, quota, or unavailable endpoints advance through capable Swarm configurations,
    including stronger models, within the original task budgets. Exact effort and completed tool results are preserved.
    A failed acceptance check stays visible: the desktop Opus 5.5 orchestrator raises requirements or excludes the failed model.
    All delegated work stays in Swarm; a desktop worker needs a documented capability/access exception.
    Explicit model names and machine-local role overrides remain available;
    role: search | code | judge | summarize | review | default, resolved to whatever model this machine wires for it (fails loudly when none).
    role "review" also brings a written rubric (problem/solution/ownership evidence, scope drift, test padding, rerun
    stability) and, when no schema is given, a verdict+findings schema; pair it with `inventory` for a coverage receipt.
    tools preset: read | edit | all | jobs | web | none | propose (or a comma list of read_file,list_dir,glob,grep,outline,unfold,write_file,edit_file,bash,read_url,propose).
    propose = read tools plus a `propose` tool that QUEUES file changes instead of making them (api backend only): use it for
    work over untrusted input, then zswarm_apply_proposals screens the queue and writes only what passes.
    jobs = all plus background shell jobs (bash_start, job_wait, job_tail, job_input, job_kill) for builds and servers.
    schema: JSON schema; the worker must call submit_result with it and the parsed object comes back in `data`. The WHOLE
    schema is enforced (nested required, minItems, types): say minItems/minProperties where an empty answer is wrong. An
    all-empty payload is pushed back once; a leg that keeps breaking the schema fails over (InvalidStructuredAnswer).
    timeout_s is RUN time: a task queued at a busy provider's gate spends none of it. `route_outlook` in the first
    response names a profile left with one provider, a resting pool, or a width past that provider's live-call cap.
    backend api = sandboxed tool loop; cc = headless Claude Code, evaluated routes with compatible endpoints and effort transport only.
    max_turns: default 24 for api, 40 for cc (a cc worker pays the repo's CLAUDE.md/rules orientation first).
    lean (cc only): skip the task folder's own CLAUDE.md, .claude/rules, hooks and settings (--setting-sources user);
    ~40% less context per turn on a big repo. Use it for fully-specified edits that need no house rules.
    result_file (per task, cc only): the ONE file the worker may write; hooks deny every other write, hand schema errors
    back after each write and block the first stop until the file validates; the parsed file comes back in `data`.
    cc permissions: read/none (and any list without a write tool) run on an allowlist (Read, Grep, Glob) that denies
    every other tool; edit/all bypass permissions and take BOTH opt-ins, the preset AND confirm_write=true, or the
    task is refused. isolated (cc only) is lean plus --strict-mcp-config: no MCP server either, not even the task
    folder's .mcp.json. Setting both lean and isolated is the same as isolated.
    capability: a least-privilege grant that REPLACES tools (api backend only): a name looked up in <cwd>/.zswarm/capabilities/
    then ~/.zswarm/capabilities/, a .json path, or an inline {identifier, permissions, allow?, deny?}. A permission is a
    preset, a tool, allow-<tool> / deny-<tool>, or {identifier: <tool>, allow: [globs], deny: [globs]}; deny beats allow.
    web = read plus read_url, a GET-only page read routed by host (HTML as markdown, a feed per entry, YouTube
    subtitles, a GitHub file raw), api backend only. It fetches only hosts in web_hosts ("example.com" covers its
    subdomains, "*" any public host) or allowed for good via zswarm_web; any other host is NOT fetched and comes
    back as summary.web_approvals (request_id, host, severity, tasks): re-run with the host in web_hosts to allow
    once, zswarm_web(allow=[host]) to allow always, or leave it out to deny. The admin block list beats both.
    verify (opt-in checked work): a shell command run in the task cwd after the worker finishes (exit 0 = pass), or
    {command?, judge?, retries?=2, timeout_s?=300} where judge is the acceptance criteria (true = "does what the task
    asks") scored PASS/FAIL by the judge role. A failed check re-runs the task with the failure appended, up to
    retries times, all inside max_cost_usd. The result's `verify` holds passed, exit, output_tail, verdict and every
    attempt's history; a task that never passed is status error "VerifyFailed:" with `verify.escalation`.
    acceptance (per task): typed criteria decided in code after the worker stops, never on its word and with nothing re-run:
    file:<path> (exists, non-empty), file_written:<path> (this worker's write/edit tool wrote it and it reads back),
    tests_passed:<command> (a bash receipt ran exactly that command - parsed, so an echo naming it, a `| tail` or `|| true`
    does not count - with exit 0 and a test summary; api backend only, UNVERIFIED on cc). Each result carries
    acceptance: [{criterion, verdict: holds|fails|UNVERIFIED, detail}] and summary.acceptance counts the verdicts.
    escalate: OFF by default. A stronger model (api backend) to re-run a task on ONCE when its worker gives up (FAILED, an
    empty or "could you clarify" / "I don't have a tool" reply, a run of tool errors) - never for a leg that could not serve.
    The failed trace goes to it as untrusted data; its answer is kept only if it does not give up too, and then a skill is
    banked under ~/.zswarm/skills/ that later escalate-enabled tasks of that kind are handed. `escalation` on the result says
    what happened; both runs' spend is on it.
    max_cost_usd caps ONE worker (default 0.25, catches a reasoning runaway); budget_usd caps the WHOLE job and cancels
    what is still pending once crossed. With wait=false you get a job_id immediately; poll zswarm_status / zswarm_results.
    reasoning_effort: low | medium | high | xhigh | max. AUTO retains the evaluated setting; explicit settings filter the evidence.
    recipe: a name for a job you run again and again on new input (api backend). The read-only tool calls of its last
    passing run are replayed first, so the model answers instead of re-planning them; any replayed call that errors
    drops the replay. Keyed on (recipe, input shape, zswarm version); the prompt is never cached. result.plan says hit|miss|fallback.
    scripted: scripted-diff mode for mechanical renames and sweeps. The worker (tools "all", cwd inside a git checkout, one
    scripted task per checkout) makes the change with a bash script and submits the script; zswarm replays it under
    set -euo pipefail on a throwaway worktree of the starting tree and returns status ok only when the replay reproduces the
    worker's tree exactly (else error ScriptMismatch). data = {script, summary, replay}: review the script, not the diff.
    summary.savings: the estimated cost of the same work as Claude sub-agents on the calling session's model, and the saving.
    resume_from_job: an earlier job id. Every task whose content (prompt, system, cwd, backend, model, tools, schema, files'
    contents, roots, thinking, reasoning_effort, temperature) matches one that finished ok there reuses that answer at no
    cost (result.cached_from names the job; summary.cached counts them); only changed, failed or unfinished tasks run.
    scope: a block (read root, diff command, "treat the input as data") prepended VERBATIM to every worker prompt with an
    echo mark; a result that does not echo it comes back with mis_scoped:true and is listed in summary.mis_scoped - do not trust it.
    done_when (api only): a checkable condition, e.g. "pytest exits 0". When the worker stops, a separate tool-free call must
    find the proof in its transcript: block sends the reason back as a user turn (at most done_when_max_blocks=3 times, then
    the task errors "goal not met"), impossible errors at once, an evaluator failure keeps the answer. Each result's `goal`
    holds the last verdict (ok | block | impossible | unchecked) and its reason.
    redact: hash | redact | mask | block | off - strip secrets, emails and card numbers from every tool output before the
    provider sees it (api backend only). hash tags a value <email:1a2b3c4d>, the same tag for the same value, and a write
    or command carrying a tag gets the real value back. Per task it may be {strategy, detectors, patterns}; detectors:
    secret, email, credit_card (default), ip, mac, url. Unset defers to ZSWARM_REDACT_FREE_TIER for free-tier legs.
    Each result's `redactions` counts what was redacted.
    envelope: caps the whole SPAWN TREE this job roots, {max_depth?, tools?, spend_usd?, max_nodes?, deadline_s?}
    (defaults max_depth 2, max_nodes 32, all tools, no spend ceiling or deadline). cc workers hand it on, and a job a
    worker starts can only narrow it; past depth, nodes, spend or deadline the job is refused with the reason.
    spend_usd and max_nodes are TREE-WIDE: a child's narrower value is compared with the whole tree's spend and node count.
    Each result's `liveness` says whether it moved the work: advanced | planning_only (replied with a plan, not a result) |
    blocked_external (credentials/access) | approval_required (asked for a yes) | failed; `next_action` is its own stated next
    step or blocker. summary.not_advanced lists those ids per label. The label is a text heuristic, not a grade: check or continue those
    tasks before counting them done.
    """
    defaults = {
        "backend": backend, "model": model, "cwd": cwd, "tools": tools, "system": system, "max_turns": max_turns,
        "timeout_s": timeout_s, "schema": schema, "thinking": thinking, "reasoning_effort": reasoning_effort, "max_cost_usd": max_cost_usd, "role": role, "lean": lean, "profile": profile,
        "confirm_write": confirm_write, "isolated": isolated,
        "recipe": recipe,
        "capability": capability,
        "web_hosts": web_hosts,
        "verify": verify,
        "scope": scope,
        "escalate": escalate,
        "done_when": done_when,
        "checkpoint_at": checkpoint_at,
        "scripted": scripted or None,
        "redact": redact,
    }
    # ⛔ A REFUSED TASK SPEC MUST SAY WHY (2026-09-19). Task validation raises ValueError with a precise
    # message ("reasoning_effort must be low|high|max"), and the MCP layer turned it into a bare
    # "Error executing tool zswarm_run" - measured: a caller passing reasoning_effort="medium" lost two
    # 10-task batches and had to bisect the arguments by hand. Return the message as data instead.
    try:
        parsed = _parse_tasks(tasks, {k: v for k, v in defaults.items() if v is not None})
    except ValueError as exc:
        return {"error": f"task spec refused: {exc}", "hint": f"reasoning_effort is one of {'|'.join(EFFORTS)}"}
    try:
        job = manager().submit(parsed, concurrency=concurrency, label=label, budget_usd=budget_usd, resume_from=resume_from_job, envelope=envelope)
    except ValueError as exc:
        if not str(exc).startswith(("spawn refused", "envelope:")):
            raise
        return {"error": str(exc)}  # a spawn-tree refusal is an answer to act on, not a crash
    # Said in the FIRST response, before any wait: a profile with one provider left whose pool is resting, or a
    # width past its live-call cap (job 20260925-143635-e3f6 learnt it after a ten-minute timeout).
    outlook = dispatch.route_outlook(parsed, manager()._gates)
    if wait:
        await manager().wait(job.id, wait_s)
    payload = job_payload(job, max_answer_chars, include_pending=True)
    if outlook:
        payload["route_outlook"] = outlook
    return payload


@_served
@_returns_errors
async def zswarm_status(job_id: str) -> dict:
    """Counts, cost and state of a job (running or finished). A running job also carries `pools`: each provider its
    tasks sit on, with the gate's live limit and how many keys are ready, and `waiting_on_rate_limited_pool` when
    every key there is resting - the state that used to read as a silent "running"."""
    try:
        return manager().status(manager().get(job_id))
    except KeyError:
        return JobManager.load_from_disk(job_id)["summary"]


@_served
@_returns_errors
async def zswarm_results(job_id: str, ids: list[str] | None = None, status: str | None = None, max_answer_chars: int = 20000, include_transcript: bool = False,
                         include_receipts: bool = False, taint: str | None = None) -> dict:
    """Results of a job. Filter by task ids or status (ok|error|timeout|loop|cancelled). include_transcript adds the worker's full message log per task.

    Every api result carries `citations`: its answer's [rN tool] citations checked against the receipt ledger of the
    tool calls it really made - verdict resolved | mismatched | unknown (a receipt id no call had) | uncited (an action
    claim with no receipt) | none. A task given `green` also carries `green`: verified, or unverified with `missing`.
    Top-level `unverified` lists the ok tasks whose done is not backed: re-check those FIRST. It certifies nothing else:
    `resolved` only proves a receipt of a fitting tool exists (any exit-0 bash backs "tests pass"), and `none` also
    covers file:line findings, so still verify anything load-bearing yourself. include_receipts adds the ledger.

    Every result also carries `taint`, sticky letters saying why to doubt it (empty = clean): F failed over to another leg,
    R re-run or re-prompted, S schema repaired, T output truncated, B turn budget forced the answer, C cost/context cap hit.
    taint="FT" keeps results carrying ANY of those letters; taint="clean" keeps only untainted ones."""
    try:
        out = job_payload(manager().get(job_id), max_answer_chars, ids=ids, status=status, include_receipts=include_receipts, taint=taint)
    except KeyError:
        out = results_from_disk(job_id, ids, status, max_answer_chars, include_receipts, taint)
    if include_transcript:
        # Long strings live once per job under blobs/; put them back so the caller sees the real messages,
        # whether the job is still a folder or has aged into its archive.
        for r in out["results"]:
            r["transcript"] = archive.transcript(job_id, r["id"])
    return out


@_served
@_returns_errors
async def zswarm_apply_proposals(job_id: str, ids: list[str] | None = None, apply: bool = True) -> dict:
    """Screen, then apply, the file changes a tools:"propose" job queued instead of making them.

    A propose-preset worker reads but cannot write: its only lever is the `propose` tool. Here each queued change
    is screened twice - rules first (guarded paths such as .git/.env/.secrets/CI, secret-shaped strings, too large
    to review), then one tool-free call per task on the `judge` role that sees the task as trusted text and the
    proposals as data - and only what passes is written, inside the task's own cwd and roots. A judge that fails
    or skips a proposal blocks it. apply=false screens only (a dry run). Applying twice re-runs what passed: an
    edit whose old_string is already gone comes back as an ERROR row and writes nothing."""
    doc = JobManager.load_from_disk(job_id)
    doc = {**doc, "tasks": blobs.expand_all_with(doc.get("tasks") or [], archive.blob_reader(job_id))}
    model = config.resolve_role("judge")

    async def ask(prompt: str, system: str, schema: dict):
        return await manager().ask_routed(prompt, model, system=system, schema=schema)

    out = await proposals.review_job(doc, ask, ids=ids, apply=apply)
    calls = out.pop("calls")
    return {"job_id": job_id, **out} | await _book_asks(calls, "screen")


@_served
@_returns_errors
async def zswarm_cancel(job_id: str) -> dict:
    """Cancel every pending/running task of a job, whichever process runs it: another live process is asked and stops
    within ~10 s; a job nothing runs any more (a killed `zswarm.py run`) is marked cancelled on disk."""
    try:
        return manager().cancel(job_id).summary()
    except KeyError:
        return JobManager.cancel_on_disk(job_id)


# Frontier loops started in this server process: loop id -> (the loop, its asyncio task). A loop outlives the tool
# call that started it, so a caller can come back for its status; its log file outlives the process.
_loops: dict[str, tuple[Any, asyncio.Task]] = {}


@_served
@_returns_errors
async def zswarm_loop(
    probe: str | None = None, cwd: str | None = None, loop_id: str | None = None, cancel: bool = False, log: str | None = None,
    max_rounds: int = 5, max_workers: int = 4, stall_rounds: int = 2, budget_usd: float | None = None, probe_timeout_s: float = 600.0,
    tools: str = "edit", model: str = "auto", backend: str = "api", max_turns: int = 24, fix_prompt: str | None = None, port_prompt: str | None = None,
    wait: bool = True, wait_s: int = 240,
) -> dict:
    """A FRONTIER LOOP: zswarm finds the next target itself, so you orchestrate without reading source.

    probe: a shell command run in cwd (absolute) that prints JSON {frontier, perStage}. frontier is the EARLIEST failing
    stage - a name, or {stage, mode?: fix|port, failures?: [...], detail?} - and null when everything passes. Each round
    zswarm runs the probe, picks FIX (stage fails) or PORT (perStage marks it missing/unported/todo, or mode says so),
    sends one worker per listed failure (at most max_workers) with your task defaults, waits, and probes again. It stops
    on frontier null (done), max_rounds, budget_usd, a probe that prints no JSON, or stall_rounds rounds that leave the
    frontier and its failures unchanged. Status and a step log go to a markdown file (log, default
    ~/.zswarm/loops/<id>.md); pointing log at an existing file resumes it. fix_prompt/port_prompt override the worker
    templates ({stage} {failure} {detail} {per_stage} {probe} {cwd}). Workers in one round share cwd, so keep
    max_workers at 1 when their fixes would touch the same files. Returns the status after wait_s (the loop keeps
    running); call again with loop_id for its status, or loop_id + cancel=true to stop it.
    """
    from . import loop as frontier

    if loop_id:
        if loop_id not in _loops:
            return {"error": f"no loop {loop_id!r} in this server process; its log file still holds its status", "loops": sorted(_loops)}
        lp, task = _loops[loop_id]
        if cancel and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif wait and not task.done():
            await asyncio.wait({task}, timeout=wait_s)
        return lp.status()
    if not probe or not cwd:
        return {"error": "give probe and an absolute cwd to start a loop, or loop_id to read one"}
    lp = frontier.build(probe, cwd, log, max_rounds=max_rounds, max_workers=max_workers, stall_rounds=stall_rounds, budget_usd=budget_usd,
                        probe_timeout_s=probe_timeout_s, task_defaults={"tools": tools, "model": model, "backend": backend, "max_turns": max_turns},
                        fix_prompt=fix_prompt or frontier.FIX_PROMPT, port_prompt=port_prompt or frontier.PORT_PROMPT)
    task = asyncio.create_task(lp.run(manager()), name=lp.id)
    _loops[lp.id] = (lp, task)
    if wait:
        await asyncio.wait({task}, timeout=wait_s)
    return lp.status()


@_served
async def zswarm_jobs(limit: int = 10) -> list[dict]:
    """Recent jobs, newest first (from disk, so it covers earlier sessions too)."""
    live = {j.id: j.summary() for j in manager().jobs.values()}
    seen: set[str] = set()
    out = []
    for s in sorted(list(live.values()) + JobManager.list_on_disk(limit), key=lambda s: s["job_id"], reverse=True):
        if s["job_id"] not in seen:
            seen.add(s["job_id"])
            out.append(s)
    return out[:limit]


@_served
@_returns_errors
async def zswarm_ask(prompt: str, system: str | None = None, model: str = "auto", schema: dict | None = None, thinking: bool | None = None, reasoning_effort: str | None = None, max_tokens: int = 16000, role: str | None = None, images: list[str] | None = None, profile: str | None = None, min_scores: dict | None = None, exclude_models: list[str] | None = None) -> dict:
    """One TOOL-FREE call, selected from published task scores and measured cost: classify,
    summarize, rewrite, second-opinion. With schema, `data` holds the parsed object. With `images`, only models
    that can see serve it; none available is an explicit error. AUTO preserves evaluated effort and escalates within Swarm.
    role "review" here picks the model only: its rubric, default schema and coverage receipt apply to zswarm_run."""
    from .selection import profile_for

    if role and config.ROLES.get(role.strip().lower()) != config.AUTO:
        model = config.resolve_role(role)
        profile = None
    elif not model or model.strip().lower() == config.AUTO:
        model = config.AUTO
        profile = profile or profile_for(role, "none")
    else:
        model = config.resolve_model(model)
        profile = None
    system = review.with_contract(role, system)  # judge / refute / doubt carry their evidence bar
    # The routed path, failing over to the next only when a path is unavailable (config.ROUTES, jobs.leg_unavailable).
    options = {"profile": profile, "min_scores": min_scores, "exclude_models": exclude_models or []} if profile else {}
    r = await manager().ask_routed(prompt, model, system=system, schema=schema, thinking=thinking, reasoning_effort=reasoning_effort, max_tokens=max_tokens, images=images or None, **options)
    return r.as_dict(brief=True) | await _book_asks([r], "ask")


@_served
@_returns_errors
async def zswarm_select(profile: str = "general", tools: str = "none", backend: str = "api", min_scores: dict | None = None, reasoning_effort: str | None = None, vision: bool = False) -> dict:
    """Preview the plan a task would run NOW, from published task scores, exact effort, cost and live key pools. No
    model call. Profiles: routine, general, code, decision, research, critical. CritPt is excluded from eligibility.
    `candidates` is what dispatch runs, in the order it would try them, each with the `load_bias` its provider's live
    load adds to its ranking cost (a resting pool only when no pool is live; a lower profile, named in `below_floor`,
    when the asked one has no key). `unavailable` is every evidenced route kept out, with why (all keys disabled,
    every key resting, no key here); `saturated` is a provider this server found answering nothing.
    Empty candidates is the answer to write `why-not-zswarm` from, without launching a job to find out.
    Set task.profile when dispatching; final acceptance remains with the desktop Opus 5.5 orchestrator."""
    from .dispatch import _plan, _pressure
    from .selection import rebias

    out = _plan(profile, tools=tools, backend=backend, min_scores=min_scores, reasoning_effort=reasoning_effort,
                vision=vision, min_context=0, explain=True)
    gates = manager()._gates
    out["candidates"] = rebias(out["candidates"], lambda p: _pressure(p, gates))  # the order dispatch applies
    if saturated := {p: why for p, gate in gates.items() if (why := gate.tripped())}:
        out["saturated"] = saturated
    return out


@_served
@_returns_errors
async def zswarm_review(cwd: str, diff: str | None = None, base: str = "HEAD", focus: str = "", findings: list[dict] | None = None,
                        refute: bool = True, nit_cap: int = 5, wait_s: int = 600, budget_usd: float | None = None) -> dict:
    """Review a change with an evidence bar, in up to two jobs: INVESTIGATE (a read-only worker under the judge
    contract returns findings, each with path, line, verbatim quote, severity important|nit|question, failure mode
    and confidence 1-10), a mechanical GATE (a quote not found in its cited file is capped at confidence 4 and an
    important one becomes a question; confidence 5+ shown, 3-4 in `appendix`, 1-2 suppressed; nits past nit_cap
    become `nits_omitted`; each finding tagged in_diff / off_diff by 3-gram overlap with the diff), then REFUTE (a
    `refute`-role worker tries to disprove each finding with cited path:line evidence; unsure means it survives,
    an off_diff finding must be confirmed, and a refute pass that fails keeps every finding).
    diff: the unified diff under review; omitted, `git diff <base>` in cwd is used (base HEAD = uncommitted work).
    findings: hand in findings you already have to skip INVESTIGATE; with refute=false no model is called at all.
    Returns findings (the survivors), refuted (each with its refutation), appendix, counts and the job ids."""
    if not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        return {"error": f"cwd must be an absolute directory, got {cwd!r}"}
    text = await review.diff_for(cwd, diff, base)
    if not text.strip() and findings is None:
        return {"error": f"nothing to review: no diff given and `git diff {base}` in {cwd} is empty"}
    return await review.run_review(manager(), cwd, text, focus=focus, findings=findings, refute=refute, nit_cap=nit_cap,
                                   wait_s=wait_s, budget_usd=budget_usd)


@_served
@_returns_errors
async def zswarm_doubt(artifact: str, contract: str, history: list[list[dict]] | None = None) -> dict:
    """One doubt cycle: a fresh, tool-free reviewer on the `doubt` role (wire a non-Claude model there
    for a second opinion from another family; nothing enforces it) is handed ONLY the
    artifact and the contract it must meet - there is deliberately no parameter for your claim or conclusion,
    which biases a reviewer toward agreeing. Issues come back in fixed precedence: contract_misread, actionable,
    tradeoff, noise. history: the `issues` lists of earlier cycles on the same artifact, oldest first. `stop` is
    true after 3 cycles, when this cycle found nothing substantive, or on `doubt_theater` (two or more cycles with
    substantive issues and none actionable: stop doubting and decide)."""
    payload, r = await review.run_doubt(manager(), artifact, contract, history)
    return payload | await _book_asks([r], "ask")


async def _book_asks(results: list, kind: str, extra: tuple = ()) -> dict:
    """Attribute and cost finished asks (ledger line + the savings running total). ⛔ Bookkeeping NEVER fails the
    answer it records (2026-09-21): the CLI `ask` does none of this and answered on the machine where the MCP tool
    kept failing, so a caller-stamp, ledger or savings-database fault must come back as a note beside the answer,
    not replace it.

    `extra` is (Result, ledger fields) pairs for spend that is not one of `results` (zswarm_decide's Jev calls). Each
    gets a ledger line AND its own utilization row: the ledger line has no `id`, so handing it to utilization.record
    failed every decide with `KeyError: 'id'` (3,070 times, 2026-09-21 to 2026-09-24) and no Jev spend was saved."""
    try:
        caller = detect_caller(kind)
        for r, fields in extra:
            append_row(ask_row(r, caller) | fields)
            await asyncio.to_thread(utilization.record, utilization.ask_row(r, caller) | {"label": fields.get("job") or kind})
        saved = None
        for r in results:
            append_row(ask_row(r, caller) | ({"job": kind} if kind != "ask" else {}))
            saved = utilization.compact(await asyncio.to_thread(utilization.record_ask, r, caller))
        return {"savings": saved} if kind == "ask" else {}
    except Exception as e:  # noqa: BLE001 - see the docstring
        return {"bookkeeping_error": f"{type(e).__name__}: {e}"[:300]}


@_served
@_returns_errors
async def zswarm_decide(items: list[dict], escalate_below: float = 0.7, fallback_model: str = "auto", batch: int = 1, model: str = "jev-latest") -> dict:
    """TYPED decisions over many items in one call, fast: classify, route, yes/no, grade on a scale.

    Each item: {id?, state (text or any JSON), question, type: choice | yesno | score, options}. options is a list of
    keys or a {key: description} map for choice (2-255), an ordered list of 2-10 level descriptions for score, and
    optional {yes: ..., no: ...} descriptions for yesno. Put the facts the decision needs in state, nothing else.
    TypeSafe's Jev answers every item first (~0.15 s and ~$0.00004 each, with a calibrated confidence); an answer
    under escalate_below confidence is re-asked through the published decision capability profile.
    If no valid stronger answer is obtained, the decision remains unanswered with an explicit error.
    The desktop orchestrator retains final authority. escalate_below=0 trusts Jev on
    everything, 1.01 sends everything to the fallback. batch packs up to 5 items per Jev call (more hurt accuracy).
    NOT for arithmetic, counting, dates or writing text (Jev is weak there by design): use zswarm_ask for those.
    model 'featherless-ai/<Model>-classifier' sends the typed leg to Featherless's keyless Simple Jev demo instead:
    for re-tests only, it measured no better than Jev and worse calibrated (docs/BENCH-2026-09-24-simple-jev.md).
    Returns answers [{id, answer, source, jev: {answer, confidence, probabilities}, fallback?}] plus a cost summary.
    """
    from .decisions import decide
    from .spec import Result, now_iso

    out = await decide(items, manager(), escalate_below=escalate_below, fallback_model=fallback_model, model=model, batch=batch)
    stats = out.pop("_jev_stats")
    fallbacks = out.pop("_fallback_results")
    jev: tuple = ()
    if stats.get("calls"):
        # The Jev part is one ledger line (all its calls), costed like an ask; the caller stamp is added in _book_asks.
        jr = Result(id="decide", status="ok" if not stats.get("errors") else "error", backend="typesafe", model=stats.get("model") or model,
                    usage={"in_hit": 0, "in_miss": stats.get("in", 0), "out": stats.get("out", 0), "reasoning": 0}, cost_usd=stats.get("cost_usd", 0.0),
                    seconds=round(stats.get("secs", 0.0), 3), turns=stats["calls"], finished=now_iso())
        jev = ((jr, {"provider": "typesafe", "job": "decide"}),)
    return out | await _book_asks(fallbacks, "decide", jev)


@_served
@_returns_errors
async def zswarm_panel(prompt: str, models: list[str] | None = None, system: str | None = None, max_findings: int = 8, reasoning_effort: str | None = "low", seed: int | None = None) -> dict:
    """A BLIND PANEL review: one prompt to 2-5 models at once, then an anonymised rebuttal round (zswarm/panel.py).

    Round 1 sends the same prompt to every panelist in parallel, so no answer anchors another; each lists numbered
    findings (at most max_findings). Round 2 shows each panelist the others' findings as Reviewer A/B/C, the letters
    shuffled afresh per panelist, and reads UPHOLD/REJECT <ref>, CONCEDE <own ref> and MISSED lines. models: model
    names, aliases or roles (default: the tool-free and tool-using defaults, or `panel` in ~/.zswarm/settings.toml).
    Returns findings grouped with who upheld/rejected each and a status (contested first, then upheld, unreviewed,
    conceded), `contested` (ids to read first: someone rejects it while someone stands behind it), `missed`, the
    per-panelist board and a cost summary. Tool-free: give it the text to review in the prompt."""
    from .panel import panel

    if reasoning_effort is not None and reasoning_effort not in EFFORTS:
        return {"error": f"reasoning_effort must be one of {'|'.join(EFFORTS)}"}
    out = await panel(prompt, manager(), models=models, system=system, max_findings=max_findings, reasoning_effort=reasoning_effort, seed=seed)
    return out | await _book_asks(out.pop("_results"), "panel")


@_served
@_returns_errors
async def zswarm_cost(days: float = 1.0, balance: bool = True) -> dict:
    """Spend from the local ledger over the last N days, current peak/off-peak rate, next rate change, and the account balance.
    by_model[m].survival, where present: how much of what that model's api workers wrote was still in the file later."""
    return await health.cost(manager(), days, balance)


@_served
@_returns_errors
async def zswarm_savings(days: int = 14, include_today: bool = False) -> dict:
    """What the zswarm saved. `running_total`: every utilization (each zswarm_run job and zswarm_ask, all machines)
    with the DeepSeek cost, the estimated cost of the same work as Claude sub-agents on the caller's model at the
    time, and the saving, plus the fleet total and the last 10. Then per local day on this machine: DeepSeek spend,
    Claude usage (API list-price equivalent), the sub-agent cost avoided as a low-high range, and a 30-day
    projection. include_today scans today's Claude transcripts live, which takes a while."""
    from . import savings

    return await asyncio.to_thread(savings.report, days, include_today)


@_served
@_returns_errors
async def zswarm_sync(push: bool = True, restore: bool = False) -> dict:
    """Sync this machine's utilization ledger with the fleet: pull the other machines' shards from this repo's
    `sync` branch, import them, export ours, regenerate TOTALS.md (the running total), commit and push.
    restore=true first reads THIS machine's own shard back, rebuilding the ledger after a lost ~/.zswarm."""
    out = await asyncio.to_thread(utilization.restore) if restore else {}
    return {**({"restore": out} if restore else {}), **await asyncio.to_thread(utilization.sync, push)}


@_served
@_returns_errors
async def zswarm_usage(hours: float = 24.0) -> dict:
    """WHO used the swarm in the last N hours (per calling account / session / folder: jobs, tasks, ok/error, cost, labels),
    plus every Claude sub-agent decision the routing gate logged, with the fan-outs that matched the mechanical rule but went to Claude anyway."""
    return usage_report(hours)


@_served
@_returns_errors
async def zswarm_doctor() -> dict:
    """Health: key present (never the value), models reachable, balance, claude/rg/bash binaries, home dir, rate window."""
    return await health.doctor(manager())


@_served
@_returns_errors
async def zswarm_web(allow: list[str] | None = None, block: list[str] | None = None, forget: list[str] | None = None) -> dict:
    """read_url's standing host policy (the web preset), and which backend serves each channel right now.

    allow: hosts every batch may fetch from now on - the allow-always answer to a summary.web_approvals entry
    (allow-once is web_hosts on the next zswarm_run). block: the admin block list; a blocked host is never
    fetched, whatever a batch lists. forget: take hosts off both lists. With no arguments this only reports.
    """
    from . import web

    if allow or block or forget:
        await asyncio.to_thread(web.save_policy, allow, block, forget)
    return web.report()


@_served
@_returns_errors
async def zswarm_keys(action: str = "list", fingerprint: str | None = None, provider: str | None = None, all_keys: bool = False, reason: str = "disabled by hand", verbose: bool = False) -> dict:
    """The API-key pools (DeepSeek, OpenRouter, ...) and the DISABLED slot. Keys are never returned, only 8-character fingerprints.

    A key that runs out of credit (a 402), or that has been revoked past its strikes, goes to the disabled slot and is
    NEVER handed to a worker again - no timer, no retry ladder - so a spent key costs nothing to route around.
    action: list (offline, default) | probe (one FREE GET per key; a topped-up key comes back out of the slot here)
    | enable (take one out by fingerprint, or all_keys=true for the lot) | disable (put one in by hand).
    A key disabled for a 402 keeps serving ':free' models while its free-tier allowance lasts.
    list is BOUNDED by default - every count, plus only the keys needing attention (at most 40 per provider);
    verbose=true returns every row (1,700+ keys is ~900k characters, more than a client will show).
    """
    from . import keys as keymod

    if action == "list":
        return keymod.report(provider, verbose=verbose)
    if action == "probe":
        return await keymod.probe(provider)
    if action in ("enable", "disable"):
        if not fingerprint and not (action == "enable" and all_keys):
            return {"error": "give a fingerprint (zswarm_keys action='list' prints them), or all_keys=true with enable"}
        return await asyncio.to_thread(keymod.set_enabled, fingerprint, action == "enable", provider, all_keys, reason)
    return {"error": f"unknown action {action!r}; use list | probe | enable | disable"}


@_served
@_returns_errors
async def zswarm_bench(suite: str | None = None, arm: str | None = None, all_versions: bool = False, include_legacy: bool = True, limit: int = 120) -> dict:
    """The bench RESULTS DATABASE: what every model/arm scored on every benchmark suite, with cost per item and latency.

    CHECK THIS BEFORE YOU BENCHMARK OR A/B TEST ANY MODEL. If the question is answered here, re-measuring it wastes
    usage (owner directive, 2026-09-21). Rows are committed in the zswarm repo (bench/results/*.jsonl), so every
    machine sees every other machine's runs; the harnesses (bench/decide.py, bench/hard_reasoning.py, `zswarm bench`)
    write every answer as it lands and skip what is already measured for the same suite version.
    suite: e.g. 'decisions.triage', 'bench.judgment', 'hard_reasoning' (substring match); arm: substring of the arm label.
    Newest suite version only unless all_versions; source 'doc' rows are aggregates transcribed from docs/BENCH-*.md.
    """
    from . import benchdb

    lines = await asyncio.to_thread(benchdb.board, None, include_legacy, all_versions)
    if suite:
        lines = [x for x in lines if suite in (x["suite"] or "")]
    if arm:
        lines = [x for x in lines if arm in (x["arm"] or "")]
    return {"rows": lines[:limit], "total": len(lines), "suites": sorted({x["suite"] for x in lines}),
            "how": "rate = passed/graded; ungraded rows (limits, errors) never count as fails; skipped = known-gap tasks the arm "
                   "was not run on, so its rate covers fewer items than its peers'; zswarm benchdb board prints the same table"}


@_served
@_returns_errors
async def zswarm_models(refresh: str | None = None, grep: str | None = None, limit: int = 60) -> dict:
    """Every model this machine can address, with its price per 1M tokens ('-' when none is on record).

    OpenRouter models are addressed as 'or:<its id>' (e.g. 'or:deepseek/deepseek-chat-v3.1', 'or:z-ai/glm-5.3-flash')
    with no registration step. refresh='openrouter' pulls its live catalogue (~440 models) and their current rates, so
    their cost stops reading '-'; OpenRouter also reports what it actually charged on every call, and that number wins.
    """
    from . import catalogue

    if refresh:
        return await catalogue.refresh(refresh)
    return await asyncio.to_thread(catalogue.listing, grep, limit)


def main() -> None:
    config.ensure_dirs()
    # Opt-in session recording (zswarm/replay.py): with ZSWARM_RECORD set this process only relays stdio to a
    # child server and writes every JSON-RPC line down, so a crash deep in a long session can be replayed.
    # 0/false/off/no mean "off", not a folder named ./0/ under the server's cwd.
    target = os.environ.get("ZSWARM_RECORD", "").strip()
    if target.lower() not in ("", "0", "false", "off", "no"):
        from . import replay

        raise SystemExit(replay.record(replay.server_command(), replay.record_path(target)))
    verdict.write()  # offline; the routing gate reads it (verdict.py)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
