"""Result -> Hermes transition mapping, against the real kanban kernel."""

import pytest

from hermes_worker_runtime import hermes_api as h, lifecycle
from hermes_worker_runtime.config import parse_config
from hermes_worker_runtime.result import Status, WorkerResult

from conftest import make_task


def lane(**kw):
    body = {"assignee": "wr:t", "adapter": "command", "command": ["true"], **kw}
    return parse_config({"lanes": {"t": body}}).lanes["wr:t"]


def claimed(conn):
    tid = make_task(conn, assignee="wr:t")
    claimer = h.new_claimer()
    task = h.claim_task(conn, tid, claimer=claimer, ttl_seconds=600)
    return tid, task.current_run_id


def apply(conn, lane_, tid, run, status, summary="s", pid_tracked=True):
    lines = []
    out = lifecycle.apply(conn, lane=lane_, task_id=tid, run_id=run,
                          result=WorkerResult(status, summary, {"k": 1}),
                          pid_tracked=pid_tracked, log=lines.append)
    return out, h.get_task(conn, tid), lines


def test_success_completes_with_metadata(board):
    tid, run = claimed(board)
    out, task, _ = apply(board, lane(), tid, run, Status.SUCCESS, "all green")
    assert (out.exit_code, out.action, task.status) == (0, "complete", "done")
    assert task.result == "all green"


def test_success_to_review_when_configured(board):
    tid, run = claimed(board)
    out, task, _ = apply(board, lane(on_success="review"), tid, run, Status.SUCCESS)
    assert (out.action, task.status) == ("review", "review")


def test_needs_review_and_blocked(board):
    tid, run = claimed(board)
    assert apply(board, lane(), tid, run, Status.NEEDS_REVIEW)[1].status == "review"
    tid, run = claimed(board)
    assert apply(board, lane(), tid, run, Status.BLOCKED, "which db?")[1].status == "blocked"


@pytest.mark.parametrize("status", [Status.FAILED, Status.TIMED_OUT])
def test_failure_blocks_by_default(board, status):
    tid, run = claimed(board)
    out, task, _ = apply(board, lane(), tid, run, status, "boom")
    assert (out.exit_code, task.status) == (0, "blocked")


@pytest.mark.parametrize("status, code", [
    (Status.FAILED, 1), (Status.TIMED_OUT, 1), (Status.RATE_LIMITED, 75), (Status.UNAVAILABLE, 78)])
def test_pid_tracked_failures_exit_without_writing(board, status, code):
    tid, run = claimed(board)
    out, task, lines = apply(board, lane(on_failure="fail"), tid, run, status, "why")
    assert (out.exit_code, out.written, task.status) == (code, False, "running")
    assert lines[-1] == f"[{status.value}] why"


def test_claim_only_failures_block(board):
    tid, run = claimed(board)
    out, task, _ = apply(board, lane(on_failure="fail"), tid, run, Status.UNAVAILABLE,
                         pid_tracked=False)
    assert (out.written, task.status) == (True, "blocked")


def test_cancelled_never_writes(board):
    tid, run = claimed(board)
    out, task, _ = apply(board, lane(), tid, run, Status.CANCELLED)
    assert (out.exit_code, out.written, task.status) == (143, False, "running")


def test_stale_run_cannot_finish_newer_run(board):
    tid, run = claimed(board)
    assert h.kb.reclaim_task(board, tid, reason="test")
    newer = h.claim_task(board, tid, claimer=h.new_claimer(), ttl_seconds=600)
    assert newer.current_run_id != run
    out, task, lines = apply(board, lane(), tid, run, Status.SUCCESS)
    assert out.exit_code == 1 and not out.written
    assert task.status == "running" and task.current_run_id == newer.current_run_id
