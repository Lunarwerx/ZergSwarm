"""Store a long string ONCE per job, not once per task.

Measured 2026-09-16 on one day of real jobs: `~/.zswarm/jobs` held 1,165 MiB, and 1,153 MiB of it was the
same few strings written thousands of times. A 1,844-task job wrote a 162 KiB shared system prompt onto
every task row (270 MiB of `job.json`) and again into every worker transcript (another ~300 MiB), while
the actual prompts, answers and structured results of that job came to under 1 MiB. Nothing read any of
it back.

So the fix is not to throw the detail away, it is to stop copying it. Any string at or over `MIN_BYTES`
is written to `<job>/blobs/<sha1>.txt` and replaced in the record by `{"<field>_ref": "<sha1>"}`. Identical
strings collapse to one file, the record stays human-readable JSON, and `expand()` puts it back exactly.
Packing something already packed is a no-op, so it is safe to run twice.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MIN_BYTES = 2048
SUFFIX = "_ref"
BLOBS = "blobs"


def blob_dir(job_dir: Path) -> Path:
    return Path(job_dir) / BLOBS


# newline="" on BOTH sides, always. Windows text mode turns every "\n" into "\r\n" on write and every
# "\r\n" back into "\n" on read, which round-trips a plain string by luck and MANGLES one that already
# contains CRLF: "a\r\nb" is stored as "a\r\r\nb" and reads back as "a\n\nb". Caught 2026-09-16 by the
# verification pass over 201 real job folders (3 of them held CRLF in a long string); the file name is the
# sha1 of the true text, so that same pass could prove the repair exact.
def write_blob(text: str, where: Path) -> str:
    """The sha1 of `text`, with the blob on disk. Content-addressed, so an identical string is written once."""
    sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
    where.mkdir(parents=True, exist_ok=True)
    p = where / f"{sha}.txt"
    if not p.exists():
        tmp = p.with_suffix(".txt.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, p)
    return sha


def read_blob(sha: str, where: Path) -> str | None:
    try:
        with open(Path(where) / f"{sha}.txt", encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        return None  # a blob the caller pruned: the record still reads, the long string is simply gone


def repair_blobs(root: Path, apply: bool = False) -> dict:
    """Undo the newline translation in blobs written before write_blob used newline="".

    Those blobs hold the original text with every "\\n" expanded to "\\r\\n", so collapsing "\\r\\n" back to
    "\\n" is the exact inverse. It is verifiable rather than merely plausible: the file name is the sha1 of
    the original text, so a repair is accepted only when the result hashes to its own name. A blob that
    already matches its name is left alone, which makes this safe to run twice."""
    stats = {"checked": 0, "already_ok": 0, "repaired": 0, "unrecoverable": []}
    for p in sorted(Path(root).rglob(f"{BLOBS}/*.txt")):
        stats["checked"] += 1
        raw = p.read_bytes()
        want = p.stem
        if hashlib.sha1(raw).hexdigest() == want:
            stats["already_ok"] += 1
            continue
        fixed = raw.decode("utf-8", "surrogatepass").replace("\r\n", "\n")
        if hashlib.sha1(fixed.encode("utf-8")).hexdigest() != want:
            stats["unrecoverable"].append(str(p))
            continue
        if apply:
            tmp = p.with_suffix(".txt.tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(fixed)
            os.replace(tmp, p)
        stats["repaired"] += 1
    return stats


def pack(record: dict, where: Path, min_bytes: int = MIN_BYTES) -> dict:
    """A copy of `record` with every long top-level string moved to a blob. The original is not touched."""
    out = {}
    for k, v in record.items():
        if isinstance(v, str) and len(v.encode("utf-8")) >= min_bytes:
            out[k + SUFFIX] = write_blob(v, where)
        else:
            out[k] = v
    return out


def expand_with(record: dict, reader) -> dict:
    """The inverse of pack(), against any source of blobs: `reader(sha) -> str | None`. A job that has been
    archived reads its blobs out of the zip, so nothing above this module cares where the job lives."""
    out = {}
    for k, v in record.items():
        if k.endswith(SUFFIX) and isinstance(v, str):
            out[k[: -len(SUFFIX)]] = reader(v)
        else:
            out[k] = v
    return out


def expand(record: dict, where: Path) -> dict:
    """The inverse of pack(): every `<field>_ref` read back into `<field>`. A missing blob yields None."""
    return expand_with(record, lambda sha: read_blob(sha, where))


def expand_all_with(records: list, reader) -> list:
    return [expand_with(r, reader) if isinstance(r, dict) else r for r in records]


def expand_any_with(doc, reader):
    if isinstance(doc, list):
        return expand_all_with(doc, reader)
    return expand_with(doc, reader) if isinstance(doc, dict) else doc


def pack_all(records: list, where: Path, min_bytes: int = MIN_BYTES) -> list:
    """Anything that is not a dict passes through untouched, so a transcript shape we did not anticipate is
    carried, never filtered away."""
    return [pack(r, where, min_bytes) if isinstance(r, dict) else r for r in records]


def expand_all(records: list, where: Path) -> list:
    return [expand(r, where) if isinstance(r, dict) else r for r in records]


def pack_any(doc, where: Path, min_bytes: int = MIN_BYTES):
    """A transcript is a list of messages from the `api` backend and a single dict from `cc`. Both are packed;
    anything else is returned as it came."""
    if isinstance(doc, list):
        return pack_all(doc, where, min_bytes)
    return pack(doc, where, min_bytes) if isinstance(doc, dict) else doc


def expand_any(doc, where: Path):
    if isinstance(doc, list):
        return expand_all(doc, where)
    return expand(doc, where) if isinstance(doc, dict) else doc


def bytes_of(job_dir: Path) -> int:
    d = blob_dir(job_dir)
    return sum(f.stat().st_size for f in d.glob("*.txt")) if d.is_dir() else 0


# ---- compacting a job folder written before this existed --------------------------------------------

def _pack_job_json(job_dir: Path, where: Path, apply: bool, min_bytes: int) -> tuple[int, int, tuple[Path, str] | None, str | None]:
    """job.json's task list, packed (or merely measured). Returns (before, after, plan entry, error): a
    job.json that will not parse is the one fatal case, and it is reported, not raised."""
    jf = job_dir / "job.json"
    if not jf.is_file():
        return 0, 0, None, None
    raw = jf.read_bytes()
    try:
        doc = json.loads(raw)
    except ValueError:
        return len(raw), len(raw), None, "unreadable job.json"
    if isinstance(doc.get("tasks"), list):
        tasks = [t for t in doc["tasks"] if isinstance(t, dict)]
        doc["tasks"] = pack_all(tasks, where, min_bytes) if apply else _measure_any(tasks, min_bytes)
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    return len(raw), len(text.encode("utf-8")), (jf, text), None


def _pack_transcript(f: Path, where: Path, apply: bool, min_bytes: int) -> tuple[int, int, tuple[Path, str] | None]:
    """One transcript file, packed (or merely measured). An unparseable one is carried at its own size, never dropped."""
    raw = f.read_bytes()
    try:
        msgs = json.loads(raw)
    except ValueError:
        return len(raw), len(raw), None
    msgs = pack_any(msgs, where, min_bytes) if apply else _measure_any(msgs, min_bytes)
    text = json.dumps(msgs, indent=1, ensure_ascii=False)
    return len(raw), len(text.encode("utf-8")), (f, text)


def compact_job(job_dir: Path, apply: bool = False, min_bytes: int = MIN_BYTES) -> dict:
    """Rewrite one job folder into the packed shape. Lossless: every string moves to a blob, nothing is
    dropped. Returns {before, after, saved} in bytes; with apply=False it only measures."""
    job_dir = Path(job_dir)
    where = blob_dir(job_dir)
    plan: list[tuple[Path, str]] = []

    before, after, entry, error = _pack_job_json(job_dir, where, apply, min_bytes)
    if entry:
        plan.append(entry)
    if error:
        return {"before": before, "after": before, "saved": 0, "error": error}

    tdir = job_dir / "transcripts"
    if tdir.is_dir():
        for f in sorted(tdir.glob("*.json")):
            b, a, entry = _pack_transcript(f, where, apply, min_bytes)
            before += b
            after += a
            if entry:
                plan.append(entry)

    if apply:
        for p, text in plan:
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, p)
        after += bytes_of(job_dir)  # the blobs themselves are the real cost of the packed shape
    else:
        after += _distinct_bytes(job_dir, min_bytes)
    return {"before": before, "after": after, "saved": max(0, before - after)}


def _measure_only(record: dict, min_bytes: int) -> dict:
    """What pack() would leave behind, without writing anything."""
    return {(k + SUFFIX if isinstance(v, str) and len(v.encode("utf-8")) >= min_bytes else k):
            ("0" * 40 if isinstance(v, str) and len(v.encode("utf-8")) >= min_bytes else v) for k, v in record.items()}


def _measure_any(doc, min_bytes: int):
    if isinstance(doc, list):
        return [_measure_only(m, min_bytes) if isinstance(m, dict) else m for m in doc]
    return _measure_only(doc, min_bytes) if isinstance(doc, dict) else doc


def _distinct_bytes(job_dir: Path, min_bytes: int) -> int:
    """How much the blobs WOULD take: every distinct long string in this folder, counted once."""
    seen: dict[str, int] = {}

    def walk(records):
        for r in records:
            if not isinstance(r, dict):
                continue
            for v in r.values():
                if isinstance(v, str):
                    b = v.encode("utf-8")
                    if len(b) >= min_bytes:
                        seen[hashlib.sha1(b).hexdigest()] = len(b)

    jf = Path(job_dir) / "job.json"
    if jf.is_file():
        try:
            walk(json.loads(jf.read_text(encoding="utf-8")).get("tasks") or [])
        except (ValueError, OSError):
            pass
    tdir = Path(job_dir) / "transcripts"
    if tdir.is_dir():
        for f in tdir.glob("*.json"):
            try:
                walk(json.loads(f.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
    return sum(seen.values()) + bytes_of(job_dir)
