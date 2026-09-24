"""The only module that imports Hermes internals.

Everything the runtime needs from Hermes Kanban goes through here so a Hermes
upgrade that moves a helper breaks one file, not the runtime. Public
``hermes_cli.kanban_db`` functions are used wherever they exist; the single
private helper (``_set_worker_pid``) is probed and degrades to claim-only mode.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

try:  # Sep 2026 decomposition moved these out of kanban_db (compat shims warn)
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_workspace as kbw
except ImportError:  # pragma: no cover - pre-decomposition Hermes
    kbc = kbw = kb

Task = kb.Task

# Exit codes Hermes' crash sweep understands (see kanban_db_dispatch._exit_code_kind).
EXIT_FAILED = 1
EXIT_RATE_LIMITED = int(getattr(kb, "KANBAN_RATE_LIMIT_EXIT_CODE", 75))
EXIT_TERMINAL = int(getattr(kb, "KANBAN_TERMINAL_PROVIDER_EXIT_CODE", 78))
EXIT_CANCELLED = 143

try:
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER as EXIT_TRAILER
except Exception:  # pragma: no cover - older/newer Hermes
    EXIT_TRAILER = "[kanban-worker-exit] rc="

_set_worker_pid = getattr(kbd, "_set_worker_pid", None)


def pid_tracking_supported() -> bool:
    """True when the supervisor pid can be recorded as the task's ``worker_pid``."""
    return callable(_set_worker_pid)


def connect(board: Optional[str]) -> sqlite3.Connection:
    return kbc.connect(board=board)


def board_slugs() -> list[str]:
    return [b["slug"] for b in kb.list_boards(include_archived=False)]


def new_claimer() -> str:
    """``<host>:<pid>:wr-<uuid>`` — the host prefix keeps Hermes' host-local
    crash detection and reclaim termination applicable to our supervisors."""
    host = socket.gethostname() or "unknown"
    return f"{host}:{os.getpid()}:wr-{uuid.uuid4().hex[:12]}"


def list_tasks(conn, *, status: str, assignee: Optional[str] = None) -> list[Task]:
    return kb.list_tasks(conn, status=status, assignee=assignee)


def get_task(conn, task_id: str) -> Optional[Task]:
    return kb.get_task(conn, task_id)


def claim_task(conn, task_id: str, *, claimer: str, ttl_seconds: Optional[int]) -> Optional[Task]:
    return kb.claim_task(conn, task_id, claimer=claimer, ttl_seconds=ttl_seconds)


def heartbeat_claim(conn, task_id: str, *, claimer: str, ttl_seconds: Optional[int]) -> bool:
    return kb.heartbeat_claim(conn, task_id, claimer=claimer, ttl_seconds=ttl_seconds)


def heartbeat_worker(conn, task_id: str, *, run_id: int, note: Optional[str] = None) -> bool:
    return kbd.heartbeat_worker(conn, task_id, note=note, expected_run_id=run_id)


def record_worker_pid(conn, task_id: str, pid: int) -> bool:
    if not pid_tracking_supported():
        return False
    _set_worker_pid(conn, task_id, int(pid))
    return True


def resolve_workspace(conn, task: Task, *, board: Optional[str]) -> Path:
    path = kbw.resolve_workspace(task, board=board)
    kbw.set_workspace_path(conn, task.id, str(path))
    if task.workspace_kind == "worktree" and not (task.branch_name or "").strip():
        kbw.set_branch_name(conn, task.id, f"wt/{task.id}")
    return Path(path)


def worker_context(conn, task_id: str) -> str:
    return kb.build_worker_context(conn, task_id)


def worker_log_path(task_id: str, *, board: Optional[str]) -> Path:
    path = kb.worker_log_path(task_id, board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def complete(conn, task_id: str, *, run_id: int, summary: str, metadata: dict) -> bool:
    return kb.complete_task(conn, task_id, summary=summary, result=summary,
                            metadata=metadata, expected_run_id=run_id)


def request_review(conn, task_id: str, *, run_id: int, summary: str, metadata: dict,
                   reviewer: Optional[str] = None) -> bool:
    return bool(kb.request_review(conn, task_id, summary=summary, metadata=metadata,
                                  reviewer=reviewer, expected_run_id=run_id))


def block(conn, task_id: str, *, run_id: int, reason: str, kind: str) -> bool:
    return kb.block_task(conn, task_id, reason=reason, kind=kind, expected_run_id=run_id)


def comment(conn, task_id: str, body: str, author: str = "worker-runtime") -> None:
    kb.add_comment(conn, task_id, author, body)


def still_owner(conn, task_id: str, *, claimer: str, run_id: int) -> bool:
    task = kb.get_task(conn, task_id)
    return bool(task and task.status == "running" and task.claim_lock == claimer
                and task.current_run_id == run_id)


def running_counts(conn, assignees: Iterable[str]) -> dict[str, int]:
    return {a: len(kb.list_tasks(conn, status="running", assignee=a)) for a in assignees}


def describe() -> dict[str, Any]:
    return {
        "hermes_kanban_db": getattr(kb, "__file__", "?"),
        "mode": "pid-tracked" if pid_tracking_supported() else "claim-only",
        "exit_codes": {"failed": EXIT_FAILED, "rate_limited": EXIT_RATE_LIMITED,
                       "terminal": EXIT_TERMINAL},
    }
