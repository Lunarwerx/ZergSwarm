"""Offline: a job past its window becomes one compressed file, and every reader keeps working.

After the blob rewrite the job folders were still 115 MiB in 14,665 files plus 30 MiB of filesystem slack,
and all of it is text that compresses to about a fifth. A finished job never changes again, so past a few
hours it is packed into `<id>.zip` and the folder goes away (Michael, 2026-09-16)."""
from __future__ import annotations

import datetime as dt
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zswarm import archive, blobs, config  # noqa: E402
from zswarm.job import Job  # noqa: E402

LONG = "You are a swarm worker. " + ("context line, " * 400)


def _job(job_id: str, tasks: int = 3) -> Path:
    d = config.JOBS_DIR / job_id
    (d / "transcripts").mkdir(parents=True)
    where = blobs.blob_dir(d)
    doc = {"summary": {"job_id": job_id, "state": "done", "tasks": tasks, "label": "unit"},
           "caller": {"instance": "m"}, "tasks": blobs.pack_all([{"id": f"t{i}", "prompt": f"q{i}", "system": LONG} for i in range(tasks)], where),
           "results": {f"t{i}": {"id": f"t{i}", "status": "ok", "answer": f"a{i}"} for i in range(tasks)}}
    (d / "job.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    for i in range(tasks):
        msgs = blobs.pack_all([{"role": "system", "content": LONG}, {"role": "user", "content": f"q{i}"}], where)
        (d / "transcripts" / f"t{i}.json").write_text(json.dumps(msgs, indent=1), encoding="utf-8")
    (d / "results.jsonl").write_text("".join(json.dumps({"id": f"t{i}"}) + "\n" for i in range(tasks)), encoding="utf-8")
    return d


def _id(hours_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).strftime("%Y%m%d-%H%M%S") + "-aaaa"


def test_an_aged_job_packs_to_one_file_and_still_reads(tmp_path):
    old, new = _id(48), _id(0.1)
    _job(old, 4)
    _job(new, 2)
    before_doc = json.loads((config.JOBS_DIR / old / "job.json").read_text(encoding="utf-8"))
    before_tr = archive.transcript(old, "t2")
    assert before_tr[0]["content"] == LONG  # blobs expand from the folder

    dry = archive.archive_old(hours=24, apply=False)
    assert dry["jobs"] == 1 and dry["saved"] > dry["before"] * 0.5  # text compresses hard
    assert (config.JOBS_DIR / old).is_dir()  # a dry run moves nothing

    done = archive.archive_old(hours=24, apply=True)
    assert done["jobs"] == 1 and not (config.JOBS_DIR / old).exists() and archive.archive_path(old).is_file()
    assert (config.JOBS_DIR / new).is_dir()  # the fresh job is untouched

    # everything a reader can ask for still answers, out of the archive
    assert archive.read_json(old, "job.json") == before_doc
    assert archive.transcript(old, "t2") == before_tr
    assert archive.transcript(old, "t2")[0]["content"] == LONG
    assert archive.read_text(old, "results.jsonl").count("\n") == 4
    assert archive.transcript(old, "nope") is None and archive.read_json(old, "missing.json") is None
    assert sorted(archive.members(old, "transcripts/")) == [f"transcripts/t{i}.json" for i in range(4)]
    assert archive.is_archived(old) and not archive.is_archived(new)
    assert archive.job_ids() == sorted([old, new], reverse=True)

    assert Job.load_from_disk(old)["summary"]["label"] == "unit"  # the job record, via the archive
    assert {s["job_id"] for s in Job.list_on_disk(10)} == {old, new}
    assert [p.name for p, _ in Job.prunable(1)] == [old + ".zip"]  # an archive is still prunable cache


def test_archiving_is_atomic_and_never_leaves_a_half_written_job(tmp_path, monkeypatch):
    jid = _id(48)
    d = _job(jid, 2)
    files = {f.relative_to(d).as_posix() for f in d.rglob("*") if f.is_file()}

    boom = zipfile.ZipFile

    class Exploding(zipfile.ZipFile):
        def write(self, *args, **kw):  # fail midway through writing members
            if getattr(self, "_n", 0) >= 1:
                raise OSError("disk full")
            self._n = getattr(self, "_n", 0) + 1
            return boom.write(self, *args, **kw)

    monkeypatch.setattr(zipfile, "ZipFile", Exploding)
    with pytest.raises(OSError):
        archive.archive_job(d, apply=True)
    monkeypatch.setattr(zipfile, "ZipFile", boom)
    assert d.is_dir() and {f.relative_to(d).as_posix() for f in d.rglob("*") if f.is_file()} == files
    assert not archive.archive_path(jid).is_file()  # only the .tmp could exist, never the real name

    assert archive.archive_job(d, apply=True)["saved"] > 0  # and a retry succeeds
    assert archive.is_archived(jid) and not d.exists()


def test_unpack_puts_a_job_back_as_a_folder(tmp_path):
    jid = _id(48)
    _job(jid, 2)
    original = archive.transcript(jid, "t1")
    archive.archive_old(hours=24, apply=True)
    assert not (config.JOBS_DIR / jid).exists()

    out = archive.unpack(jid)
    assert out and out.is_dir() and (out / "job.json").is_file()
    assert archive.transcript(jid, "t1") == original  # reads the folder now, same content
    assert archive.unpack("no-such-job") is None
