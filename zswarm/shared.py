"""ONE MCP server per machine instead of one per chat (owner ask, 2026-09-24).

A stdio server is a child of each Claude chat: 59-73 MB apiece, about 2.6 GB at 40 chats. `zswarm.py mcp --http`
serves FastMCP's streamable-http transport on 127.0.0.1 only, and every chat connects to it with
{"type": "http", "url": "http://127.0.0.1:7790/mcp"}. `zswarm.py serve-ensure` keeps it up with nobody watching:
it returns at once when the port answers, else starts the server detached and hidden, behind a lock file so two
callers cannot start two.

⛔ ONE PROCESS SERVES EVERY CHAT, so nothing a tool does may read the server's own environment or working folder:
both belong to whoever started the server. The caller stamp (caller.detect) and a task's default cwd
(spec.Task._validate) read the calling request instead: REQUEST holds the X-Zswarm-* headers of the MCP request
being handled, set by `_request_headers` for the duration of that request. A header the chat did not send stays
"" - the stamp never falls back to the server's identity.
"""
from __future__ import annotations

import contextvars
import http.client
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

from . import config

HOST = "127.0.0.1"  # loopback only: the server runs workers with file and shell tools, it must never face a network
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
PORT = 7790
REPLACE_TRIES = 5  # os.replace attempts: Windows refuses it while another process has the target open
LOCK_STALE_S = 60.0  # a lock older than this belongs to a caller that died mid-start
IDLE_SESSION_S = 86_400.0  # a chat idle overnight keeps its session; the SDK default (30 min) would drop it

ACTIVE = False  # True inside the shared HTTP server; stdio servers and the CLI keep reading their own environment
SERVING_PORT: int | None = None  # the port this process serves on, when it is the shared server
REQUEST: contextvars.ContextVar[dict | None] = contextvars.ContextVar("zswarm_request", default=None)
HEADERS = {"session": "x-zswarm-session", "chat": "x-zswarm-chat", "instance": "x-zswarm-instance", "cwd": "x-zswarm-cwd",
           "mcp_session": "mcp-session-id",
           "envelope": "x-zswarm-envelope"}  # the spawn envelope a cc worker's zswarm runs under (envelope.inherited)
SHARED_NOTE = (" This is the ONE zswarm server every chat on this machine shares, so it cannot see your folder: give every "
               "task that uses tools or files an ABSOLUTE cwd (your project folder).")


def local_host(host: str, port: int) -> bool:
    """True when a request's Host header names this machine. A page on another origin that rebinds its DNS name to
    127.0.0.1 still sends its own name, so every custom route refuses anything else."""
    return host in {f"{h}:{port}" for h in LOCAL_HOSTS}


def atomic_write(path: Path, text: str, private: bool = False) -> None:
    """Swap `text` in as the whole of `path`: a reader sees the old file or the new one, never half of either.
    private=True makes it owner-only (0600 on POSIX) from the moment the temp file exists, never after the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # one per process: two writers never share a temp file
    tmp.unlink(missing_ok=True)  # a leftover from a crash could carry a wider mode than asked for
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a server is reading the target right now (config.refresh)
            if attempt == REPLACE_TRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))


class _NoQuery(logging.Filter):
    """uvicorn's access log writes each request line to the server log; a query string can carry a key, a prompt or
    the console's sign-in token, so the line keeps the path only."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


async def _request_headers(ctx, call_next):
    """Server middleware: every MCP request runs with REQUEST set from its own HTTP headers."""
    headers = getattr(getattr(ctx, "request", None), "headers", None) or {}
    token = REQUEST.set({k: unquote(headers.get(h) or "") for k, h in HEADERS.items()})  # connect() percent-encodes
    try:
        return await call_next(ctx)
    finally:
        REQUEST.reset(token)


def serve(port: int = PORT) -> None:
    """Run the shared server in this process (blocks). `zswarm.py mcp --http [--port N]`."""
    global ACTIVE, SERVING_PORT
    from starlette.responses import JSONResponse

    from .mcp_server import mcp

    ACTIVE, SERVING_PORT = True, port
    config.ensure_dirs()
    from . import verdict

    verdict.write()  # offline; the routing gate reads it (verdict.py)
    started = time.time()
    # Only this server has no folder of the caller's own; a stdio server's chats never need the sentence.
    mcp._lowlevel_server.instructions = (mcp._lowlevel_server.instructions or "") + SHARED_NOTE

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        if not local_host(request.headers.get("host", ""), port):  # a rebound page must not learn zswarm runs here
            return JSONResponse({"error": "answers only on 127.0.0.1 / localhost"}, status_code=403)
        return JSONResponse({"zswarm": True, "pid": os.getpid(), "port": port, "up_s": round(time.time() - started)})

    from . import console

    console.mount(mcp, port)  # the web console and the HTTP API: /ui, /api/* (console.py)
    mcp._lowlevel_server.middleware.append(_request_headers)  # the SDK's documented seam: Server.middleware
    logging.getLogger("uvicorn.access").addFilter(_NoQuery())
    mcp.run(transport="streamable-http", host=HOST, port=port, session_idle_timeout=IDLE_SESSION_S)


def probe(port: int = PORT, timeout: float = 0.5) -> dict | None:
    """None when nothing listens; the /health body when zswarm does; {"zswarm": False} when something else does."""
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        try:  # Windows answers a closed loopback port with a connect TIMEOUT (it retries the SYN), not a refusal
            conn.connect()
        except OSError:
            return None
        conn.sock.settimeout(max(timeout, 3.0))  # a busy zswarm is still zswarm: give the answer longer than the connect
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = resp.read(4096)
    except OSError:
        return {"zswarm": False}
    finally:
        conn.close()
    try:
        data = json.loads(body)
    except ValueError:
        return {"zswarm": False}
    return data if resp.status == 200 and isinstance(data, dict) and data.get("zswarm") is True else {"zswarm": False}


def lock_path(port: int) -> Path:
    return config.HOME / f"mcp-http-{port}.lock"


def log_path(port: int) -> Path:
    return config.HOME / "logs" / f"mcp-http-{port}.log"


def _take(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
            lock.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _spawn(port: int) -> int:
    """Start `zswarm.py mcp --http` detached from the caller, with no window, output to the log. Returns its pid."""
    exe = Path(sys.executable)
    if os.name == "nt" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    argv = [str(exe), *config.launcher()[1:], "mcp", "--http", "--port", str(port)]
    log = log_path(port)
    log.parent.mkdir(parents=True, exist_ok=True)
    kw: dict = {"cwd": str(config.HOME), "stdin": subprocess.DEVNULL, "close_fds": True}
    with open(log, "ab") as out:
        kw.update(stdout=out, stderr=subprocess.STDOUT)
        if os.name != "nt":
            return subprocess.Popen(argv, start_new_session=True, **kw).pid
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:  # out of the starting chat's job object, so closing that chat does not take the server with it
            return subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw).pid
        except OSError:  # the job forbids breakaway: still detached, but tied to the job's lifetime
            return subprocess.Popen(argv, creationflags=flags, **kw).pid


def _wait(port: int, wait_s: float) -> dict | None:
    deadline = time.time() + wait_s
    while True:
        h = probe(port)
        if h or time.time() >= deadline:
            return h
        time.sleep(0.2)


def _answer(h: dict, port: int, state: str) -> dict:
    if not h.get("zswarm"):
        return {"ok": False, "error": f"port {port} answers but it is not zswarm; pick another with --port"}
    return {"ok": True, "state": state, "pid": h.get("pid"), "url": f"http://{HOST}:{port}/mcp"}


def ensure(port: int = PORT, wait_s: float = 30.0) -> dict:
    """The keeper: running -> return at once; down -> start it once (lock-guarded) and wait until it answers."""
    h = probe(port)
    if h:
        return _answer(h, port, "running")
    lock = lock_path(port)
    if not _take(lock):  # another caller is starting it right now
        h = _wait(port, wait_s)
        if h:
            return _answer(h, port, "running")
        return {"ok": False, "error": f"another caller holds {lock} (starting the server) and it has not answered in {wait_s:.0f}s"}
    try:
        h = probe(port)
        if h:
            return _answer(h, port, "running")
        pid = _spawn(port)
        h = _wait(port, wait_s)
        if not h:
            return {"ok": False, "error": f"started pid {pid} but port {port} did not answer in {wait_s:.0f}s; see {log_path(port)}"}
        return _answer(h, port, "started")
    finally:
        lock.unlink(missing_ok=True)


def connect(port: int = PORT) -> int:
    """The chat's headersHelper (`zswarm.py connect`): make sure the shared server is up, then print the chat's
    X-Zswarm-* headers as JSON. Claude Code runs it with the chat's folder and environment each time it connects the
    entry, so a chat never reaches the server without its own cwd and caller stamp, and a server that cannot come up
    fails that chat's connection with the reason. Register it once per machine:
      claude mcp add-json -s user zswarm '{"type":"http","url":"http://127.0.0.1:7790/mcp","headersHelper":"python <abs>/zswarm.py connect"}'
    """
    from .caller import _instance_of

    out = ensure(port, wait_s=8.0)  # Claude Code gives a headersHelper 10 s
    if not out["ok"]:
        print(f"[zswarm connect] {out['error']}", file=sys.stderr)
        return 1
    env = os.environ
    values = {"cwd": env.get("CLAUDE_PROJECT_DIR") or os.getcwd(), "session": env.get("CLAUDE_CODE_SESSION_ID") or "",
              "chat": env.get("CLAUDE_CODE_HOST_SESSION_ID") or "",
              "instance": _instance_of(env.get("CLAUDE_CODE_EXECPATH") or env.get("CLAUDE_CONFIG_DIR") or "")}
    print(json.dumps({HEADERS[k]: quote(v, safe="") for k, v in values.items() if v}))
    return 0
