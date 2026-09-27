# Providers, models and keys: the files

Every provider is one TOML file, named after the provider. Two folders hold them:

- **Shipped:** `zswarm/providers/<name>.toml`, one per provider zswarm knows, with every model zswarm knows how to call and why
  it is set up the way it is. Read them as examples; do not edit them in place (an upgrade replaces them).
- **Yours:** `~/.zswarm/providers/<name>.toml` (`$ZSWARM_HOME/providers/` if you set it). A file with a shipped
  provider's name changes only the fields it names, model by model. A file with a new name is a new provider.

Roles, the review panel and price routing are machine-wide, so they live in `~/.zswarm/settings.toml`.

`zswarm ui`, `zswarm keys add|remove` and the HTTP API write these same files, one change at a time, and keep your
comments and layout. A running server reads a changed file on its next call and a new key within a few seconds.
A file that is not valid TOML is reported on the server's stderr and ignored (the server keeps what it last read
from it), and the console refuses to write over it until you fix it.

## A provider file

```toml
# ~/.zswarm/providers/acme.toml
base_url = "https://api.acme.example/v1"      # the part before /chat/completions
keys = [                                      # your keys; every one is used, in turn
    "sk-acme-...",
    "sk-acme-...",
]
key_priority = { "3f9a1c2e" = 1 }             # by fingerprint: 1 first, the same number takes turns, none last
docs = "https://docs.acme.example"

[models.acme-large]
api_id = "acme/large-2"                       # the id the API expects
ctx = 200000
price = { hit = 0.10, miss = 0.50, out = 2.00 }   # USD per 1M tokens: cached input, input, output
tools = true

[models.acme-small]
api_id = "acme/small-2"
ctx = 128000
price = { hit = 0.02, miss = 0.10, out = 0.40 }
aliases = ["small"]
```

### Provider fields

| field | meaning | default |
|---|---|---|
| `base_url` | the OpenAI-compatible endpoint, up to `/v1` | required for a new provider |
| `keys` | your API keys. Keep this file private: never commit or share it | none |
| `key_env` | environment variables to read keys from, comma separated inside each | `<NAME>_API_KEYS`, `<NAME>_API_KEY` |
| `key_files` | files in a clone's `.secrets/` folder, one key per line | `<name>_api_keys` |
| `key_priority` | `{ "<fingerprint>" = n }`: which keys serve first (the console shows fingerprints) | every key equal |
| `enabled` | `false` stops every call to this provider and keeps its keys | `true` |
| `options` | non-standard request fields the endpoint accepts (`reasoning_effort`, ...); others are dropped | none |
| `omit` | standard fields the endpoint rejects (Mistral 422s on `user`) | none |
| `headers` | extra request headers (attribution, never credentials) | none |
| `models_path` | the model list route, used by `zswarm doctor` | none |
| `balance_path`, `credits_path` | balance routes a probe reads | none |
| `balance_authority` | `"reading"`: a zero balance parks the key before a worker gets it; `"status"`: the number is shown only, and a real 402 parks the key | `"reading"` (the console's Add provider writes `"status"`) |
| `passthrough` | prefixes that reach any model the provider serves (`["or:"]` gives `or:<id>`) | none |
| `host_pin` | `"suffix"`: a `#<host>` pin rides in the model id instead of a request field | none |
| `upstream_header` | the response header naming the host that served the call | none |
| `anthropic_url` | an Anthropic Messages endpoint for `cc` (headless Claude Code) workers; `"facade"` runs them through zswarm's local translator | none: no `cc` |
| `anthropic_auth` | `"x-api-key"` or `"bearer"` | |
| `live_per_key` | live calls per usable key before a job waits | 4 |
| `free_tier` | the provider's free terms may keep what it is sent; `ZSWARM_REDACT_FREE_TIER` redacts a task that sets no `redact` when it runs here | `false` |
| `free_calls` | a call here costs nothing (NVIDIA's trial keys): AUTO serves this provider's routes before any paid route that meets the same floor, still cheapest capable model first. Give its models `price = {hit = 0.0, miss = 0.0, out = 0.0}` too: the ledger and a task's `max_cost_usd` read the price, and a test fails a free provider that carries one | `false` |
| `about` | one plain sentence about the provider, shown in the console | |
| `key_url` | the page where a person makes an API key (the console's "Get a key" link) | |
| `website` | the company's site; the console shows its favicon (fetched by your server into `~/.zswarm/favicons/`) | |
| `check_path` | the endpoint the key check asks, when the balance or model list would answer without a valid key | balance, else models |
| `check_model` | check a key with a one-token chat on this (free) model instead of a GET | |
| `docs` | a link shown in the console | |

### Model fields

A model's provider is the file it is in. In your file for a shipped provider, a model table changes only what it
names: `[models.deepseek-flash-or]` with `fallback = false` promotes that leg and leaves the rest as shipped.

| field | meaning |
|---|---|
| `api_id` | the id sent to the API (defaults to the table name) |
| `ctx`, `max_out` | context window and output limit, in tokens |
| `price` | `{ hit, miss, out }` in USD per 1M tokens. No price means cost unknown (shown as `-`), never zero |
| `peak` | DeepSeek-style peak rates instead of `price`; off-peak is half |
| `tools` | `true` when the model can call tools |
| `vision` | `true` when it reads images |
| `concurrency_limit` | the provider's own ceiling on parallel calls |
| `extra` | request fields sent with every call (OpenRouter's `provider` steer) |
| `reasoning_mandatory` | `true` when the model refuses to run with reasoning off |
| `aliases` | other names for it (`aliases = ["flash"]`) |
| `route` | the paths to this model, in order (`["deepseek-flash", "deepseek-flash-or"]`). Price orders them within a tier |
| `route_cc` | the same for `cc` workers; a `route` of yours without one applies to `cc` too |
| `fallback` | `true`: used only when no primary path has a key with credit |
| `enabled` | `false`: never called, even when a task names it |
| `priority` | a star: AUTO tries it first among the models that meet the task's bar, lowest number first |
| `kind` | `"typed"` for a model that answers typed questions only (pick one, yes/no, rate), like TypeSafe's Jev: `zswarm_decide` uses it, a task never does; default `"chat"` |
| `backup` | `true`: a last resort for any task (not a critical one) after every tested model, when those are busy or dead, marked untested in the plan |

### AUTO's models

Models with a `benchmark_slug` take part in AUTO: the slug names their published scores in
`zswarm/data/published-models.json`, and AUTO picks the cheapest one that meets the task's score floors. The
shipped ones are named `rank:<configuration>`. Their extra fields:

| field | meaning |
|---|---|
| `benchmark_slug` | the published configuration it was measured as |
| `inherits` | start as a copy of another model (its wire id included), then change what differs |
| `default_reasoning_effort`, `default_thinking` | the effort and thinking it was measured at, and is run at |
| `cc_effort` | `true` when a `cc` worker can carry that effort |
| `siblings` | same-provider models to try, unevidenced, after every evaluated one fails (a per-model quota) |

Star or switch one off like any model. A file of yours that changes a shipped AUTO model's `api_id`, effort or
provider takes it out of AUTO (it would be claiming scores it was not measured at); give your own configuration its
own name instead.

## settings.toml

```toml
routing = true          # false serves every model by its own name, never a cheaper path to it
load_bias = 0.5         # 0 ranks AUTO on benchmark cost alone; higher spreads a batch across near-equal providers
daily_cap_usd = 5      # once today's spend reaches this, new jobs and asks are refused until tomorrow
panel = ["rank:gpt-6-luna-high", "rank:mimo-v2-6-pro"]   # zswarm_panel's default seats

[roles]
code = "auto"           # or a model name: every task with role "code" runs on it
search = "gemini-3.8-flash"
```
