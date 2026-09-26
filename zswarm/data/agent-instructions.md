## zswarm (ZergSwarm): delegate cheap parallel work

The `zswarm` MCP server fans tasks out to cheap LLM workers (DeepSeek, Gemini, Groq, OpenRouter and any
OpenAI-compatible endpoint the user configured) and returns their answers as data.

- Use `zswarm_run` for a batch: one task per file, item or question, many at once. Give every task an
  ABSOLUTE `cwd`, the `tools` preset it needs (`read` to look, `edit` to change files, `none` for pure
  reasoning), and a JSON `schema` whenever the answer is data.
- Use `zswarm_ask` for one tool-free question (classify, summarize, rewrite, second opinion).
- Leave `model` as `auto`: it picks the cheapest configured model that meets the task's published score
  floors and fails over when a provider runs out of keys or credit. `profile` (`general`, `code`,
  `decision`, `research`, `critical`) raises the bar for harder work.
- A long batch: pass `wait: false`, then poll `zswarm_status` and read `zswarm_results`.
- Workers are cheap, not infallible. Before relying on a finding, open the `file:line` it cites or re-run
  the command it quotes, and drop anything whose quote is not there.
- Keys, providers, model order and priority are the user's to manage in the web console (`zswarm ui`).
  `zswarm_doctor` and `zswarm_keys` show what is configured; never ask the user to paste a key into chat.
