"""Discovery + claim + supervisor spawn. Not an orchestrator.

The scheduler keeps no task state: per-lane concurrency is recomputed from the
Hermes DB every tick, so a restarted daemon picks up exactly where the board
says things are. Supervisors run in their own sessions and survive a daemon
restart.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import hermes_api as h
from . import paths, procutil
from .config import Lane, RuntimeConfig

log = logging.getLogger("hermes_worker_runtime")


@dataclass
class TickResult:
    claimed: list[tuple[str, str, int]] = field(default_factory=list)   # (board, task, run)
    capped: list[str] = field(default_factory=list)
    lost_race: list[str] = field(default_factory=list)
    spawn_failed: list[str] = field(default_factory=list)
    swept_orphans: list[str] = field(default_factory=list)


class DaemonLockError(RuntimeError):
    pass


class Scheduler:
    def __init__(self, config: RuntimeConfig, *, python: Optional[str] = None):
        self.config = config
        self.python = python or sys.executable
        self._children: dict[int, subprocess.Popen] = {}

    # -- one tick ---------------------------------------------------------------------
    def tick(self) -> TickResult:
        result = TickResult()
        self.reap()
        result.swept_orphans = sweep_orphans()
        for board in self._boards():
            try:
                self._tick_board(board, result)
            except Exception as exc:
                log.warning("board %s: tick failed: %s", board, exc)
        return result

    def _boards(self) -> list[str]:
        if self.config.boards is not None:
            return list(self.config.boards)
        return h.board_slugs()

    def _tick_board(self, board: str, result: TickResult) -> None:
        conn = h.connect(board)
        try:
            lanes = self.config.lanes
            running = h.running_counts(conn, lanes.keys())
            for assignee, lane in lanes.items():
                ready = h.list_tasks(conn, status="ready", assignee=assignee)
                for task in ready:
                    if running[assignee] >= lane.concurrency:
                        result.capped.append(task.id)
                        continue
                    if task.claim_lock:
                        continue
                    claimer = h.new_claimer()
                    claimed = h.claim_task(conn, task.id, claimer=claimer,
                                           ttl_seconds=self.config.claim_ttl_seconds)
                    if claimed is None:
                        result.lost_race.append(task.id)
                        continue
                    run_id = claimed.current_run_id
                    try:
                        proc = self._spawn(board, claimed.id, run_id, claimer, lane)
                    except Exception as exc:
                        log.error("task %s: supervisor spawn failed: %s", task.id, exc)
                        self._release_failed_spawn(conn, claimed.id, run_id, exc)
                        result.spawn_failed.append(task.id)
                        continue
                    h.record_worker_pid(conn, claimed.id, proc.pid)
                    running[assignee] += 1
                    result.claimed.append((board, claimed.id, run_id))
                    log.info("board %s: claimed %s (run %s) for %s → supervisor pid %s",
                             board, claimed.id, run_id, assignee, proc.pid)
        finally:
            conn.close()

    def _spawn(self, board: str, task_id: str, run_id: int, claimer: str,
               lane: Lane) -> subprocess.Popen:
        argv = [self.python, "-m", "hermes_worker_runtime.supervisor",
                "--board", board, "--task", task_id, "--run-id", str(run_id),
                "--claimer", claimer, "--lane-json", json.dumps(lane.to_dict()),
                "--heartbeat-interval", str(self.config.heartbeat_interval_seconds),
                "--worker-heartbeat-interval", str(self.config.worker_heartbeat_interval_seconds)]
        if self.config.claim_ttl_seconds:
            argv += ["--claim-ttl", str(self.config.claim_ttl_seconds)]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (_package_root(), _hermes_root(), env.get("PYTHONPATH", "")) if p)
        # stderr catches anything the supervisor fails to log itself (import errors).
        with open(h.worker_log_path(task_id, board=board), "a") as stderr:
            proc = subprocess.Popen(
                argv, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=stderr, close_fds=True,
            )
        self._children[proc.pid] = proc
        return proc

    def _release_failed_spawn(self, conn, task_id: str, run_id: int, exc: Exception) -> None:
        try:
            h.block(conn, task_id, run_id=run_id, kind="transient",
                    reason=f"worker-runtime could not start a supervisor: {exc}")
        except Exception as inner:  # TTL expiry is the backstop
            log.error("task %s: could not release claim after spawn failure: %s", task_id, inner)

    def reap(self) -> None:
        for pid, proc in list(self._children.items()):
            if proc.poll() is not None:
                self._children.pop(pid, None)

    @property
    def active_supervisors(self) -> int:
        self.reap()
        return len(self._children)

    # -- loop -------------------------------------------------------------------------
    def run_forever(self, *, stop=lambda: False) -> None:
        with daemon_lock():
            log.info("worker-runtime daemon started (pid %s, mode %s, lanes %s)", os.getpid(),
                     h.describe()["mode"], ", ".join(self.config.lanes))
            while not stop():
                started = time.monotonic()
                try:
                    self.tick()
                except Exception as exc:  # a bad tick must never kill the daemon
                    log.exception("tick failed: %s", exc)
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, self.config.poll_interval_seconds - elapsed))


class daemon_lock:
    """One daemon per HERMES_HOME. Multiple hosts are safe via claim CAS."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.daemon_lock_path()
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            raise DaemonLockError(f"another worker-runtime daemon holds {self.path}")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()


def sweep_orphans() -> list[str]:
    """Kill child process groups whose supervisor died without cleaning up
    (e.g. SIGKILLed by Hermes after a reclaim). Returns the swept run keys."""
    swept = []
    for f in paths.runs_dir().glob("*.json"):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            sup, pgid = int(info["supervisor_pid"]), int(info["child_pgid"])
        except (OSError, ValueError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        if _pid_alive(sup):
            continue
        if procutil.group_alive(pgid):
            log.warning("sweeping orphaned worker group %s (task %s run %s)",
                        pgid, info.get("task"), info.get("run"))
            procutil.terminate_group(pgid, grace_seconds=3)
        f.unlink(missing_ok=True)
        swept.append(f.stem)
    return swept


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie is dead for our purposes
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2)
        return out.returncode == 0 and "Z" not in out.stdout
    except (OSError, subprocess.SubprocessError):
        return True


def _package_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def _hermes_root() -> str:
    import hermes_cli
    return str(Path(hermes_cli.__file__).resolve().parent.parent)
