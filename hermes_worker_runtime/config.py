"""Runtime configuration — explicit and fail-closed.

Anything unexpected raises :class:`ConfigError` before a single card is
claimed. Lanes are the only way an assignee becomes executable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

ASSIGNEE_PREFIX = "wr:"
ADAPTERS = {"command", "acp"}
SUCCESS_ACTIONS = {"complete", "review"}
FAILURE_ACTIONS = {"block", "fail"}
PERMISSION_POLICIES = {"allow", "deny"}
_ASSIGNEE_RE = re.compile(r"^wr:[a-z0-9][a-z0-9._-]{0,62}$")

_TOP_KEYS = {"boards", "poll_interval_seconds", "heartbeat_interval_seconds",
             "worker_heartbeat_interval_seconds", "claim_ttl_seconds", "lanes"}
_LANE_KEYS = {"assignee", "adapter", "command", "agent", "agent_command", "concurrency",
              "timeout_seconds", "on_success", "on_failure", "reviewer", "env",
              "permission_policy", "kill_grace_seconds", "instructions"}
_ENV_KEYS = {"pass", "set"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EnvPolicy:
    passthrough: tuple[str, ...] = ()
    set: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Lane:
    name: str
    assignee: str
    adapter: str
    concurrency: int = 1
    timeout_seconds: int = 3600
    on_success: str = "complete"
    on_failure: str = "block"
    reviewer: Optional[str] = None
    command: tuple[str, ...] = ()
    agent: Optional[str] = None
    agent_command: tuple[str, ...] = ()
    permission_policy: str = "allow"
    kill_grace_seconds: int = 10
    instructions: str = ""
    env: EnvPolicy = field(default_factory=EnvPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "assignee": self.assignee, "adapter": self.adapter,
            "concurrency": self.concurrency, "timeout_seconds": self.timeout_seconds,
            "on_success": self.on_success, "on_failure": self.on_failure,
            "reviewer": self.reviewer, "command": list(self.command), "agent": self.agent,
            "agent_command": list(self.agent_command),
            "permission_policy": self.permission_policy,
            "kill_grace_seconds": self.kill_grace_seconds, "instructions": self.instructions,
            "env": {"pass": list(self.env.passthrough), "set": dict(self.env.set)},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Lane":
        body = {k: v for k, v in data.items() if k != "name" and v not in (None, [], "")}
        return _parse_lane(data["name"], body)


@dataclass(frozen=True)
class RuntimeConfig:
    lanes: dict[str, Lane]
    boards: Optional[tuple[str, ...]] = None   # None = every non-archived board
    poll_interval_seconds: float = 5.0
    heartbeat_interval_seconds: float = 30.0
    worker_heartbeat_interval_seconds: float = 300.0
    claim_ttl_seconds: Optional[int] = None

    def lane_for(self, assignee: Optional[str]) -> Optional[Lane]:
        if not assignee:
            return None
        return self.lanes.get(assignee.strip().lower())


def default_config_path() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home).expanduser() / "worker-runtime.yaml"


def load_config(path: Optional[Path] = None) -> RuntimeConfig:
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: Any) -> RuntimeConfig:
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    _reject_unknown(raw, _TOP_KEYS, "config")
    lanes_raw = raw.get("lanes")
    if not isinstance(lanes_raw, dict) or not lanes_raw:
        raise ConfigError("config.lanes must be a non-empty mapping")

    lanes: dict[str, Lane] = {}
    for name, body in lanes_raw.items():
        lane = _parse_lane(str(name), body)
        if lane.assignee in lanes:
            raise ConfigError(f"duplicate assignee {lane.assignee!r} "
                              f"(lanes {lanes[lane.assignee].name!r} and {name!r})")
        lanes[lane.assignee] = lane

    boards = raw.get("boards", "all")
    if boards == "all" or boards is None:
        boards_t = None
    elif isinstance(boards, list) and boards and all(isinstance(b, str) and b for b in boards):
        boards_t = tuple(boards)
    else:
        raise ConfigError("boards must be 'all' or a non-empty list of board slugs")

    ttl = raw.get("claim_ttl_seconds")
    return RuntimeConfig(
        lanes=lanes,
        boards=boards_t,
        poll_interval_seconds=_positive(raw, "poll_interval_seconds", 5.0, float),
        heartbeat_interval_seconds=_positive(raw, "heartbeat_interval_seconds", 30.0, float),
        worker_heartbeat_interval_seconds=_positive(
            raw, "worker_heartbeat_interval_seconds", 300.0, float),
        claim_ttl_seconds=None if ttl is None else _positive(raw, "claim_ttl_seconds", 900, int),
    )


def _parse_lane(name: str, body: Any) -> Lane:
    where = f"lanes.{name}"
    if not isinstance(body, dict):
        raise ConfigError(f"{where} must be a mapping")
    _reject_unknown(body, _LANE_KEYS, where)

    assignee = body.get("assignee")
    if not isinstance(assignee, str) or not _ASSIGNEE_RE.match(assignee):
        raise ConfigError(
            f"{where}.assignee must be an explicit lowercase '{ASSIGNEE_PREFIX}<name>' "
            f"identifier, got {assignee!r}")

    adapter = body.get("adapter")
    if adapter not in ADAPTERS:
        raise ConfigError(f"{where}.adapter must be one of {sorted(ADAPTERS)}, got {adapter!r}")

    command = _argv(body.get("command"), f"{where}.command")
    agent_command = _argv(body.get("agent_command"), f"{where}.agent_command")
    agent = body.get("agent")
    if adapter == "command":
        if not command:
            raise ConfigError(f"{where}.command is required for adapter 'command'")
        if agent or agent_command:
            raise ConfigError(f"{where}: 'agent'/'agent_command' are only valid for adapter 'acp'")
    else:
        if command:
            raise ConfigError(f"{where}.command is only valid for adapter 'command'")
        from .adapters.acp import PRESETS
        if agent is None and not agent_command:
            raise ConfigError(f"{where}: adapter 'acp' needs 'agent' (preset) or 'agent_command'")
        if agent is not None and agent not in PRESETS:
            raise ConfigError(f"{where}.agent must be one of {sorted(PRESETS)}, got {agent!r}")

    on_success = body.get("on_success", "complete" if adapter == "command" else "review")
    if on_success not in SUCCESS_ACTIONS:
        raise ConfigError(f"{where}.on_success must be one of {sorted(SUCCESS_ACTIONS)}")
    on_failure = body.get("on_failure", "block" if adapter == "command" else "fail")
    if on_failure not in FAILURE_ACTIONS:
        raise ConfigError(f"{where}.on_failure must be one of {sorted(FAILURE_ACTIONS)}")
    policy = body.get("permission_policy", "allow")
    if policy not in PERMISSION_POLICIES:
        raise ConfigError(f"{where}.permission_policy must be one of {sorted(PERMISSION_POLICIES)}")

    reviewer = body.get("reviewer")
    if reviewer is not None and not (isinstance(reviewer, str) and reviewer.strip()):
        raise ConfigError(f"{where}.reviewer must be a non-empty string")
    instructions = body.get("instructions", "")
    if not isinstance(instructions, str):
        raise ConfigError(f"{where}.instructions must be a string")

    return Lane(
        name=name, assignee=assignee, adapter=adapter,
        concurrency=_positive(body, "concurrency", 1, int, where),
        timeout_seconds=_positive(body, "timeout_seconds", 3600, int, where),
        on_success=on_success, on_failure=on_failure, reviewer=reviewer,
        command=command, agent=agent, agent_command=agent_command,
        permission_policy=policy,
        kill_grace_seconds=_positive(body, "kill_grace_seconds", 10, int, where),
        instructions=instructions,
        env=_parse_env(body.get("env"), f"{where}.env"),
    )


def _parse_env(raw: Any, where: str) -> EnvPolicy:
    if raw is None:
        return EnvPolicy()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping with 'pass' and/or 'set'")
    _reject_unknown(raw, _ENV_KEYS, where)
    passthrough = raw.get("pass", [])
    if not isinstance(passthrough, list) or not all(
            isinstance(n, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", n) for n in passthrough):
        raise ConfigError(f"{where}.pass must be a list of environment variable names")
    setv = raw.get("set", {})
    if not isinstance(setv, dict) or not all(
            isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in setv.items()):
        raise ConfigError(f"{where}.set must map names to scalar values")
    return EnvPolicy(tuple(passthrough), {k: str(v) for k, v in setv.items()})


def _argv(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raise ConfigError(f"{where} must be an argv list, not a shell string")
    if not isinstance(raw, list) or not raw or not all(isinstance(a, str) and a for a in raw):
        raise ConfigError(f"{where} must be a non-empty list of strings")
    return tuple(raw)


def _positive(raw: dict, key: str, default, typ, where: str = "config"):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where}.{key} must be a positive number")
    if typ is int and int(value) != value:
        raise ConfigError(f"{where}.{key} must be an integer")
    return typ(value)


def _reject_unknown(raw: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(map(str, raw)) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}")
