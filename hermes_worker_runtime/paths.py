"""Runtime-owned files (never task state — that lives in Hermes)."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_home() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    path = Path(home).expanduser() / "worker-runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runs_dir() -> Path:
    """One JSON file per live supervisor: ``{supervisor_pid, child_pgid, task, run}``.
    Used only to sweep orphaned child process groups after a supervisor is SIGKILLed."""
    path = runtime_home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def daemon_lock_path() -> Path:
    return runtime_home() / "daemon.lock"
