import os
import subprocess
import sys
import time

from hermes_worker_runtime import gitstate, procutil


def test_terminate_group_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "gc.pid"
    script = (f"import subprocess,time; p=subprocess.Popen(['sleep','60']); "
              f"open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(60)")
    proc = procutil.popen_group([sys.executable, "-c", script])
    deadline = time.time() + 10
    while not pidfile.exists() or not pidfile.read_text():
        assert time.time() < deadline
        time.sleep(0.05)
    grandchild = int(pidfile.read_text())
    assert procutil.group_alive(proc.pid)
    procutil.terminate_group(proc.pid, grace_seconds=1, proc=proc)
    assert not procutil.group_alive(proc.pid)
    time.sleep(0.2)
    try:
        os.kill(grandchild, 0)
        alive = subprocess.run(["ps", "-o", "stat=", "-p", str(grandchild)],
                               capture_output=True, text=True).stdout.strip()
        assert alive.startswith("Z") or alive == ""
    except ProcessLookupError:
        pass


def test_terminate_group_escalates_to_sigkill(tmp_path):
    proc = procutil.popen_group([sys.executable, "-c",
                                 "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                 "print('ready', flush=True); time.sleep(60)"],
                                stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    started = time.monotonic()
    procutil.terminate_group(proc.pid, grace_seconds=0.5, proc=proc)
    assert proc.poll() is not None and time.monotonic() - started < 5


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_change_metadata(tmp_path):
    assert gitstate.porcelain(tmp_path) is None
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "init")
    (tmp_path / "dirty.txt").write_text("pre-existing")
    before = gitstate.porcelain(tmp_path)
    (tmp_path / "new.py").write_text("x = 1")
    after = gitstate.porcelain(tmp_path)
    meta = gitstate.change_metadata(before, after)
    assert meta["changed_files"] == ["new.py"]
    assert meta["preexisting_changes"] == ["dirty.txt"]
    assert gitstate.change_metadata(None, None) == {}
