"""Process-group helpers: children run as session leaders so cancellation
reaches every grandchild, not just the direct child."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Optional


def popen_group(argv, **kwargs) -> subprocess.Popen:
    return subprocess.Popen(argv, start_new_session=True, **kwargs)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def terminate_group(pgid: Optional[int], *, grace_seconds: float = 10.0,
                    proc: Optional[subprocess.Popen] = None) -> bool:
    """SIGTERM the group, wait up to ``grace_seconds``, then SIGKILL.

    Returns True once the group is gone. The direct child is reaped when
    ``proc`` is given so it doesn't linger as a zombie in the group.
    """
    if not pgid:
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return _reap(proc)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        _reap(proc, wait=0)
        if not group_alive(pgid):
            return _reap(proc)
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    for _ in range(50):
        _reap(proc, wait=0)
        if not group_alive(pgid):
            return _reap(proc)
        time.sleep(0.1)
    return not group_alive(pgid)


def _reap(proc: Optional[subprocess.Popen], wait: float = 2.0) -> bool:
    """Collect the direct child's exit status (a dead group can still hold its zombie)."""
    if proc is not None:
        try:
            if wait:
                proc.wait(timeout=wait)
            else:
                proc.poll()
        except Exception:
            pass
    return True
