# Working on zswarm (ZergSwarm)

An MCP server + CLI + local web console that fans tasks out to cheap OpenAI-compatible LLM workers. One Python
package, `zswarm/`, asyncio, no GPU. `zswarm.py` at the root runs it from a clone.

## Where things are

- `zswarm/providers/<name>.toml`: every provider and its models, routes, aliases and AUTO configurations, one
  file each (docs/PROVIDERS.md). `~/.zswarm/providers/<name>.toml` layers over them (keys included) and
  `~/.zswarm/settings.toml` holds roles and knobs.
- `zswarm/config.py`: loads those files into the registry (`reload`), key sources, prices, peak windows;
  `refresh()` reloads a running server when a file changes.
- `zswarm/settings.py`: the ONE writer for the user's files (keys, key priority, provider/model switches,
  priority, custom providers and models, roles, options), through tomlkit so hand-written comments survive. The
  console, the CLI `keys add|remove` verbs and the HTTP API all call it.
- `zswarm/console.py` + `zswarm/ui/console.html`: `zswarm ui` and `/api/*`, mounted on the shared server.
- `zswarm/install.py`: `zswarm setup` (every client found, then the console) and `zswarm install`: registration with Claude Code, Claude Desktop and Codex.
- `zswarm/selection.py`: AUTO, over the models with a `benchmark_slug` and `zswarm/data/published-models.json`;
  priority and switches filter here. Roles, the panel and a bare `auto` pick through `dispatch.first_choice` over the keys this machine holds.
- `zswarm/client.py` (`KeyPool`, `ChatClient`), `jobs.py` (`JobManager`), `dispatch.py` (failover), `agent.py`
  and `worker.py` (the worker loop), `tools.py` (the sandboxed worker tools).
- `zswarm/mcp_server.py`: every MCP tool; `shared.py`: the one HTTP server per machine.
- `tests/`: pytest. `conftest.py` isolates every test in a throwaway home; nothing touches the real `~/.zswarm`.

## Rules

- Never print, log or return an API key. Show a fingerprint (`config.fingerprint`) or `settings.mask`.
- A settings change goes through `settings.py`; do not write the user's provider files or settings.toml anywhere else.
- A new provider or model is a TOML file or table, never a code change.
- A new user-facing knob shows up in the console, the API table in `docs/API.md`, and the README table.
- Workers run with the tools their preset grants and nothing else; keep the loopback-only server and the
  console's Host and token checks intact.

## Checks

```bash
pip install -e ".[test]"
pytest -q                          # unit tests, no network
python scripts/console_dev.py      # console against a scratch home at http://127.0.0.1:7815/ui
```
