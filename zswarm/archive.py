"""Cold jobs become one compressed file each; hot jobs stay as folders.

Measured 2026-09-16 after the blob rewrite: 115 MiB of job records in 14,665 files, plus 30 MiB of
filesystem slack because most of those files are smaller than a 4 KiB cluster. Everything in them is text
and compresses hard (job.json to 6%, transcripts to 15%, blobs to 28% under LZMA), so a finished job is
worth about a fifth of its size and a single file instead of seventy.

The split is by age, not by importance (owner ask, Michael, 2026-09-16: "have the compression auto-apply
after 24 hours or 6 hours"). A job younger than the window stays a plain folder: it is cheap to write, easy
to look at, and may still be read while the work is fresh. Past the window a job never changes again, so it
is packed into `<job-id>.zip` and the folder is removed. Every reader here checks the folder first and the
archive second, so nothing above this module knows the difference.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import zipfile
from pathlib import Path

from . import blobs, config

SUFFIX = ".zip"
DEFAULT_HOURS = 24
# LZMA: stdlib, and measured on this data at roughly half the size of deflate for the JSON members.
COMPRESSION = zipfile.ZIP_LZMA


def archive_path(job_id: str) -> Path:
    return config.JOBS_DIR / f"{job_id}{SUFFIX}"


def job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def is_archived(job_id: str) -> bool:
    return archive_path(job_id).is_file()


def job_ids(limit: int | None = None) -> list[str]:
    """Every job on disk, newest first, folders and archives alike (the id sorts chronologically)."""
    if not config.JOBS_DIR.exists():
        return []
    ids = set()
    for p in config.JOBS_DIR.iterdir():
        if p.is_dir():
            ids.add(p.name)
        elif p.suffix == SUFFIX:
            ids.add(p.stem)
    out = sorted(ids, reverse=True)
    return out[:limit] if limit else out


def read_text(job_id: str, member: str) -> str | None:
    """A file from a job, whether it is a folder or an archive. `member` is a posix path like
    "transcripts/t1.json". None when it is not there."""
    p = job_dir(job_id) / member
    if p.is_file():
        try:
            with open(p, encoding="utf-8", newline="") as f:
                return f.read()
        except OSError:
            return None
    z = archive_path(job_id)
    if not z.is_file():
        return None
    try:
        with zipfile.ZipFile(z) as zf:
            return zf.read(member).decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


def read_json(job_id: str, member: str):
    raw = read_text(job_id, member)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def members(job_id: str, prefix: str = "") -> list[str]:
    """Member names under `prefix`, from the folder or the archive."""
    d = job_dir(job_id)
    if d.is_dir():
        base = d / prefix if prefix else d
        if not base.is_dir():
            return []
        return sorted((f.relative_to(d).as_posix()) for f in base.iterdir() if f.is_file())
    z = archive_path(job_id)
    if not z.is_file():
        return []
    try:
        with zipfile.ZipFile(z) as zf:
            return sorted(n for n in zf.namelist() if n.startswith(prefix) and not n.endswith("/"))
    except (OSError, zipfile.BadZipFile):
        return []


def blob_reader(job_id: str):
    """A reader for blobs.expand_with that works against a folder or an archive, caching what it opens."""
    cache: dict[str, str | None] = {}

    def read(sha: str) -> str | None:
        if sha not in cache:
            cache[sha] = read_text(job_id, f"{blobs.BLOBS}/{sha}.txt")
        return cache[sha]

    return read


def transcript(job_id: str, task_id: str):
    """One task's transcript with its blobs expanded, from wherever the job lives. None when absent."""
    doc = read_json(job_id, f"transcripts/{task_id}.json")
    return None if doc is None else blobs.expand_any_with(doc, blob_reader(job_id))


# ---- packing -------------------------------------------------------------------------------------

def older_than(hours: float) -> list[Path]:
    """Job FOLDERS whose id is older than `hours`. The id is a UTC timestamp, so it is the age test."""
    if not config.JOBS_DIR.exists():
        return []
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(0.0, hours))).strftime("%Y%m%d-%H%M%S")
    return [d for d in sorted(config.JOBS_DIR.iterdir()) if d.is_dir() and d.name < cutoff]


def archive_job(d: Path, apply: bool = False) -> dict:
    """Pack one job folder into `<id>.zip` and remove the folder. Returns {before, after, saved} in bytes.

    The archive is written to a temporary name and moved into place, and the folder is only deleted once
    every member has been read back from the finished archive, so an interrupted run leaves either the
    folder or a complete archive, never a half of each."""
    d = Path(d)
    files = [f for f in sorted(d.rglob("*")) if f.is_file()]
    before = sum(f.stat().st_size for f in files)
    if not files:
        return {"before": 0, "after": 0, "saved": 0}
    if not apply:  # measure honestly: compress into memory, keep nothing
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", COMPRESSION) as zf:
            for f in files:
                zf.write(f, f.relative_to(d).as_posix())
        after = buf.tell()
        return {"before": before, "after": after, "saved": max(0, before - after)}

    final = archive_path(d.name)
    tmp = final.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", COMPRESSION) as zf:
        for f in files:
            zf.write(f, f.relative_to(d).as_posix())
    with zipfile.ZipFile(tmp) as zf:
        if zf.testzip() is not None or len(zf.namelist()) != len(files):
            tmp.unlink(missing_ok=True)
            return {"before": before, "after": before, "saved": 0, "error": f"archive of {d.name} did not verify"}
    os.replace(tmp, final)
    shutil.rmtree(d, ignore_errors=True)
    after = final.stat().st_size
    return {"before": before, "after": after, "saved": max(0, before - after)}


def archive_old(hours: float = DEFAULT_HOURS, apply: bool = False) -> dict:
    """Pack every job folder past the window. Dry run reports what it would save."""
    out = {"jobs": 0, "before": 0, "after": 0, "saved": 0, "errors": []}
    for d in older_than(hours):
        r = archive_job(d, apply)
        if not r["before"]:
            continue
        out["jobs"] += 1
        for k in ("before", "after", "saved"):
            out[k] += r[k]
        if r.get("error"):
            out["errors"].append(r["error"])
    return out


def unpack(job_id: str) -> Path | None:
    """Put an archived job back as a folder (for poking at it by hand). The archive is left in place."""
    z = archive_path(job_id)
    if not z.is_file():
        return job_dir(job_id) if job_dir(job_id).is_dir() else None
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(d)
    return d
