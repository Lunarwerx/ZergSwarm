"""`cc` backend: a headless Claude Code process pointed at DeepSeek's Anthropic-compatible
endpoint (or Hugging Face's / OpenRouter's, when the task's route fails over to them, or a loopback
facade in front of an OpenAI-only provider such as gemini or groq: anthropic_facade.py). Full
Claude Code tooling (Read/Edit/Bash/Grep/Glob, project CLAUDE.md, hooks in the project) with
DeepSeek flash doing the thinking. Costs are computed from the serving path's rates, never from
the `total_cost_usd` Claude Code prints (it assumes Anthropic prices for an unrecognised model
and is ~1000x too high). Binary and environment: claude_env.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from . import config, result_hooks
from .acceptance import decide as decide_acceptance, prompt_clause
from .anthropic_facade import anthropic_endpoint
from .claude_env import cc_env, cc_model_id, claude_argv, claude_bin, ensure_cc_config  # noqa: F401 - claude_bin, ensure_cc_config re-exported for the installer
from .code_brief import system_for
from .procs import TIMEOUT_EXIT, run_hidden
from .spec import CC_WRITE_TIERS, Result, Task, cc_tier, inline_files, now_iso

READ_ONLY_DISALLOWED = "Edit,Write,MultiEdit,NotebookEdit,Bash,PowerShell"
RESULT_FILE_DISALLOWED = "Edit,MultiEdit,NotebookEdit,Bash,PowerShell"
# A read-only worker fails CLOSED: under dontAsk every tool this list does not name is denied, where the
# denylist alone let through whatever it forgot (WebFetch, Agent, Workflow, a new MCP tool). dontAsk, not
# plan: measured against a local fake endpoint, plan mode sent each un-allowed call to an extra classifier
# model request (44 requests for a 2-tool turn against 4), and dontAsk denies it outright for free.
READ_ONLY_ALLOWED = "Read,Grep,Glob"
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
# Lines Claude Code prints on every run under this setup that explain nothing about a failure. They
# used to be the whole error message of a worker that simply ran out of turns.
_STDERR_NOISE = (re.compile(r"^Ignoring \d+ permissions\.allow entries"), re.compile(r"^\[claude-code:unrecognized_model\]"))


def _extract_json(text: str):
    """First JSON object in a reply (handles ```json fences and prose around it)."""
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    cands = [m.group(1)] if m else []
    start = text.find("{")
    while start != -1 and len(cands) <= 8:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    cands.append(text[start : i + 1])
                    break
        start = text.find("{", start + 1)
    for c in cands:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    return None


def _prompt(task: Task) -> str:
    user = task.prompt.strip() + prompt_clause(task.acceptance)
    if task.result_file:
        # The hooks enforce this; saying it up front saves the worker a denied write to learn it.
        user += (f"\n\nResult file: write your result to {Path(task.result_file).as_posix()} with the Write tool. It is the ONLY "
                 "file you may write; any other write is denied. A hook checks it after every write and before you finish.")
        if task.schema:
            user += " It must hold ONE JSON value and nothing else, matching this JSON schema:\n" + json.dumps(task.schema, ensure_ascii=False)
    elif task.schema:
        # Claude Code has no submit_result tool, so the schema rides as an output contract and the reply is parsed.
        user += "\n\nOutput contract: your final message must be ONE JSON object and nothing else, matching this JSON schema:\n" + json.dumps(task.schema, ensure_ascii=False)
    return inline_files(task, user)


def _command(task: Task, hook_settings: Path | None = None) -> list[str]:
    # stream-json (with --verbose, which it requires) carries every tool call, so a worker's edits and
    # tool count come from what IT did - the plain json envelope has neither.
    cmd = [*claude_argv(), "-p", "--output-format", "stream-json", "--verbose", "--model", cc_model_id(task.model), "--max-turns", str(task.max_turns)]
    if task.reasoning_effort is not None:
        cmd += ["--effort", task.reasoning_effort]
    # No --max-budget-usd: Claude Code cannot price a DeepSeek model ("unrecognized_model") and bills it at its own
    # fallback rate, measured 2026-09-25 at ~63x the real bill ($0.253 vs $0.004 for one 26.7k-token turn), so a
    # DeepSeek-dollar cap stopped every full-context worker on the Connections checkout after 2 turns
    # (job 20260925-053205-45c1, 4 of 4). max_cost_usd is enforced in real dollars by worker.py after the run.
    tier = cc_tier(task.tools)
    # Bypassing permissions takes both opt-ins (spec validates them; this re-checks, so a Task built around
    # the validator still gets the read-only argv). Every other case, a comma list included, is read-only.
    if tier in CC_WRITE_TIERS and task.confirm_write is True:
        cmd += ["--dangerously-skip-permissions"]
        if tier == "edit":
            cmd += ["--disallowedTools", "Bash,PowerShell"]
    elif task.result_file:
        # A read-only worker with a result file keeps Write: the PreToolUse hook holds it to that one path.
        cmd += ["--permission-mode", "dontAsk", "--allowedTools", READ_ONLY_ALLOWED + ",Write", "--disallowedTools", RESULT_FILE_DISALLOWED]
    else:
        cmd += ["--permission-mode", "dontAsk", "--allowedTools", READ_ONLY_ALLOWED, "--disallowedTools", READ_ONLY_DISALLOWED]
    if task.lean or task.isolated:
        # Measured 2026-09-24 from the Connections checkout against a local probe endpoint: the first request was
        # 138.7k chars with AGENTS.md in it; with `--setting-sources user` 84.6k, AGENTS.md, .claude/rules, the
        # project's hooks and settings gone, the worker's own CLAUDE.md and the user-level shield hook kept.
        # `--bare` (6.5k) was rejected: it drops the worker CLAUDE.md and the shield hook too. "user", not
        # "project,local": CLAUDE_CONFIG_DIR already keeps the operator's ~/.claude out, so "user" is the
        # worker's own config dir, and dropping it would drop the shield. Emitted once for lean and isolated.
        cmd += ["--setting-sources", "user"]
    if task.isolated:
        # isolated is lean plus no MCP server at all, so the task folder's .mcp.json servers cannot start either.
        cmd += ["--strict-mcp-config"]
    system = system_for(task)
    if system:
        cmd += ["--append-system-prompt", system]
    if hook_settings is not None:
        cmd += ["--settings", str(hook_settings)]
    return cmd


def _hook_dir(task: Task, stale_stamp: int | None = None) -> Path:
    """A private folder for one result-file task: the hooks' spec, the --settings file wiring them, and the
    log of every write the worker attempted. Removed after the run; the log rides on the transcript.
    `stale_stamp` is the result file's pre-launch result_hooks.file_stamp(), so the Stop hook refuses a leftover."""
    d = Path(tempfile.mkdtemp(prefix="zswarm-cc-result-"))
    spec = {"result_file": task.result_file, "schema": task.schema, "log": str(d / "writes.log"), "stale_stamp": stale_stamp}
    (d / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (d / "settings.json").write_text(json.dumps(result_hooks.settings(d / "spec.json")), encoding="utf-8")
    return d


def read_result_file(res: Result, task: Task, stale_stamp: int | None = None) -> None:
    """Publish the result file through the same validate() the hooks ran: a valid file becomes res.data (the
    parsed JSON under a schema, the text otherwise); an invalid one turns an ok run into an error that names why.
    A file still carrying its pre-launch `stale_stamp` was not written by this run and counts as missing."""
    value, errors = result_hooks.validate(task.result_file, task.schema, stale_stamp)
    if errors:
        res.data = None  # with a result file the data comes from the file alone, never a JSON-looking reply
        if res.status == "ok":
            res.status, res.error = "error", "result file not publishable: " + "; ".join(errors)
        return
    res.data = value


def _usage(u: dict) -> dict:
    """Claude Code's usage envelope as the ledger's four counters (Anthropic-shaped fields)."""
    inp, hit = int(u.get("input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0)
    miss = (inp - hit if inp >= hit else inp) + int(u.get("cache_creation_input_tokens") or 0)
    return {"in_hit": hit, "in_miss": miss, "out": int(u.get("output_tokens") or 0), "reasoning": int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)}


def _parse_stream(out: str) -> tuple[dict | None, int, list[str]]:
    """Claude Code's stream-json: the final `result` event (the same fields the json envelope has), the
    number of tool calls, and every path an edit tool was pointed at, in order. Unparseable lines skip."""
    final, tool_calls, edited = None, 0, []
    for ev in _events(out):
        if ev.get("type") == "result":
            final = ev
        for block in _tool_uses(ev):
            tool_calls += 1
            key = EDIT_TOOLS.get(block.get("name"))
            path = (block.get("input") or {}).get(key) if key else None
            if path:
                edited.append(str(path))
    return final, tool_calls, edited


def _events(out: str):
    """Each JSON event line of a stream-json run; unparseable lines skip."""
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _tool_uses(ev: dict) -> list[dict]:
    if ev.get("type") != "assistant":
        return []
    return [b for b in (ev.get("message") or {}).get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]


TRACE_INPUT_CHARS = 300


def tool_trace(out: str) -> list[dict]:
    """Every tool call a worker made, in order, as {tool, input} with the input cut to a readable line.
    WHY: `zswarm comply` grades whether a rule was obeyed by what the worker DID, and a call count alone
    cannot say whether the test ran before the commit."""
    return [{"tool": str(b.get("name") or ""), "input": json.dumps(b.get("input") or {}, ensure_ascii=False)[:TRACE_INPUT_CHARS]}
            for ev in _events(out) for b in _tool_uses(ev)]


def _relative_paths(paths: list[str], cwd: str) -> list[str]:
    """Sorted, de-duplicated, forward-slashed; repo-relative when under the task dir, as given otherwise."""
    root = Path(cwd).resolve()
    out = set()
    for p in paths:
        try:
            out.add(Path(p).resolve().relative_to(root).as_posix())
        except ValueError:
            out.add(Path(p).as_posix())
    return sorted(out)


def _meaningful_stderr(err: str) -> str:
    return "\n".join(l for l in err.splitlines() if l.strip() and not any(rx.search(l) for rx in _STDERR_NOISE))


def trust_project(cwd: str) -> None:
    """Mark the task folder trusted in the WORKER's own config dir (never the operator's), so Claude Code
    stops warning that it is ignoring the project's permission list. Write-tier workers already run with
    permissions bypassed. A read-only worker's denylist outranks a project allow rule for the edit and shell
    tools, but a trusted project's allow rule for some other tool does widen it: `isolated` drops project
    settings for a worker that must not inherit them. Concurrent workers may race the write - each
    writes the same key, and the replace is atomic, so a lost update only costs one repeat warning."""
    d = config.CC_CONFIG_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / ".claude.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        data = {}
    data.setdefault("hasCompletedOnboarding", True)
    key = str(Path(cwd)).replace("\\", "/")
    proj = data.setdefault("projects", {}).setdefault(key, {})
    if proj.get("hasTrustDialogAccepted") is True:
        return
    proj["hasTrustDialogAccepted"] = True
    tmp = d / f".claude.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _failure_reason(task: Task, res: Result, subtype: str, code: int, err: str) -> str:
    """Why a failed run failed: the turn cap by name, else the worker's own answer, else the exit and stderr."""
    if subtype == "error_max_turns":
        return f"stopped at max_turns={task.max_turns} after {res.turns} turns with no final answer; raise max_turns or split the task"
    if res.answer:
        return res.answer
    why = _meaningful_stderr(err)[-500:]
    return f"claude exit {code}" + (f" ({subtype})" if subtype and subtype != "success" else "") + (f": {why}" if why else "")


def _read_reply(res: Result, task: Task, j: dict, code: int, err: str) -> None:
    """Fill the Result from Claude Code's final result event: usage at DeepSeek rates, answer, status."""
    res.usage = _usage(j.get("usage") or {})
    res.cost_usd = config.cost_usd(task.model, res.usage["in_hit"], res.usage["in_miss"], res.usage["out"])
    res.turns = int(j.get("num_turns") or 0)
    res.answer = str(j.get("result") or "").strip()
    res.data = _extract_json(res.answer) if task.schema and res.answer else None
    if res.data is not None and not (res.answer.startswith("{") and res.answer.endswith("}")):
        res.add_taint("S")  # the contract asked for ONE JSON object and nothing else; this one was dug out of prose or a fence
    failed = j.get("is_error") or code != 0
    res.status = "error" if (failed or res.answer.startswith("FAILED:")) else "ok"
    if failed:
        res.error = _failure_reason(task, res, str(j.get("subtype") or ""), code, err)
    elif res.status == "error":
        res.error = res.answer


OUT_OF_BALANCE_SIGNATURE = "api error: 402"  # Claude Code's own rendering of the provider's 402, on every path that reaches res.error


def out_of_balance(res: Result) -> bool:
    """A cc run that died because its key had no balance. Claude Code renders the provider's 402 as the run's
    error, "API Error: 402 Insufficient Balance" (seen five times on 2026-09-16), and that literal is the whole
    signature: the ERROR is read and nothing else, a worker's own answer is never the run's death, and a looser
    match (a 402 anywhere near the word balance) took a failed worker's text about a payment gateway for the
    provider's verdict. A task that failed for its own reasons ("FAILED: ...") is not it either."""
    if res.status != "error" or not res.error:
        return False
    text = res.error.lower()
    return OUT_OF_BALANCE_SIGNATURE in text and not text.startswith("failed:")


async def run_cc_task(task: Task, api_key: str) -> tuple[Result, dict]:
    res = Result(id=task.id, backend="cc", model=task.model, started=now_iso())
    t0 = time.perf_counter()
    transcript: dict = {}
    hook_dir: Path | None = None
    stale_stamp: int | None = None
    try:
        if task.result_file:
            # Inside the try: a tempfile failure comes back as an error Result, not an exception out of here.
            stale_stamp = result_hooks.file_stamp(task.result_file)
            hook_dir = _hook_dir(task, stale_stamp)
        cmd = _command(task, hook_dir / "settings.json" if hook_dir else None)
        trust_project(task.cwd)
        # An OpenAI-only provider gets a loopback Anthropic facade for exactly this one Claude Code process.
        async with anthropic_endpoint(config.provider_of(task.model)) as base_url:
            code, out, err = await run_hidden(cmd, task.cwd, task.timeout_s, env=cc_env(api_key, task.model, envelope=task.envelope, anthropic_url=base_url), stdin_text=_prompt(task))
        err, out = err.replace(api_key, "sk-***"), out.replace(api_key, "sk-***")
        transcript = {"cmd": cmd, "exit": code, "stderr": err[-4000:]}
        if code == TIMEOUT_EXIT and not out:
            res.status, res.error = "timeout", f"claude exceeded {task.timeout_s}s"
            return res, transcript
        j, tool_calls, edited = _parse_stream(out)
        if j is None:
            try:
                j = json.loads(out)  # a CLI that ignored stream-json and printed one envelope
            except ValueError:
                res.status, res.error = "error", f"claude exit {code}, no result event: {out[-800:]!r} stderr: {_meaningful_stderr(err)[-800:]!r}"
                return res, transcript
        transcript["json"] = {k: v for k, v in j.items() if k != "result"}
        transcript["tool_trace"] = tool_trace(out)
        _read_reply(res, task, j, code, err)
        res.tool_calls = tool_calls
        # Only what THIS worker's edit tools touched. A git diff of the task dir credited every peer's edit
        # on a shared checkout to the worker; edits made through Bash are not visible here, and say so.
        res.files_changed = _relative_paths(edited, task.cwd)
        if task.result_file:
            read_result_file(res, task, stale_stamp)
    except asyncio.CancelledError:
        res.status, res.error = "cancelled", "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        res.status, res.error = "error", f"{type(e).__name__}: {e}"
    finally:
        if hook_dir is not None:
            log = hook_dir / "writes.log"
            transcript["result_file_writes"] = log.read_text(encoding="utf-8").splitlines()[-200:] if log.exists() else []
            shutil.rmtree(hook_dir, ignore_errors=True)
        res.seconds = round(time.perf_counter() - t0, 3)
        if task.acceptance:
            # File leaves are decided against the task dir as for api. Claude Code's Bash calls leave no receipt with
            # an exit code here, so tests_passed stays UNVERIFIED (runs=None) rather than read off the worker's prose.
            try:
                from .tools import Sandbox  # a path resolver only; cc.py otherwise never builds the api sandbox
                res.acceptance = decide_acceptance(task.acceptance, Sandbox(task.cwd, roots=task.roots or None), res.files_changed, None)
            except Exception as e:  # noqa: BLE001 - a checker bug leaves every criterion UNVERIFIED, never the result lost
                res.acceptance = [{"criterion": c, "verdict": "UNVERIFIED", "detail": f"checker error {type(e).__name__}: {e}"} for c in task.acceptance]
        res.finished = now_iso()
    return res, transcript
