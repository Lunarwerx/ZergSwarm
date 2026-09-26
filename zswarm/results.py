"""Shaping a job's results for the MCP tools: compact by default, filtered by id, status or taint, and
readable from disk when the job belongs to an earlier server process."""
from __future__ import annotations

from .job import Job
from .receipts import unverified_ids
from .spec import merge_taint

CLEAN = "clean"  # the taint filter value that keeps only results with no taint letter at all


def _queue_hint(job: Job, running: list[dict], pending: list[str]) -> str:
    oldest = max((x["elapsed_s"] for x in running), default=0.0)
    return (f"{len(running)} running (oldest {oldest:.0f}s), {len(pending)} queued; "
            f"call zswarm_results('{job.id}') later or zswarm_status to poll.")


def taint_matches(taint: str | None, want: str | None) -> bool:
    """The taint filter: None keeps everything, "clean" keeps the untainted, and letters ("F", "TS") keep a result
    carrying ANY of them, so an orchestrator can pull exactly the results it must re-verify (spec.TAINTS)."""
    if not want:
        return True
    if want.strip().lower() == CLEAN:
        return not taint
    # Separators a caller may type ("F,T", "F T") are ignored; an unknown letter still raises, naming the known ones.
    return bool(set(taint or "") & set(merge_taint("".join(c for c in want.upper() if c.isalpha()))))


def _split(task_ids, results_by_id, ids: list[str] | None, status: str | None, ages: dict, max_answer_chars: int, taint: str | None = None) -> tuple[list, list, list]:
    """The finished results (answers truncated), the running ones with their age, and the queued ids."""
    results, running, pending = [], [], []
    for t in task_ids:
        r = results_by_id.get(t.id)
        if r is None or (ids and t.id not in ids) or (status and r.status != status):
            continue
        if r.status == "running":
            # Listed apart from the queue with its age, so a worker that has run for 40 minutes is visible
            # as exactly that instead of hiding among tasks that have not started.
            running.append({"id": t.id, "started": r.started, "elapsed_s": ages.get(t.id, 0.0)})
        elif r.status == "pending":
            pending.append(t.id)
        elif taint_matches(r.taint, taint):
            results.append(_compact(r.as_dict(max_answer_chars)))  # long answers are truncated with a hint, never silently cut
    return results, running, pending


def _compact(d: dict, include_receipts: bool = False) -> dict:
    """The receipt ledger stays on disk: the verdicts say what it proved, and include_receipts brings it back."""
    if not include_receipts:
        d.pop("receipts", None)
    return d


def _flag_unverified(out: dict) -> dict:
    """Name the finished tasks whose "done" is not backed by receipts, so the orchestrator re-checks those first
    (it certifies nothing about the rest)."""
    unverified = unverified_ids(out["results"])
    if unverified:
        out["unverified"] = unverified
    return out


def job_payload(job: Job, max_answer_chars: int, include_pending: bool = False, ids: list[str] | None = None, status: str | None = None,
                include_receipts: bool = False, taint: str | None = None) -> dict:
    results, running, pending = _split(job.tasks, job.results, ids, status, job.running_ages(), max_answer_chars, taint)
    if include_receipts:
        for r in results:
            r["receipts"] = job.results[r["id"]].receipts
    out = _flag_unverified({"summary": job.summary(), "results": results})
    if include_pending or running or pending:
        out["running"] = running
        out["pending"] = pending
        if running or pending:
            out["hint"] = _queue_hint(job, running, pending)
    return out


def results_from_disk(job_id: str, ids: list[str] | None, status: str | None, max_answer_chars: int, include_receipts: bool = False,
                      taint: str | None = None) -> dict:
    """A job from an earlier server process: read its record instead of the live manager. A record written before
    taints existed has no `taint` key and reads as clean."""
    d = Job.load_from_disk(job_id)
    res = [r for r in d["results"].values()
           if (not ids or r["id"] in ids) and (not status or r["status"] == status) and taint_matches(r.get("taint"), taint)]
    for r in res:
        if len(r.get("answer", "")) > max_answer_chars:
            r["answer"] = r["answer"][:max_answer_chars] + "... [truncated]"
        _compact(r, include_receipts)
    return _flag_unverified({"summary": d["summary"], "results": res})
