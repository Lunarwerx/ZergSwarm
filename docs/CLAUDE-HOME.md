# What zswarm reads and writes in the Claude Code home

Claude Code keeps its per-user state in `.claude.json` and the `.claude/` folder of the user's home
directory. Both are machine-private and nothing in them is in this repo, so this page is the in-repo record
of every place zswarm touches them, what for, and which module does it. Everything else zswarm keeps lives
in the clone and in `~/.zswarm` (the README's "What lives in `~/.zswarm`").

## What zswarm writes

| path (under the user's home) | written by | what |
|---|---|---|
| `.claude.json` (and `$CLAUDE_CONFIG_DIR/.claude.json`, and any `install --also` path) | `zswarm install` / `zswarm setup`, `zswarm/install.py` | One `mcpServers.zswarm` entry, written atomically. Nothing else in the file changes; `--remove` takes the entry out. This is the "one MCP registration line" the README means. |
| `.claude/CLAUDE.md` | `zswarm install --instructions`, `zswarm/install.py` | Only with that flag: the short "how to use zswarm" block from `zswarm/data/agent-instructions.md`, between markers, so a re-run replaces it and `--remove` takes it out. |
| `.claude/skills/zswarm-cli/SKILL.md` | `zswarm skill --install`, `zswarm/clihelp.py` | The CLI skill ("use `help --json`, honour the effect"); `skill --check` says when it is stale. |

## What zswarm only reads

| path (under the user's home) | read by | what for |
|---|---|---|
| `.claude/projects/` | `zswarm/claude_usage.py` (`savings`), `zswarm/distill.py`, `zswarm/procedures.py` | Claude Code's session transcripts: this machine's own Claude usage for savings, and the input to the memory distiller and procedure miner. `ZSWARM_CLAUDE_PROJECTS` or `--root` points elsewhere. Layout: `<project slug>/<session>.jsonl`; a sub-agent's transcript is `<project slug>/<session>/subagents/agent-<id>.jsonl` beside a `.meta.json` naming its agent type. Dedupe on `requestId` before summing usage. |
| `.claude/settings.json` | `zswarm/toolhooks.py` | Only when `ZSWARM_API_HOOKS=claude`: the operator's hooks, run on an `api` worker's tool calls (README, "Claude Code hooks on the `api` backend"). |
| `.claude/tools/agent_shield.py` | `zswarm/claude_env.py` | When present, the one hook a `cc` worker inherits (PostToolUse, a prompt-injection screen). Absent, the worker simply has no hooks. |
| the default Claude Code login | headless `claude -p` outside zswarm | Not used by zswarm workers. Bench arms that ran `claude -p` on it failed when its OAuth session expired (BENCH-2026-09-15.md). |

## What zswarm keeps out of it

A `cc` worker runs Claude Code with `CLAUDE_CONFIG_DIR` set to its own config dir, `~/.zswarm/claude-config`
(`config.CC_CONFIG_DIR`, seeded by `claude_env.ensure_cc_config`). The operator's own Claude Code login,
hooks, settings and CLAUDE.md therefore never reach a worker, and a worker never writes into them. That is
why `lean` passes `--setting-sources user` rather than `project,local`: `user` now means the worker's own
config dir, whose only hook is the shield above.

## The shared layer that fills the Claude Code home on these machines

On the fleet machines most of the `.claude/` folder is not hand-made: it is the `home/` folder of the
private `Lunarwerx/claude-memory` repo (cloned at `~/claude-memory`), copied into place
by `node install.mjs` in that repo after every pull. It holds the global `CLAUDE.md`, the hooks (among them
`agent_routing_gate`, which writes `~/.zswarm/routing.jsonl`), the tools (`agent_shield.py`, and the memory
tools `memlint`, `memcurate`, `memsearch`, `memapply`), the skills (among them `zswarm-routing`) and the
commands. zswarm does not need it: without it there is no shield hook and no routing gate, and everything
above still works. Where it all lives: WHERE-EVERYTHING-LIVES.md.
