"""``hermes worker-runtime <run|tick|lanes|doctor>`` (also ``python -m hermes_worker_runtime``)."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import sys
from pathlib import Path

from .config import ConfigError, default_config_path, load_config


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None,
                        help=f"config file (default: {default_config_path()})")
    sub = parser.add_subparsers(dest="wr_command")
    sub.add_parser("run", help="run the daemon (foreground)")
    sub.add_parser("tick", help="run a single discovery/claim pass and exit")
    sub.add_parser("lanes", help="print the validated lane registry")
    sub.add_parser("doctor", help="check config, Hermes compatibility and executor availability")


def handle(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = getattr(args, "wr_command", None) or "doctor"
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"worker-runtime: config error: {exc}", file=sys.stderr)
        return 2

    if cmd == "lanes":
        print(json.dumps({a: l.to_dict() for a, l in config.lanes.items()}, indent=2))
        return 0
    if cmd == "doctor":
        return _doctor(config)

    from .scheduler import DaemonLockError, Scheduler
    sched = Scheduler(config)
    if cmd == "tick":
        res = sched.tick()
        print(json.dumps(res.__dict__, indent=2, default=str))
        return 0

    stopping = {"flag": False}

    def _stop(*_):
        stopping["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        sched.run_forever(stop=lambda: stopping["flag"])
    except DaemonLockError as exc:
        print(f"worker-runtime: {exc}", file=sys.stderr)
        return 1
    return 0


def _doctor(config) -> int:
    from . import hermes_api as h
    from .adapters import create_adapter
    info = h.describe()
    print(f"hermes kanban : {info['hermes_kanban_db']}")
    print(f"mode          : {info['mode']}")
    if info["mode"] != "pid-tracked":
        print("  ! claim-only: failures are recorded as blocks and dead supervisors are "
              "recovered by claim TTL expiry")
    print(f"boards        : {', '.join(config.boards) if config.boards else 'all'}")
    worst = 0
    for assignee, lane in config.lanes.items():
        ok, why = create_adapter(lane).available()
        print(f"lane {lane.name:<12} {assignee:<16} adapter={lane.adapter:<8} "
              f"concurrency={lane.concurrency} {'OK' if ok else 'UNAVAILABLE: ' + why}")
        worst = worst or (0 if ok else 1)
    if not shutil.which("git"):
        print("  ! git not found: changed_files metadata disabled")
    return worst


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-worker-runtime")
    setup_parser(parser)
    return handle(parser.parse_args(argv))
