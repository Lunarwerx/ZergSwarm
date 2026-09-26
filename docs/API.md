# HTTP API

The console at `http://127.0.0.1:7790/ui` is a page over this API; anything it does, a script can do. By default
`/ui` opens without a login for any request from this machine, and the page carries the token. With
`ZSWARM_UI_SIGN_IN=1` set where the server starts, `zswarm ui` signs the browser in once through `/ui?t=<token>`,
which sets an HttpOnly session cookie and redirects to `/ui`; without that cookie `/ui` then answers 401 and carries
no token.

- Base: `http://127.0.0.1:7790/api/` (the shared server; `zswarm serve-ensure` or `zswarm ui` starts it).
- Every call sends `X-Zswarm-Token: <contents of ~/.zswarm/console-token>`. Missing or wrong: 401.
- The Host header must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`; anything else: 403.
- GET takes query parameters; POST takes a JSON object body and nothing in the URL (a POST with a query string is
  refused with 400: URLs land in logs). Names (providers, models, fingerprints) always go in the body or query,
  never the path. A body over 1 MB: 413.
- Errors come back as `{"error": "..."}`: 400 for a request that cannot be done as asked (the message says what to
  do instead), 404 for an unknown route (with the list of routes), 500 with the file and line that raised.

```bash
TOKEN=$(cat ~/.zswarm/console-token)
curl -s -H "X-Zswarm-Token: $TOKEN" http://127.0.0.1:7790/api/state
```

## Settings

Every settings change returns the full `state` afterwards, except the three key routes: `keys/add` returns
`{fingerprint, added, masked, file}`, `keys/remove` `{fingerprint, removed}` and `keys/enabled`
`{fingerprint, enabled}`; read `state` or `keys` after them.

| method | route | body / query | does |
|---|---|---|---|
| GET | `state` | | providers, models, priority, roles, options, file locations. No network call, no key |
| GET | `keys` | `provider` | that provider's keys in the order the pool reaches for them: fingerprint, masked form, source, state, priority, editable |
| POST | `keys/add` | `provider`, `key` | append a key to `keys` in `~/.zswarm/providers/<provider>.toml` |
| POST | `keys/remove` | `provider`, `fingerprint` | delete a key from that file (keys from the env or `.secrets/` are refused) |
| POST | `keys/priority` | `provider`, `fingerprint`, `priority` | a key's priority number: 1 is used first, keys sharing a number take turns, keys with none come last; empty clears it |
| POST | `keys/enabled` | `provider`, `fingerprint`, `enabled` | move a key out of or into the disabled slot |
| POST | `keys/probe` | `provider` (optional) | one free balance read per key; a topped-up key leaves the disabled slot |
| POST | `keys/check` | `provider`, `fingerprint` | one free request with that key alone: `result` is `ok`, `rejected` (the key goes to the disabled slot with the reason) or `unchecked` |
| POST | `providers/set` | `name`, and `enabled`, `base_url` and/or `website` | change a provider (`website`: the company's site, whose icon the console shows) |
| POST | `providers/add` | `name`, `base_url`, optional `docs`, `anthropic_url`, `website` | add an OpenAI-compatible provider |
| POST | `providers/remove` | `name` | remove a provider you added: deletes `~/.zswarm/providers/<name>.toml`, its models and the keys in it |
| POST | `favicons/fetch` | optional `providers` (a list of names) | fetch the icons from the providers' `website`; by default every provider whose icon was never fetched |
| POST | `models/enabled` | `name`, `enabled` | switch a model on or off |
| POST | `models/add` | `name`, `provider`, optional `api_id`, `ctx`, `price` `{hit, miss, out}` (USD per 1M), `vision`, `tools` | add a model |
| POST | `models/remove` | `name` | remove a model you added |
| POST | `models/priority` | `name`, `priority` | star a model with a priority number (1 first; AUTO tries numbered models first, among those that meet the bar); empty clears it |
| POST | `roles` | `role`, `model` (`auto` or a name) | point a role at a model |
| POST | `options` | `routing` (bool), `load_bias` (0-5), `daily_cap_usd` (dollars, `""` clears) | price routing between equivalent paths; how hard AUTO spreads load; the most one local day may spend |

## Work

| method | route | body / query | does |
|---|---|---|---|
| POST | `run` | the `zswarm_run` arguments: `tasks`, `cwd`, `tools`, `model`, `profile`, `schema`, `wait`, `wait_s`, `concurrency`, `budget_usd`, `label`, ... | start a batch; with `wait: false` it returns the job id at once |
| GET | `jobs` | `limit` | recent jobs |
| GET | `job` | `id`, optional `max_answer_chars` | a job's status and results |
| POST | `job/cancel` | `id` | cancel a running job |
| POST | `ask` | the `zswarm_ask` arguments: `prompt`, `model`, `system`, `schema`, `profile`, ... | one tool-free call |
| POST | `models/test` | `model` | a one-word paid call on exactly that model, to check a key and a model |
| POST | `select` | `profile`, `tools`, `backend` | preview AUTO's candidates now, no model call |
| GET | `doctor` | | the same report as `zswarm doctor` |
| GET | `usage` | `days` (1-90, default 14) | spend, tasks and outcomes per local day, newest last, for the console's charts |

## Clients

| method | route | body | does |
|---|---|---|---|
| GET | `clients` | | Claude Code, Claude Desktop and Codex: config path, whether zswarm is registered |
| POST | `clients/install` | `client`, optional `remove`, `instructions` | register (or unregister) zswarm with that client |
