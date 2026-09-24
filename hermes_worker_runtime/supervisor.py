"""Per-task supervisor process.

Spawned detached by the scheduler right after a successful claim and recorded
as the task's ``worker_pid``. It owns exactly one run:

* resolves the Hermes workspace and launches the adapter inside it;
* keeps the claim alive (``heartbeat_claim``) and stops the child the moment
  ownership is lost;
* enforces the lane timeout;
* on SIGTERM (Hermes reclaim / max-runtime) kills the child's whole process
  group and exits WITHOUT a lifecycle write;
* maps the normalized result onto a Hermes transition or exit code.

    python -m hermes_worker_runtime.supervisor --board B --task T --run-id R \
        --claimer LOCK --lane-json '{...}'
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import gitstate, hermes_api as h, lifecycle, paths, procutil
from .adapters import LaunchContext, create_adapter
from .config import Lane
from .envpolicy import Redactor, build_child_env
from .result import Status, WorkerResult


class TaskLog:
    """Append-only, redacting writer into the Hermes per-task worker log."""

    def __init__(self, path: Path):
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self.redact = Redactor()

    def __call__(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            for part in (self.redact(str(line)).splitlines() or [""]):
                self._fh.write(f"[wr {stamp}] {part}\n")

    def trailer(self, code: int) -> None:
        with self._lock:
            self._fh.write(f"{h.EXIT_TRAILER}{int(code)}\n")
            self._fh.flush()


class Supervisor:
    def __init__(self, *, board: Optional[str], task_id: str, run_id: int, claimer: str,
                 lane: Lane, heartbeat_interval: float = 30.0,
                 worker_heartbeat_interval: float = 300.0, claim_ttl: Optional[int] = None,
                 poll_interval: float = 0.5):
        self.board, self.task_id, self.run_id, self.claimer = board, task_id, run_id, claimer
        self.lane = lane
        self.heartbeat_interval = heartbeat_interval
        self.worker_heartbeat_interval = worker_heartbeat_interval
        self.claim_ttl = claim_ttl
        self.poll_interval = poll_interval
        self._terminate: Optional[str] = None

    def request_termination(self, reason: str) -> None:
        self._terminate = self._terminate or reason

    def run(self) -> int:
        log = TaskLog(h.worker_log_path(self.task_id, board=self.board))
        conn = h.connect(self.board)
        log(f"worker-runtime supervisor pid={_pid()} task={self.task_id} run={self.run_id} "
            f"lane={self.lane.name} ({self.lane.assignee}, adapter={self.lane.adapter}) "
            f"mode={'pid-tracked' if h.pid_tracking_supported() else 'claim-only'}")
        try:
            if not h.still_owner(conn, self.task_id, claimer=self.claimer, run_id=self.run_id):
                log("claim no longer owned at startup — exiting without writes")
                code = lifecycle.exit_code_for_unowned()
            else:
                result = self._execute(conn, log)
                outcome = lifecycle.apply(conn, lane=self.lane, task_id=self.task_id,
                                          run_id=self.run_id, result=result,
                                          pid_tracked=h.pid_tracking_supported(), log=log)
                code = outcome.exit_code
        except Exception as exc:  # never die silently: Hermes needs the trailer
            log(f"supervisor error: {type(exc).__name__}: {exc}")
            code = h.EXIT_FAILED
        log.trailer(code)
        try:
            conn.close()
        except Exception:
            pass
        return code

    # Hermes SIGKILLs a reclaimed worker ~5 s after SIGTERM; the child group must
    # be gone before that or it would outlive its supervisor.
    SIGNAL_GRACE_SECONDS = 3.0

    # ------------------------------------------------------------------------------
    def _execute(self, conn, log: TaskLog) -> WorkerResult:
        adapter = create_adapter(self.lane)
        ok, why = adapter.available()
        if not ok:
            return WorkerResult(Status.UNAVAILABLE, why)

        task = h.get_task(conn, self.task_id)
        try:
            workspace = h.resolve_workspace(conn, task, board=self.board)
        except Exception as exc:
            return WorkerResult(Status.FAILED, f"workspace: {exc}")
        before = gitstate.porcelain(workspace)

        env = build_child_env(self.lane, extra={
            "WR_TASK_ID": self.task_id, "WR_RUN_ID": str(self.run_id),
            "WR_WORKSPACE": str(workspace), "WR_LANE": self.lane.name,
        })
        log.redact = Redactor(env)
        ctx = LaunchContext(task_id=self.task_id, run_id=self.run_id, title=task.title,
                            prompt=h.worker_context(conn, self.task_id), workspace=workspace,
                            env=env, log=log)
        if self._terminate:
            return WorkerResult(Status.CANCELLED, self._terminate)
        adapter.launch(ctx)
        runfile = self._write_runfile(adapter)
        try:
            return self._supervise(conn, log, adapter, workspace, before)
        finally:
            pgid = adapter.pid()
            if pgid and procutil.group_alive(pgid):
                # Stragglers (daemons, watchers) the worker left in its group.
                log(f"cleaning up leftover processes in group {pgid}")
                procutil.terminate_group(pgid, grace_seconds=self.SIGNAL_GRACE_SECONDS)
            if runfile is not None:
                runfile.unlink(missing_ok=True)

    def _supervise(self, conn, log, adapter, workspace, before) -> WorkerResult:
        started = time.monotonic()
        next_claim_hb = started + self.heartbeat_interval
        next_worker_hb = started + min(self.worker_heartbeat_interval, 60.0)
        override: Optional[WorkerResult] = None

        while not adapter.poll():
            now = time.monotonic()
            if self._terminate:
                override = WorkerResult(Status.CANCELLED, self._terminate)
            elif now - started > self.lane.timeout_seconds:
                override = WorkerResult(Status.TIMED_OUT,
                                        f"lane timeout of {self.lane.timeout_seconds}s exceeded")
            elif not workspace.is_dir():
                override = WorkerResult(Status.FAILED, f"workspace disappeared: {workspace}")
            elif now >= next_claim_hb:
                next_claim_hb = now + self.heartbeat_interval
                if not self._keep_claim(conn):
                    override = WorkerResult(Status.CANCELLED,
                                            "claim lost (reclaimed, reassigned or expired)")
            if override is None and now >= next_worker_hb:
                next_worker_hb = now + self.worker_heartbeat_interval
                self._safe(lambda: h.heartbeat_worker(conn, self.task_id, run_id=self.run_id,
                                                      note=f"{self.lane.assignee} running"))
            if override is not None:
                log(f"stopping worker: {override.summary}")
                adapter.cancel(override.summary,
                               grace=self.SIGNAL_GRACE_SECONDS if self._terminate else None)
                break
            time.sleep(self.poll_interval)

        result = adapter.collect_result()
        if override is not None:
            result.status, result.summary = override.status, override.summary
        elif self._terminate:  # signal raced with a natural exit: still not ours to write
            result.status, result.summary = Status.CANCELLED, self._terminate
        result.metadata.update(gitstate.change_metadata(before, gitstate.porcelain(workspace)))
        result.metadata["duration_seconds"] = round(time.monotonic() - started, 1)
        log(f"result: {result.status.value}: {result.summary[:500]}")
        return result

    def _write_runfile(self, adapter) -> Optional[Path]:
        # The adapter may start its child asynchronously (ACP); wait briefly for a pid.
        for _ in range(50):
            if adapter.pid() or adapter.poll():
                break
            time.sleep(0.1)
        if not adapter.pid():
            return None
        path = paths.runs_dir() / f"{self.task_id}.{self.run_id}.json"
        try:
            path.write_text(json.dumps({"supervisor_pid": os.getpid(), "child_pgid": adapter.pid(),
                                        "task": self.task_id, "run": self.run_id,
                                        "board": self.board}), encoding="utf-8")
            return path
        except OSError:
            return None

    def _keep_claim(self, conn) -> bool:
        try:
            if not h.heartbeat_claim(conn, self.task_id, claimer=self.claimer,
                                     ttl_seconds=self.claim_ttl):
                return False
            return h.still_owner(conn, self.task_id, claimer=self.claimer, run_id=self.run_id)
        except Exception:
            # A transient DB error is not proof of lost ownership; the TTL is.
            return True

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:
            pass


def _pid() -> int:
    return os.getpid()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="hermes_worker_runtime.supervisor")
    p.add_argument("--board", default=None)
    p.add_argument("--task", required=True)
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--claimer", required=True)
    p.add_argument("--lane-json", required=True)
    p.add_argument("--heartbeat-interval", type=float, default=30.0)
    p.add_argument("--worker-heartbeat-interval", type=float, default=300.0)
    p.add_argument("--claim-ttl", type=int, default=None)
    a = p.parse_args(argv)

    sup = Supervisor(board=a.board, task_id=a.task, run_id=a.run_id, claimer=a.claimer,
                     lane=Lane.from_dict(json.loads(a.lane_json)),
                     heartbeat_interval=a.heartbeat_interval,
                     worker_heartbeat_interval=a.worker_heartbeat_interval, claim_ttl=a.claim_ttl)

    def _on_signal(signum, _frame):
        sup.request_termination(f"supervisor received {signal.Signals(signum).name} "
                                "(Hermes reclaim, max-runtime or shutdown)")

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
