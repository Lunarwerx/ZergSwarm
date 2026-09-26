# Connecting clients

`zswarm install --client <name>` (or the console's **Connect clients** page) writes one `zswarm` entry into the
client's config and leaves the rest of the file alone. `--client all` does every client; `--remove` takes the
entry out; `--instructions` also adds the short how-to block to the client's global instruction file.

Below is what each one writes, so you can do it by hand or check it. `<python>` is the interpreter zswarm is
installed in (`python -c "import sys; print(sys.executable)"`).

## Claude Code (terminal, VS Code / JetBrains, desktop app Code tab)

File: `~/.claude.json` (and `$CLAUDE_CONFIG_DIR/.claude.json`), as the [README](../README.public.md) sets up. By default every chat connects to
ONE shared server on 127.0.0.1:7790 instead of starting its own process per chat; the `headersHelper` starts that
server on demand and tells it which folder the chat is in.

```json
{
  "mcpServers": {
    "zswarm": {
      "type": "http",
      "url": "http://127.0.0.1:7790/mcp",
      "headersHelper": "<python> -m zswarm connect"
    }
  }
}
```

`zswarm install --stdio` registers a per-chat stdio server instead:
`{"type": "stdio", "command": "<python>", "args": ["-m", "zswarm", "mcp"]}`.

Or with Claude Code's own command: `claude mcp add --scope user zswarm -- <python> -m zswarm mcp`.

Instructions file: `~/.claude/CLAUDE.md`; the block written there is [agent-instructions.md](../zswarm/data/agent-instructions.md).

## Claude Desktop (the chat app)

File: `%APPDATA%\Claude\claude_desktop_config.json` on Windows, `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `~/.config/Claude/claude_desktop_config.json` on Linux. Claude Desktop starts local servers over stdio:

```json
{
  "mcpServers": {
    "zswarm": {"command": "<python>", "args": ["-m", "zswarm", "mcp"], "env": {}}
  }
}
```

Quit and reopen Claude Desktop afterwards. Claude Desktop has no global instruction file; the server's own
instructions (sent when it connects) tell the model how to use the tools.

Give tasks an absolute `cwd`: a desktop chat has no project folder of its own.

## Codex (CLI, IDE extension, desktop app)

File: `$CODEX_HOME/config.toml`, by default `~/.codex/config.toml`, shared by all three.

```toml
[mcp_servers.zswarm]
command = "<python>"
args = ["-m", "zswarm", "mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 900
```

`tool_timeout_sec` matters: Codex cancels a tool call after 60 s by default, and `zswarm_run` waits for its
batch (up to `wait_s`, 240 s by default). With a lower timeout, pass `wait: false` and poll `zswarm_status`.

Or with Codex's own command: `codex mcp add zswarm -- <python> -m zswarm mcp` (then raise the timeout in the file).

Instructions file: `~/.codex/AGENTS.md`.

## Anything else

Any MCP client that can start a stdio server can run `<python> -m zswarm mcp`. Clients that speak streamable
HTTP can use `http://127.0.0.1:7790/mcp` once the server is up (`zswarm serve-ensure` starts it). Programs that
do not speak MCP can use the [HTTP API](API.md).

## Troubleshooting

- `zswarm doctor` says what is missing: keys, models, `rg`/`bash` for worker tools, the `claude` binary for the
  `cc` backend (optional).
- **"No <provider> API key found"**: add one in `zswarm ui` or `zswarm keys add <provider>`.
- **Every task fails with NoCapableSwarmRoute**: no provider with an AUTO-ranked model has a ready key. Open
  Models & priority, tick "only AUTO-ranked", and add a key for one of those providers.
- **The client does not show the tools**: restart the client or open a new chat; check the file above has the
  entry (`zswarm install` prints the path it wrote).
- **Windows, `bash` tools**: worker shell commands need Git Bash (`bash.exe` on PATH or in the default Git
  install folder). Read-only work does not.
