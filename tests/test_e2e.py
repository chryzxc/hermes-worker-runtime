"""Scheduler -> detached supervisor -> worker -> Hermes, end to end."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from hermes_worker_runtime import hermes_api as h, paths
from hermes_worker_runtime.config import parse_config
from hermes_worker_runtime.scheduler import DaemonLockError, Scheduler, daemon_lock

from conftest import make_task, wait_for

FAKE_AGENT = str(Path(__file__).with_name("fake_acp_agent.py"))
PY = sys.executable


def config(**lanes):
    return parse_config({"lanes": lanes, "heartbeat_interval_seconds": 0.5,
                         "worker_heartbeat_interval_seconds": 1, "poll_interval_seconds": 0.2})


def cmd(assignee, argv, **kw):
    return {"assignee": assignee, "adapter": "command", "command": argv, **kw}


def acp(assignee, mode, **kw):
    env = kw.pop("env", {})
    env.setdefault("set", {})["FAKE_ACP_MODE"] = mode
    return {"assignee": assignee, "adapter": "acp", "agent_command": [PY, FAKE_AGENT],
            "env": env, **kw}


def status_of(conn, tid):
    return h.get_task(conn, tid).status


def settle(conn, tid, *, not_in=("ready", "running"), timeout=30):
    return wait_for(lambda: (s := status_of(conn, tid)) not in not_in and s, timeout)


def log_text(tid):
    return h.worker_log_path(tid, board=None).read_text()


def wait_supervisor_exit(tid, timeout=30):
    return wait_for(lambda: "[kanban-worker-exit] rc=" in log_text(tid), timeout)


def trailer(tid):
    return [ln for ln in log_text(tid).splitlines() if ln.startswith("[kanban-worker-exit]")][-1]


# -- command lanes ---------------------------------------------------------------------

def test_command_success_completes(board):
    sched = Scheduler(config(t=cmd("wr:tests", [PY, "-c", "print('ran fine')"])))
    tid = make_task(board, assignee="wr:tests")
    res = sched.tick()
    assert [c[1] for c in res.claimed] == [tid]
    task = h.get_task(board, tid)
    assert task.worker_pid and task.claim_lock.startswith(h.new_claimer().split(":")[0] + ":")
    assert settle(board, tid) == "done"
    wait_supervisor_exit(tid)
    assert "ran fine" in log_text(tid) and trailer(tid).endswith("rc=0")


def test_command_success_to_review(board):
    sched = Scheduler(config(t=cmd("wr:tests", ["true"], on_success="review")))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    assert settle(board, tid) == "review"


def test_command_failure_blocks_with_output(board):
    sched = Scheduler(config(t=cmd("wr:tests", [PY, "-c",
                                                "import sys; print('3 failed'); sys.exit(2)"])))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    assert settle(board, tid) == "blocked"
    wait_supervisor_exit(tid)
    events = h.kb.list_events(board, tid)
    assert any("exited with code 2" in str(e.payload) for e in events)


def test_command_failure_fail_mode_uses_hermes_crash_accounting(board):
    sched = Scheduler(config(t=cmd("wr:tests", ["false"], on_failure="fail")))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    wait_supervisor_exit(tid)
    assert trailer(tid).endswith("rc=1")
    assert status_of(board, tid) == "running"   # we wrote nothing
    wait_for(lambda: (h.kbd.detect_crashed_workers(board) or True)
             and status_of(board, tid) != "running", 15)
    task = h.get_task(board, tid)
    assert task.status in ("ready", "blocked") and task.claim_lock is None


def test_secrets_are_not_inherited_and_are_redacted(board, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "tok-" + "q" * 24)
    monkeypatch.setenv("PASSED_API_KEY", "abcdef1234567890")
    script = ("import os; print('inherited', os.environ.get('SUPER_SECRET_TOKEN')); "
              "print('passed', os.environ['PASSED_API_KEY']); "
              "print('kanban', [k for k in os.environ if k.startswith('HERMES_KANBAN')])")
    sched = Scheduler(config(t=cmd("wr:tests", [PY, "-c", script],
                                   env={"pass": ["PASSED_API_KEY"]})))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    assert settle(board, tid) == "done"
    text = log_text(tid)
    assert "inherited None" in text and "kanban []" in text
    assert "abcdef1234567890" not in text and "passed [REDACTED]" in text


def test_timeout_kills_worker_and_blocks(board, tmp_path):
    pidfile = tmp_path / "child.pid"
    script = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(120)"
    sched = Scheduler(config(t=cmd("wr:tests", [PY, "-c", script], timeout_seconds=2,
                                   kill_grace_seconds=1)))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    assert settle(board, tid) == "blocked"
    child = int(pidfile.read_text())
    wait_for(lambda: not _alive(child), 10)
    assert "timed_out" in log_text(tid)


def test_unknown_or_unconfigured_assignees_are_never_claimed(board):
    sched = Scheduler(config(t=cmd("wr:tests", ["true"])))
    ids = [make_task(board, assignee=a) for a in ("wr:unknown", "coder", "wr:TESTS-x")]
    res = sched.tick()
    assert res.claimed == []
    assert [status_of(board, i) for i in ids] == ["ready"] * 3


def test_concurrency_cap(board):
    sched = Scheduler(config(t=cmd("wr:tests", ["sleep", "3"], concurrency=1)))
    a = make_task(board, assignee="wr:tests")
    b = make_task(board, assignee="wr:tests")
    first = sched.tick()
    assert len(first.claimed) == 1 and len(first.capped) == 1
    second = sched.tick()
    assert second.claimed == []            # still capped while the first runs
    settle(board, first.claimed[0][1])
    wait_for(lambda: sched.tick().claimed, 10)
    assert settle(board, a) == settle(board, b) == "done"


def test_hermes_reclaim_kills_worker_tree_without_writes(board, tmp_path):
    pidfile = tmp_path / "gc.pid"
    script = (f"import subprocess,time; p=subprocess.Popen(['sleep','120']); "
              f"open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(120)")
    sched = Scheduler(config(t=cmd("wr:tests", [PY, "-c", script])))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    wait_for(lambda: pidfile.exists() and pidfile.read_text(), 15)
    grandchild = int(pidfile.read_text())
    run_before = h.get_task(board, tid).current_run_id
    assert h.kb.reclaim_task(board, tid, reason="operator test")   # SIGTERMs worker_pid
    wait_supervisor_exit(tid, 15)
    wait_for(lambda: not _alive(grandchild), 10)
    assert trailer(tid).endswith("rc=143")
    task = h.get_task(board, tid)
    assert task.status == "ready" and task.claim_lock is None
    runs = h.kb.list_runs(board, tid)
    assert [r for r in runs if r.id == run_before][0].outcome not in ("completed", "blocked")
    assert list(paths.runs_dir().glob("*.json")) == []


def test_ownership_loss_stops_worker(board, tmp_path):
    """Claim moved out from under a live supervisor (no signal): heartbeat notices."""
    pidfile = tmp_path / "child.pid"
    script = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(120)"
    sched = Scheduler(config(t=cmd("wr:tests", [PY, "-c", script], kill_grace_seconds=1)))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    wait_for(lambda: pidfile.exists() and pidfile.read_text(), 15)
    child = int(pidfile.read_text())
    assert h.kb.reclaim_task(board, tid, reason="t", signal_fn=lambda pid, sig: None)
    wait_supervisor_exit(tid, 15)
    wait_for(lambda: not _alive(child), 10)
    assert "claim lost" in log_text(tid)
    assert status_of(board, tid) == "ready"


def test_orphan_sweep_after_supervisor_sigkill(board, tmp_path):
    pidfile = tmp_path / "child.pid"
    script = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(120)"
    sched = Scheduler(config(t=cmd("wr:tests", [PY, "-c", script])))
    tid = make_task(board, assignee="wr:tests")
    sched.tick()
    wait_for(lambda: pidfile.exists() and pidfile.read_text(), 15)
    wait_for(lambda: list(paths.runs_dir().glob("*.json")), 5)
    child = int(pidfile.read_text())
    os.kill(h.get_task(board, tid).worker_pid, signal.SIGKILL)
    sched.reap()
    swept = wait_for(lambda: sched.tick().swept_orphans, 10)
    assert swept and not _alive(child)


def test_daemon_lock_is_exclusive(hermes_home):
    with daemon_lock():
        with pytest.raises(DaemonLockError):
            with daemon_lock():
                pass
    with daemon_lock():
        pass


# -- ACP lanes -------------------------------------------------------------------------

def test_acp_structured_result_goes_to_review_by_default(board):
    sched = Scheduler(config(c=acp("wr:fake", "result")))
    tid = make_task(board, assignee="wr:fake", title="Fix the bug", body="details here")
    sched.tick()
    assert settle(board, tid) == "review"
    wait_supervisor_exit(tid)
    text = log_text(tid)
    assert "stop_reason=end_turn" in text and trailer(tid).endswith("rc=0")


def test_acp_success_can_complete(board):
    sched = Scheduler(config(c=acp("wr:fake", "result", on_success="complete")))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    assert settle(board, tid) == "done"
    assert "did it in" in h.get_task(board, tid).result


def test_acp_prose_without_block_needs_review(board):
    sched = Scheduler(config(c=acp("wr:fake", "prose", on_success="complete")))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    assert settle(board, tid) == "review"


def test_acp_blocked_status(board):
    sched = Scheduler(config(c=acp("wr:fake", "result", env={"set": {"FAKE_ACP_STATUS": "blocked"}})))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    assert settle(board, tid) == "blocked"


@pytest.mark.parametrize("policy, chosen", [("allow", "yes"), ("deny", "no")])
def test_acp_permission_policy(board, policy, chosen):
    sched = Scheduler(config(c=acp("wr:fake", "permission", permission_policy=policy,
                                   on_success="complete")))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    assert settle(board, tid) == "done"
    assert h.get_task(board, tid).result == f"permission={chosen}"


def test_acp_child_env_is_filtered(board, monkeypatch):
    monkeypatch.setenv("LEAKY_SECRET", "nope")
    sched = Scheduler(config(c=acp("wr:fake", "env", on_success="complete")))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    assert settle(board, tid) == "done"
    names = set(h.get_task(board, tid).result.split(","))
    assert "LEAKY_SECRET" not in names and not any(n.startswith("HERMES_KANBAN") for n in names)
    assert {"WR_TASK_ID", "WR_WORKSPACE", "FAKE_ACP_MODE"} <= names


def test_acp_auth_failure_is_terminal(board):
    sched = Scheduler(config(c=acp("wr:fake", "auth")))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    wait_supervisor_exit(tid)
    assert trailer(tid).endswith("rc=78")
    assert "authentication failed" in log_text(tid)


def test_acp_timeout_kills_agent(board):
    sched = Scheduler(config(c=acp("wr:fake", "hang", timeout_seconds=2, kill_grace_seconds=1,
                                   on_failure="block")))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    assert settle(board, tid) == "blocked"
    wait_supervisor_exit(tid)
    assert "timed_out" in log_text(tid)


def test_acp_missing_executable_is_unavailable(board):
    sched = Scheduler(config(c={"assignee": "wr:fake", "adapter": "acp",
                                "agent_command": ["definitely-not-an-agent-xyz"]}))
    tid = make_task(board, assignee="wr:fake")
    sched.tick()
    wait_supervisor_exit(tid)
    assert trailer(tid).endswith("rc=78")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    import subprocess
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return stat.returncode == 0 and not stat.stdout.strip().startswith("Z")
