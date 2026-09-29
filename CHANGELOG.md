# Changelog

What changed in each ZergSwarm release, newest first. A release's notes on GitHub are its section here, so
every version gets a section before its tag is pushed: the release workflow refuses a version without one.

## [Unreleased]

### Fixed
- On Windows, a crawl mark that another ZergSwarm process had cleared could stay set in this one: when the two
  writes to `~/.zswarm/crawl.json` landed in the same clock tick with the same file size, the newer reading was
  never read. A file written in the last two seconds is now always read again.
- The shared server no longer stalls every chat's calls on large files in `~/.zswarm`: `zswarm_doctor` checks only
  the egress receipts added since its last check (and off the server's loop), `zswarm_usage` and `zswarm_cost` read
  the cost ledger from the end back to the window asked for instead of the whole file, a running job's 10-second
  checkpoint no longer rewrites every finished answer (they are already in `results.jsonl`), and the job list
  parses a finished job's record once instead of on every poll.
- Key state (`keys.json`) is written compactly, retried when Windows briefly refuses the swap, and no longer leaves
  a `keys.json.<pid>.tmp` file behind when the swap fails.

## [1.2.7] - 2026-09-27

### Fixed
- A Groq key whose organisation has hit its spend alert is now set aside in the disabled slot, like a key that
  ran out of credit, and the call moves to your next Groq key at once. Before, one such key failed the task's
  whole Groq leg. Groq blocks each organisation separately, so your other keys can still serve.

## [1.2.6] - 2026-09-27

### Changed
- A task on a slow model now stays and finishes when every model after it is also crawling, rate-limited or
  broken, instead of hopping across them and ending on its cost cap.
- When a slow task does move, it skips the models marked crawling since it started.

## [1.2.5] - 2026-09-27

### Fixed
- 1.2.3 and 1.2.4 cut any call that ran past 150 seconds on a model with a fallback, which could also cut a
  healthy model in the middle of writing a large file. Now one turn that slow marks the model crawling for
  every task at once, a call is cut only when its model is already marked, and a call to an unmarked model
  always finishes.

## [1.2.4] - 2026-09-27

### Fixed
- A task that moves to another model no longer loses its conversation:
  - Only the message fields every chat API accepts cross to the next host. NVIDIA's replies carry
    `refusal: null`, which Groq rejects, so tasks that failed over from NVIDIA to Groq used to die.
  - A pinned model served by several hosts now continues the conversation on its next host instead of
    starting again from the prompt.
- An empty answer, after the worker's own nudges, moves the task to the next model, the same as a malformed one.

## [1.2.3] - 2026-09-27

### Changed
- A task moves off a crawling model mid-task, keeping its conversation, and always has somewhere to go:
  - A task's route now ends with the models of the next profile down, so even its last capable model is
    timed. If every model it qualifies for crawls, it moves to a fast one below the floor, and the answer
    says so and asks to be verified.
  - A call that has not answered in 150 seconds is cut, and the next model resends that turn.
  - Once one task sees a model crawl, the other tasks on it move after one slow turn instead of three.
  - Crawl marks are shared by every ZergSwarm process on the machine (`~/.zswarm/crawl.json`).

### Fixed
- A `404 model_not_found` opens that model's breaker for 30 minutes.
- Groq's `spend_limit_reached` moves the task to its next model.

### Docs
- A free provider's models carry zero prices, since the cost cap reads them (docs/PROVIDERS.md).

## [1.2.2] - 2026-09-27

### Fixed
- Calls to NVIDIA's free models charge nothing against a task's cost cap. A long task on them was being
  stopped at its $0.25 cap having spent nothing, because the provider file carried list prices.
- A single ask whose first model crawls now leaves time for the models behind it instead of spending all of
  it on the first.

### Docs
- README: NVIDIA's free trial keys in the provider lists and table (tried first; several keys paste in at once).
- docs/RUNTIME-SELECTION.md lists the full AUTO order.

## [1.2.1] - 2026-09-27

### Fixed
- `zswarm keys probe` re-reads every key's balance, but a balance read in the same clock tick still counted as
  fresh and was skipped. Windows' clock ticks coarsely, so there some keys were never re-read (and two probe
  tests failed on the Windows CI legs of 1.1.1). A read is now due once its age reaches the window.

## [1.2.0] - 2026-09-27

1.2.0 is the same code as 1.1.1: the minor version marks the NVIDIA work that first shipped there. Coming
from 1.1.0, this is what is new:

### Added
- NVIDIA (build.nvidia.com trial keys, free): AUTO serves these routes before any paid route that meets the
  same floor, still cheapest capable model first. Tool calling on GLM 5.3, GLM 5.3 Flash, Kimi K3 and
  Nemotron 3 Ultra, reasoning effort max, one live call per key spread round-robin across your keys.
- The console's key box and `zswarm keys add` take several keys at once.

### Changed
- NVIDIA's model list answers any key, so an NVIDIA key is checked with a one-token chat, and
  `zswarm keys probe` runs that check on every NVIDIA key (dead ones move to the disabled slot).
- AUTO tries a model whose last call crawled or timed out after the capable models that did not, until one
  fast call clears it.
- `zswarm_decide` sends every question that shares a state in one Jev call (36x fewer calls on a 36-question
  ask, answers 98-99% identical), passes structured questions to Jev as objects, and pins jev-1.13.0.
- Kimi K3 and Nemotron 3 Ultra are ranked on Artificial Analysis evidence.

## [1.1.1] - 2026-09-27

### Added
- Anonymous usage statistics to the LunarWerx dashboard: which command started, and a finished job's task and
  success counts, with the ZergSwarm version, OS, Python version and a random install id. Never a prompt,
  file, path, answer, provider or key. `ZSWARM_NO_PING=1` switches it off (README, Security and privacy).
- NVIDIA's free routes, listed under 1.2.0.

### Fixed
- A model-output 400 (`tool_use_failed` and its kin) that resampling could not clear moves the task to the
  next model instead of failing it.
- The shared server survives a client that drops mid-connection.
- A redact pattern named like a built-in one is refused instead of replacing the built-in;
  `conf['password'] = '...'` is redacted like `password = '...'`; two distilled facts whose names slug alike
  are both kept.
- `zswarm_run` results carry AUTO's pick instead of its whole record.

### Changed
- The installers take `ZSWARM_NO_SETUP` (the old `ZERGSWARM_NO_SETUP` still works).
- The console links the Discord.

### Docs
- README: telling your agent to install it comes first; a new header GIF from a real run (69 s, $0.30,
  12 of 12); the five providers AUTO picks from by itself are named; the spending limit is off until you set
  one; and it now says what the website says (one free key, ask your agent in plain words, a real review task
  with its answer, use without an agent, choosing which assistants setup connects).
- The README says plainly that anonymous usage statistics are collected.

## [1.1.0] - 2026-09-26

### Changed
- Every model pick uses your keys. Roles (judge, doubt, review), the blind panel, the doctor's routes and a
  bare `auto` now pick from the published benchmark plan over the providers you have a key for, as batch
  tasks already did. Before, a machine with only a Groq key had its judge, doubt and panel pinned to models
  it could not call.

### Added
- `zswarm setup` connects every assistant it finds (Claude Code, Claude Desktop, Codex) and opens the console.
- One command installs and connects: the installers run setup, install with uv when it is there or pipx on
  Python 3.11+, and install uv (which brings its own Python) when neither is, so there is nothing to install
  first.
- The console marks the providers ZergSwarm picks from by itself, and names them when your keys reach none.

### Docs
- README: a GIF of a real 12-job run on top, who it is for and how it differs from subagents, and no daily
  cap until you set one.

## [1.0.2] - 2026-09-26

1.0.1 never shipped (its build stopped at the package check), so 1.0.2 carries its changes too.

### Fixed
- The package metadata has one `[project.urls]` table again, with the website as its Homepage, so the
  package builds.

### Changed
- The console uses the logo's black and white.
- README screenshots you can read, and badges in the logo's charcoal instead of blue.
- The README and the package metadata link the website, zergswarm.lunarwerx.com.

### Docs
- README corrections: Mistral is not in AUTO, `cc` edit workers are not held to their folder, free tiers
  may keep what they get, and the daily cap is one you set.

## [1.0.1] - not released

Its build refused the package metadata (two `[project.urls]` tables). Everything in it shipped in 1.0.2.

## [1.0.0] - 2026-09-26

- The first 1.x. The code is the same as 0.2.1.

## [0.2.1] - 2026-09-26

### Added
- The logo: a README header in light and dark, a tab icon for the console, and a social preview card.

## [0.2.0] - 2026-09-26

The first public release.

### Added
- One-line installers for Windows, macOS and Linux.
- A tag-triggered release that tests, builds and publishes the wheel and source archive, and CI on three systems.
- `zswarm --version`, and `zswarm keys add` checks a key before it saves it.
- Console: models listed under their providers, use charts per provider and per model, denser tables and
  provider logos; long tables show five rows, then Show all.

### Fixed
- Linux and macOS: a timed-out child no longer takes the server down with it (children start their own session).
- Python 3.11 and 3.12: files are read with `open(newline=)` instead of the 3.13-only
  `Path.read_text(newline=)`.
- AgentHydra instance names, the process-slot check and the native benchmark work on Linux and macOS.
