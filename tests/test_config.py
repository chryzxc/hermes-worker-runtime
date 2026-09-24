import pytest

from hermes_worker_runtime.config import ConfigError, Lane, load_config, parse_config


def cfg(**lanes):
    return {"lanes": lanes}


def cmd_lane(**kw):
    return {"assignee": "wr:tests", "adapter": "command", "command": ["pytest", "-q"], **kw}


def test_minimal_command_lane_defaults():
    c = parse_config(cfg(tests=cmd_lane()))
    lane = c.lane_for("WR:Tests ")
    assert lane.name == "tests" and lane.command == ("pytest", "-q")
    assert (lane.on_success, lane.on_failure, lane.concurrency) == ("complete", "block", 1)
    assert c.boards is None and c.claim_ttl_seconds is None


def test_acp_lane_defaults_to_review_and_fail():
    c = parse_config(cfg(codex={"assignee": "wr:codex", "adapter": "acp", "agent": "codex"}))
    lane = c.lanes["wr:codex"]
    assert (lane.on_success, lane.on_failure) == ("review", "fail")


@pytest.mark.parametrize("raw, fragment", [
    ([], "mapping"),
    ({}, "non-empty"),
    ({"lanes": {}, "extra": 1}, "unknown key"),
    (cfg(t=cmd_lane(assignee="codex")), "wr:"),
    (cfg(t=cmd_lane(assignee="wr:Codex")), "wr:"),
    (cfg(t=cmd_lane(assignee="wr:")), "wr:"),
    (cfg(t=cmd_lane(adapter="shell")), "adapter"),
    (cfg(t=cmd_lane(command="pytest -q")), "shell string"),
    (cfg(t=cmd_lane(command=[])), "non-empty list"),
    (cfg(t=cmd_lane(concurrency=0)), "positive"),
    (cfg(t=cmd_lane(concurrency=True)), "positive"),
    (cfg(t=cmd_lane(timeout_seconds=1.5)), "integer"),
    (cfg(t=cmd_lane(on_success="merge")), "on_success"),
    (cfg(t=cmd_lane(on_failure="retry")), "on_failure"),
    (cfg(t=cmd_lane(surprise=1)), "unknown key"),
    (cfg(t=cmd_lane(env={"pass": ["OK", "BAD NAME"]})), "pass"),
    (cfg(t=cmd_lane(env={"inherit": True})), "unknown key"),
    (cfg(t=cmd_lane(agent="codex")), "only valid for adapter 'acp'"),
    (cfg(a={"assignee": "wr:a", "adapter": "acp"}), "needs 'agent'"),
    (cfg(a={"assignee": "wr:a", "adapter": "acp", "agent": "gpt"}), "agent must be one of"),
    (cfg(a={"assignee": "wr:a", "adapter": "acp", "agent": "codex", "command": ["x"]}),
     "only valid for adapter 'command'"),
    (cfg(a=cmd_lane(), b=cmd_lane()), "duplicate assignee"),
    ({"lanes": {"t": cmd_lane()}, "boards": []}, "boards"),
    ({"lanes": {"t": cmd_lane()}, "poll_interval_seconds": -1}, "positive"),
])
def test_fail_closed(raw, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_config(raw)


def test_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("lanes: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


def test_lane_roundtrip():
    lane = parse_config(cfg(t=cmd_lane(env={"pass": ["GH_TOKEN"], "set": {"CI": 1}},
                                       reviewer="lead", timeout_seconds=60))).lanes["wr:tests"]
    assert Lane.from_dict(lane.to_dict()) == lane
    acp = parse_config(cfg(c={"assignee": "wr:c", "adapter": "acp",
                              "agent_command": ["my-agent", "--acp"]})).lanes["wr:c"]
    assert Lane.from_dict(acp.to_dict()) == acp
