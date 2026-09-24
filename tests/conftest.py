"""Tests run against the real Hermes kanban kernel in a throwaway HERMES_HOME.

    PYTHONPATH=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python -m pytest
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_HERMES = Path(os.environ.get("HERMES_AGENT_ROOT", Path.home() / ".hermes" / "hermes-agent"))
if (_HERMES / "hermes_cli").is_dir() and str(_HERMES) not in sys.path:
    sys.path.insert(1, str(_HERMES))


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    return home


@pytest.fixture
def board(hermes_home):
    from hermes_worker_runtime import hermes_api as h
    h.kb.init_db()
    conn = h.connect(None)
    yield conn
    conn.close()


def make_task(conn, *, assignee: str, title: str = "t", body: str = "do it", **kw) -> str:
    from hermes_cli import kanban_db as kb
    return kb.create_task(conn, title=title, body=body, assignee=assignee,
                          **kw)


def wait_for(pred, timeout: float = 30.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")
