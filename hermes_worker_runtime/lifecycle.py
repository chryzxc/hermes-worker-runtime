"""Normalized result -> Hermes lifecycle transition (+ supervisor exit code).

Trusted logic lives here, in the runtime wrapper; external agents never get
Kanban mutation rights. Every write carries ``expected_run_id`` so a stale
supervisor cannot finish a newer run.

In pid-tracked mode failures are *not* written as transitions: the supervisor
exits with the code Hermes' crash sweep understands, so the board's retry
budget, rate-limit cooldown and breaker apply to external workers unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import hermes_api as h
from .config import Lane
from .result import Status, WorkerResult


@dataclass
class Outcome:
    exit_code: int
    action: str        # what happened on the board (for logs/tests)
    written: bool      # did we perform a lifecycle write ourselves


def apply(conn, *, lane: Lane, task_id: str, run_id: int, result: WorkerResult,
          pid_tracked: bool, log: Callable[[str], None]) -> Outcome:
    status = result.status
    summary = (result.summary or "").strip() or f"{lane.assignee} finished with status {status.value}"
    metadata = {**result.metadata, "worker_runtime": {
        "lane": lane.name, "assignee": lane.assignee, "adapter": lane.adapter,
        "status": status.value, "exit_code": result.exit_code}}
    if result.artifacts:
        metadata["artifacts"] = list(result.artifacts)

    if status is Status.CANCELLED:
        log(f"cancelled: {summary} — no lifecycle write")
        return Outcome(h.EXIT_CANCELLED, "none", False)

    if status is Status.SUCCESS:
        if lane.on_success == "review":
            return _write(log, "review", lambda: h.request_review(
                conn, task_id, run_id=run_id, summary=summary, metadata=metadata,
                reviewer=lane.reviewer))
        return _write(log, "complete", lambda: h.complete(
            conn, task_id, run_id=run_id, summary=summary, metadata=metadata))

    if status is Status.NEEDS_REVIEW:
        return _write(log, "review", lambda: h.request_review(
            conn, task_id, run_id=run_id, summary=summary, metadata=metadata,
            reviewer=lane.reviewer))

    if status is Status.BLOCKED:
        return _write(log, "block", lambda: h.block(
            conn, task_id, run_id=run_id, reason=summary, kind="needs_input"))

    if status in (Status.FAILED, Status.TIMED_OUT) and lane.on_failure == "block":
        return _write(log, "block", lambda: h.block(
            conn, task_id, run_id=run_id, reason=_reason(status, summary), kind="needs_input"))

    code, kind = {
        Status.FAILED: (h.EXIT_FAILED, "transient"),
        Status.TIMED_OUT: (h.EXIT_FAILED, "transient"),
        Status.RATE_LIMITED: (h.EXIT_RATE_LIMITED, "transient"),
        Status.UNAVAILABLE: (h.EXIT_TERMINAL, "capability"),
    }[status]
    if pid_tracked:
        # Hermes' crash sweep books this exit (failure budget / quota requeue /
        # breaker) and quotes the last log line, so make that line the reason.
        log(_reason(status, summary))
        return Outcome(code, f"exit:{code}", False)
    return _write(log, "block", lambda: h.block(
        conn, task_id, run_id=run_id, reason=_reason(status, summary), kind=kind))


def _reason(status: Status, summary: str) -> str:
    return f"[{status.value}] {summary}"


def _write(log, action: str, fn) -> Outcome:
    try:
        ok = bool(fn())
    except Exception as exc:  # EmptyCompletion / ArtifactPreservation / LiveClaim ...
        log(f"lifecycle {action} raised {type(exc).__name__}: {exc}")
        return Outcome(h.EXIT_FAILED, f"{action}:error", False)
    if not ok:
        log(f"lifecycle {action} refused (ownership changed or card moved)")
        return Outcome(h.EXIT_FAILED, f"{action}:refused", False)
    log(f"lifecycle: {action}")
    return Outcome(0, action, True)


def exit_code_for_unowned() -> int:
    return h.EXIT_CANCELLED


def describe(outcome: Optional[Outcome]) -> str:
    return "none" if outcome is None else f"{outcome.action} (exit {outcome.exit_code})"
