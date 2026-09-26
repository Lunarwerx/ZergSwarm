# ZergSwarm (`zswarm`)

**Hand your coding agent a swarm.** ZergSwarm is an MCP server, CLI and local web console that lets Claude Code,
Claude Desktop or Codex fan work out to dozens of cheap LLM workers at once (DeepSeek, Gemini, Groq, Cerebras,
OpenRouter, Hugging Face, Mistral, or any OpenAI-compatible endpoint) and get their answers back as data.

Your main agent stays the orchestrator: it plans, decides and verifies. The swarm does the wide, repetitive
parts: read 60 files and report on each, check every call site, summarize every log, grade every answer.
Workers are sandboxed to the folder you give them, and every call is costed.

- **One batch, many workers.** `zswarm_run` takes a list of tasks, each with its own folder, tool preset
  (`none`, `read`, `edit`, `all`) and optional JSON schema, and runs them concurrently.
- **AUTO model choice.** Leave `model: "auto"` and each task gets the cheapest configured model whose
  published benchmark scores meet the task's bar (`profile`: general, code, decision, research, critical).
- **Keys that manage themselves.** Each provider has a key pool. A rate-limited key rests; a key out of
  credit goes to a disabled slot and is never retried until a free balance probe sees a top-up. When a
  provider runs dry, tasks fail over to the next capable model instead of stopping.
- **A web console** for keys, providers, models, priority, roles, jobs and client setup: `zswarm ui`. It is a
  searchable tree of everything on the left and the selected item's settings on the right; it adapts down to
  phone-width screens.
- **Works with** Claude Code (CLI, IDE extensions, desktop Code tab), Claude Desktop, and Codex (CLI, IDE
  extension, desktop app), plus a local HTTP API for anything else.

## Quick start

Needs Python 3.11+.

```bash
pip install "git+https://github.com/Lunarwerx/ZergSwarm"
zswarm ui
```

`zswarm ui` starts the local server and opens the console at `http://127.0.0.1:7790/ui`. Then:

1. **Providers**: pick a provider in the tree and paste a key. Free tiers work: Gemini, Groq and Cerebras all
   have one. Each key goes into that provider's file, `~/.zswarm/providers/<name>.toml`, and is never shown
   again in full.
2. **Clients**: pick Claude Code, Claude Desktop and/or Codex and click Install.
3. Open a new chat in your agent and ask it to use the swarm, for example:
   *"Use zswarm to read every file under src/ and list the functions that do network I/O."*

Prefer the terminal? The same steps without the browser:

```bash
zswarm keys add gemini            # prompts for the key (hidden), never takes it on the command line
zswarm install --client all --instructions
zswarm doctor                     # keys, models, binaries: what is ready
```

From a clone instead of pip: `git clone https://github.com/Lunarwerx/ZergSwarm && cd ZergSwarm && pip install -e .`
(or run `python zswarm.py <command>` with `httpx`, `jsonschema`, `mcp` and `tomlkit` installed).

## Using it from your agent

Once registered, the agent sees these tools (your client lists every one under the `zswarm` server):

| tool | what it does |
|---|---|
| `zswarm_run` | a batch of tasks, run concurrently; returns each answer (and `data` when a schema was given) |
| `zswarm_ask` | one tool-free question: classify, summarize, rewrite, second opinion |
| `zswarm_status` / `zswarm_results` / `zswarm_cancel` | follow a long batch started with `wait: false` |
| `zswarm_select` | preview which models AUTO would use right now, without a model call |
| `zswarm_keys` / `zswarm_models` / `zswarm_doctor` | what is configured and ready |
| `zswarm_review`, `zswarm_panel`, `zswarm_decide` | reviewer roster over a diff, a blind multi-model panel, batch decisions |

A task looks like this:

```json
{"id": "auth", "prompt": "List every place src/auth/ reads a token from the environment.",
 "cwd": "/abs/path/to/repo", "tools": "read",
 "schema": {"type": "object", "required": ["sites"], "properties": {"sites": {"type": "array", "items": {"type": "string"}}}}}
```

`zswarm install --instructions` adds a short block to `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` telling
your agent when and how to delegate; it is between markers, so re-running replaces it and `--remove` takes it
out. The text is in [zswarm/data/agent-instructions.md](zswarm/data/agent-instructions.md).

**Verify what workers tell you.** They are cheap, not infallible: a finding that cites `file:line` should be
checked at that line before anything depends on it.

## Configuring providers, models and keys

Every provider is one TOML file. zswarm ships one for each provider it knows, in [zswarm/providers/](zswarm/providers/); yours live in
`~/.zswarm/providers/` (set `ZSWARM_HOME` to move it). A file of yours with the same name as a shipped one
changes only what it says; a new name is a new provider. Roles, the review panel and price routing live in
`~/.zswarm/settings.toml`. The console writes these files for you and keeps any comments you add, so edit
whichever way you like. The full field list is in [docs/PROVIDERS.md](docs/PROVIDERS.md).

```toml
# ~/.zswarm/providers/groq.toml: your changes to the shipped Groq provider
keys = [
    "gsk_...",
    "gsk_...",
]
key_priority = { "3f9a1c2e" = 1 }   # this key (by fingerprint) first; the rest take turns after it

[models.groq-gpt-oss-120b]
priority = 1                         # starred: AUTO tries it first among the models that qualify
```

```toml
# ~/.zswarm/providers/ollama.toml: a new provider
base_url = "http://127.0.0.1:11434/v1"
keys = ["ollama"]                    # a local server needs no key: any placeholder works

[models.llama-local]
api_id = "llama3.2"
ctx = 131072
price = { hit = 0, miss = 0, out = 0 }
```

| you want to | console | in the provider's file |
|---|---|---|
| add a key | Providers › name: paste it | `keys = ["..."]` (or set `GROQ_API_KEY`, comma separated for several) |
| use some keys first, taking turns within a group | give keys a priority number (1 first; the same number takes turns; none is last) | `key_priority = { "<fingerprint>" = 1 }` |
| spread calls over every key (default) | leave the numbers empty | no `key_priority` |
| park one key | its switch in the key table | kept in `~/.zswarm/keys.json` by fingerprint |
| check a key works | added keys are checked at once; Check on a key row checks again (free) | `POST /api/keys/check` |
| cap what a day can cost | Routing & roles: Daily cap | `daily_cap_usd = 5` in `settings.toml` |
| stop using a provider without deleting its keys | its switch | `enabled = false` |
| never use a model | Models › name: its switch | `[models.<name>]` `enabled = false` |
| try some models first | the star, then its priority number (1 first) | `[models.<name>]` `priority = 1` |
| point a role at a model | Routing & roles | `[roles]` `code = "<model>"` in `settings.toml` |
| add an OpenAI-compatible endpoint | + Add provider | a new `<name>.toml` with `base_url` |
| add a model | + Add model | `[models.<name>]` with `api_id`, `ctx`, `price` |

Priority reorders; it never lowers the bar. AUTO still only considers models that meet the task's score
floors, and a model you add yourself has no published scores, so reach it by name (`model: "my-model"`), by a
role, or through a route. A running server picks up file changes on its next call, and new keys within
seconds; no restart needed.

## The console and the HTTP API

`zswarm ui` serves the console from the same local server your agents connect to (port 7790, loopback only).
Every button is a JSON call you can make yourself; see [docs/API.md](docs/API.md). Calls need the header
`X-Zswarm-Token` with the value in `~/.zswarm/console-token`, and the server refuses any request whose Host is
not this machine, so a web page cannot drive it. The page itself opens without a login; on a machine other people
share, set `ZSWARM_UI_SIGN_IN=1` and `zswarm ui` opens a one-time sign-in link instead.

## Client setup by hand

[docs/CLIENTS.md](docs/CLIENTS.md) has the exact config for each client, the timeouts that matter (Codex cuts
tool calls at 60 s unless told otherwise), and troubleshooting.

## How AUTO picks a model

`zswarm/data/published-models.json` holds published benchmark results, and every model whose provider file
names a `benchmark_slug` takes part in AUTO (29 ship: see `zswarm/providers/`). For a task, AUTO keeps the
configurations that meet every score floor of its profile, drops those with no ready key, puts your starred
list first, and orders the rest by benchmark cost. If a provider runs out mid-task, the task continues on the
next configuration with its completed tool calls preserved. See [docs/RUNTIME-SELECTION.md](docs/RUNTIME-SELECTION.md).

## Security notes

- The server listens on 127.0.0.1 only. Workers with `edit` or `all` tools can change files and run commands in
  the task's folder; give them the narrowest preset that does the job.
- Keys you add are stored in plain text in `~/.zswarm/providers/<name>.toml` (mode 600 on macOS/Linux), like
  most CLI tools' credentials: do not share or commit that folder. Keys are never logged, printed or returned by
  any tool: everything shows a fingerprint. Prefer environment variables? Leave `keys` out and set
  `<PROVIDER>_API_KEY`.
- Prompts and the files a worker reads are sent to the provider that serves the task. Switch off providers you
  do not want your code sent to.

## Development

```bash
pip install -e ".[test]"
pytest                      # unit tests; `-m live` also runs the ones that call real APIs
python scripts/console_dev.py   # the console against a scratch home, on port 7815
```

Layout and conventions for contributors (human or agent) are in [AGENTS.md](AGENTS.md).

## License

MIT. See [LICENSE](LICENSE).
