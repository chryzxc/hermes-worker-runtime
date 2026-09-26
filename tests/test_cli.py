import importlib.util
import json

import pytest

from hermes_worker_runtime import cli

needs_hermes = pytest.mark.skipif(importlib.util.find_spec("hermes_cli") is None,
                                  reason="needs a Hermes Agent checkout")


def write(home, text):
    path = home / "worker-runtime.yaml"
    path.write_text(text)
    return path


def test_config_error_exits_2(hermes_home, capsys):
    write(hermes_home, "lanes:\n  x:\n    assignee: codex\n    adapter: command\n    command: [true]\n")
    assert cli.main(["lanes"]) == 2
    assert "config error" in capsys.readouterr().err


@needs_hermes
def test_lanes_and_doctor(hermes_home, capsys):
    write(hermes_home, "lanes:\n  tests:\n    assignee: wr:tests\n    adapter: command\n"
                       "    command: [\"true\"]\n  ghost:\n    assignee: wr:ghost\n"
                       "    adapter: command\n    command: [no-such-binary-xyz]\n")
    assert cli.main(["lanes"]) == 0
    lanes = json.loads(capsys.readouterr().out)
    assert set(lanes) == {"wr:tests", "wr:ghost"}
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "pid-tracked" in out and "UNAVAILABLE: executable not found" in out


def test_plugin_registers_cli_command():
    import hermes_worker_runtime as pkg

    class Ctx:
        def __init__(self):
            self.calls = []

        def register_cli_command(self, **kw):
            self.calls.append(kw)

    ctx = Ctx()
    pkg.register(ctx)
    assert ctx.calls[0]["name"] == "worker-runtime"


def test_daemon_logs_reach_stderr_when_host_configured_logging(hermes_home, capsys):
    # Hermes configures the root logger before running plugin commands, which
    # makes logging.basicConfig a no-op; daemon logs must still reach stderr.
    import logging
    root = logging.getLogger()
    host = logging.NullHandler()
    root.addHandler(host)
    try:
        write(hermes_home, "lanes:\n  t:\n    assignee: wr:t\n    adapter: command\n"
                           "    command: [\"true\"]\n")
        cli.main(["lanes"])
        cli.main(["lanes"])  # handler is installed once
        logging.getLogger("hermes_worker_runtime").info("hello-daemon-log")
        assert capsys.readouterr().err.count("hello-daemon-log") == 1
    finally:
        root.removeHandler(host)
