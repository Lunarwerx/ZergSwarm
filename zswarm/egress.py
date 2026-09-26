"""Egress receipts: a content-free, hash-chained record of every request body that left this machine.

WHY: the swarm ships session transcripts, files and prompts to third-party model hosts, and until this
module nothing on disk said what left, or where. After an incident the question is "did this ever get
sent, and to whom", and the answer must not require keeping the secret itself. So before each provider
POST one JSON line goes to ~/.zswarm/egress.jsonl holding the sink, the sha256 and byte count of the
EXACT bytes sent, and `prev` = sha256 of the previous raw line. Nothing of the payload is stored; a
line that is edited, dropped or reordered breaks the chain, and `zswarm egress verify` says where.

Two modes. The default is fail-open: a receipt that cannot be written never stops a swarm job (it is
counted and reported once on stderr). Inside `fail_closed()` - the memory pipeline's distill and triage,
which carry transcript text - a receipt that cannot be written REFUSES the send with EgressReceiptFailed.
`ZSWARM_EGRESS_STRICT=1` makes every send fail-closed.

Idea from garrytan/gstack lib/egress-receipt.ts (MIT); no code copied, written fresh for zswarm.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path

try:
    import msvcrt  # Windows: byte-range lock on the ledger's lock file
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
    import fcntl

from . import config

LOCK_WAIT_S = 2.0  # how long an append waits for another process's append before refusing (strict) or going ahead
TAIL_BYTES = 8192  # one receipt is ~250 bytes; the last line is always inside this window

_STRICT: contextvars.ContextVar[bool] = contextvars.ContextVar("zswarm_egress_strict", default=False)
FAILURES = 0  # receipts this process could not write in fail-open mode
_warned = False


class EgressReceiptFailed(RuntimeError):
    """A fail-closed send whose receipt could not be written. The request was NOT sent."""


def ledger_path() -> Path:
    # Derived from config.HOME at call time, so ZSWARM_HOME and the test fixture's patched HOME both hold.
    return config.HOME / "egress.jsonl"


def strict() -> bool:
    return _STRICT.get() or os.environ.get("ZSWARM_EGRESS_STRICT", "").lower() in ("1", "true", "on", "yes")


@contextlib.contextmanager
def fail_closed():
    """Every send started inside this block (and in asyncio tasks created inside it) refuses to go out
    without its receipt. Tasks inherit the flag because asyncio copies the context at create_task."""
    token = _STRICT.set(True)
    try:
        yield
    finally:
        _STRICT.reset(token)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _last_line(fd: int) -> bytes:
    """The last complete line of the ledger, without its newline; b"" for an empty file."""
    size = os.lseek(fd, 0, os.SEEK_END)
    if size == 0:
        return b""
    start = max(0, size - TAIL_BYTES)
    os.lseek(fd, start, os.SEEK_SET)
    tail = os.read(fd, size - start).rstrip(b"\r\n")
    return tail.rsplit(b"\n", 1)[-1]


def _lock(fd: int) -> bool:
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            if msvcrt is not None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)


def _unlock(fd: int) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _append(entry: dict) -> None:
    """Chain and append one receipt under the ledger's lock, so two processes never fork the chain."""
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _lock(lock_fd):
            raise OSError("egress ledger lock not taken within %.0fs" % LOCK_WAIT_S)
        try:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                prev = _last_line(fd)
                entry["prev"] = _sha(prev) if prev else ""
                line = json.dumps(entry, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
                os.lseek(fd, 0, os.SEEK_END)
                if os.write(fd, line) != len(line):
                    raise OSError("short write to the egress ledger")
            finally:
                os.close(fd)
        finally:
            _unlock(lock_fd)
    finally:
        os.close(lock_fd)


def record(sink: str, payload: bytes, provider: str = "", model: str = "") -> dict | None:
    """Write the receipt for `payload` BEFORE it is sent. Returns the receipt, or None when it could not
    be written in fail-open mode. In fail-closed mode a failure raises EgressReceiptFailed, and the caller
    must not send."""
    global FAILURES, _warned
    entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"), "sink": sink,
             "provider": provider, "model": model, "sha256": _sha(payload), "bytes": len(payload)}
    try:
        _append(entry)
        return entry
    except OSError as e:
        if strict():
            raise EgressReceiptFailed(f"EGRESS_RECEIPT_FAILED: no receipt for a send to {sink}, so it was not sent ({e})") from e
        FAILURES += 1
        if not _warned:
            _warned = True
            print(f"zswarm: egress receipt not written ({e}); sends continue fail-open", file=sys.stderr)
        return None


def verify(path: Path | None = None) -> dict:
    """Recompute the chain. {ok, lines, broken_at (1-based line), reason}; a missing ledger is ok with 0 lines."""
    p = path or ledger_path()
    if not p.exists():
        return {"ok": True, "lines": 0, "path": str(p)}
    prev = b""
    n = 0
    with p.open("rb") as f:
        for raw in f:
            n += 1
            line = raw.rstrip(b"\r\n")
            try:
                entry = json.loads(line)
            except ValueError:
                return {"ok": False, "lines": n, "broken_at": n, "reason": "not JSON", "path": str(p)}
            want = _sha(prev) if prev else ""
            if not isinstance(entry, dict) or entry.get("prev") != want:
                return {"ok": False, "lines": n, "broken_at": n, "reason": "prev does not match the line before", "path": str(p)}
            prev = line
    return {"ok": True, "lines": n, "path": str(p)}


def tail(limit: int = 20, path: Path | None = None) -> list[dict]:
    p = path or ledger_path()
    if not p.exists():
        return []
    with p.open("rb") as f:
        lines = f.read().splitlines()[-limit:] if limit > 0 else []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append({"unparseable": True})
    return out


def find(sha256: str, path: Path | None = None) -> list[dict]:
    """Every receipt for one exact payload hash: "did these bytes ever leave, and where to"."""
    p = path or ledger_path()
    if not p.exists():
        return []
    want = sha256.strip().lower()
    hits = []
    with p.open("rb") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("sha256") == want:
                hits.append(e)
    return hits
