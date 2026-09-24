"""Deterministic command worker: tests, lint, builds, scanners.

The argv comes only from trusted lane config — card text never reaches it.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections import deque
from typing import Optional

from .. import procutil
from ..result import Status, WorkerResult, looks_rate_limited
from .base import LaunchContext, WorkerAdapter

_TAIL_LINES = 40


class CommandAdapter(WorkerAdapter):
    def __init__(self, lane):
        super().__init__(lane)
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self._cancelled: Optional[str] = None

    def available(self) -> tuple[bool, str]:
        exe = self.lane.command[0]
        if "/" in exe and not exe.startswith("/"):
            return (True, "")  # workspace-relative script; resolved at launch
        ok = shutil.which(exe) is not None
        return (ok, "" if ok else f"executable not found: {exe}")

    def launch(self, ctx: LaunchContext) -> None:
        self._log = ctx.log
        ctx.log(f"$ {' '.join(self.lane.command)}  (cwd={ctx.workspace})")
        self._proc = procutil.popen_group(
            list(self.lane.command), cwd=str(ctx.workspace), env=ctx.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._reader = threading.Thread(target=self._pump, name="wr-cmd-output", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            self._tail.append(line)
            self._log(line)

    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def poll(self) -> bool:
        return self._proc is None or self._proc.poll() is not None

    def cancel(self, reason: str, *, grace: Optional[float] = None) -> None:
        self._cancelled = reason
        if self._proc is not None:
            procutil.terminate_group(
                self._proc.pid, proc=self._proc,
                grace_seconds=self.lane.kill_grace_seconds if grace is None else grace)

    def collect_result(self) -> WorkerResult:
        if self._reader is not None:
            self._reader.join(timeout=5)
        code = self._proc.returncode if self._proc else None
        tail = "\n".join(self._tail).strip()
        meta = {"command": list(self.lane.command), "exit_code": code, "output_tail": tail[-4000:]}
        if self._cancelled:
            return WorkerResult(Status.CANCELLED, self._cancelled, meta, exit_code=code)
        if code == 0:
            summary = f"`{' '.join(self.lane.command)}` succeeded."
            return WorkerResult(Status.SUCCESS, summary, meta, exit_code=0)
        if looks_rate_limited(tail):
            return WorkerResult(Status.RATE_LIMITED, "command hit a rate limit", meta, exit_code=code)
        last = tail.splitlines()[-1] if tail else ""
        summary = f"`{' '.join(self.lane.command)}` exited with code {code}." + (
            f" Last output: {last}" if last else "")
        return WorkerResult(Status.FAILED, summary, meta, exit_code=code)
