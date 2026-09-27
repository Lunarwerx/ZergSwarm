## zswarm (ZergSwarm): hand cheap, wide work to other models

The `zswarm` MCP server runs tasks on cheaper models (Gemini, Groq, Cerebras, DeepSeek, OpenRouter and any
OpenAI-compatible endpoint the user added a key for), many at once, and returns their answers as data.
Delegate to it instead of grinding through wide, repetitive work yourself or spawning subagents on your own
model: reading or checking many files, per-item summaries, classification, extraction, grading, log and diff
scans, mechanical edits, second opinions. Keep the plan, the hard calls and the final answer yourself.

- `zswarm_run`: a batch, one task per file, item or question. Give every task an ABSOLUTE `cwd`, the `tools`
  it needs (`none` to reason, `read` to look, `web` to read web pages with read_url plus `web_hosts`, `edit` to
  change files in its folder, `all` adds a shell), and a JSON `schema` whenever the answer is data. `read`
  carries no read_url: a web question sent with it answers from memory. A long batch: `wait: false`, then `zswarm_status` and `zswarm_results`.
- `zswarm_ask`: one tool-free question (classify, summarize, rewrite, second opinion).
- Leave `model` on `auto`: each task gets the cheapest model the user has a key for whose published benchmark
  scores meet the task's bar, and moves to the next one when a provider runs out. `profile` (`code`,
  `decision`, `research`, `critical`) raises the bar; `zswarm_select` previews the pick without a model call.
- Workers are cheap, not infallible: before relying on a finding, open the `file:line` it cites or re-run the
  command it quotes, and drop anything whose quote is not there.
- Keys, providers and model order are the user's to manage in the console (`zswarm ui`); `zswarm_doctor` shows
  what is ready. Never ask the user to paste a key into chat.
