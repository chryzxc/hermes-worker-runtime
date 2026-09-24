from hermes_worker_runtime.config import parse_config
from hermes_worker_runtime.envpolicy import Redactor, build_child_env, redact


def lane(env=None):
    return parse_config({"lanes": {"t": {"assignee": "wr:t", "adapter": "command",
                                         "command": ["true"], "env": env}}}).lanes["wr:t"]


PARENT = {
    "PATH": "/bin", "HOME": "/h", "OPENAI_API_KEY": "sk-parent-secret-value-123456",
    "HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_CLAIM": "lock", "AWS_SECRET_ACCESS_KEY": "x" * 20,
    "GH_TOKEN": "ghp_" + "a" * 30, "BASH_FUNC_x%%": "() { :; }",
}


def test_only_allowlisted_env_reaches_child():
    env = build_child_env(lane(), parent=PARENT)
    assert env == {"PATH": "/bin", "HOME": "/h"}


def test_pass_and_set_but_never_hermes_kanban():
    env = build_child_env(lane({"pass": ["GH_TOKEN", "HERMES_KANBAN_TASK"],
                                "set": {"CI": "1", "HERMES_KANBAN_CLAIM": "x"}}),
                          parent=PARENT, extra={"WR_TASK_ID": "t_1"})
    assert env["GH_TOKEN"] == PARENT["GH_TOKEN"] and env["CI"] == "1"
    assert env["WR_TASK_ID"] == "t_1"
    assert not any(k.startswith("HERMES_KANBAN_") for k in env)
    assert "OPENAI_API_KEY" not in env


def test_redacts_env_values_and_token_shapes():
    r = Redactor({"MY_SERVICE_PASSWORD": "hunter2hunter2", "PLAIN": "hunter3hunter3"})
    out = r("pw=hunter2hunter2 plain=hunter3hunter3")
    assert "hunter2hunter2" not in out and "hunter3hunter3" in out
    samples = [
        "sk-ant-api03-abcdefghijklmnopqrstuv",
        "ghp_" + "Z" * 36,
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ]
    for s in samples:
        assert s not in redact(f"token: {s} end"), s
    assert redact("Authorization: Bearer abcdefghijklmnop1234") == "Authorization: Bearer [REDACTED]"
    assert redact("export API_TOKEN=abc123") == "export API_TOKEN=[REDACTED]"
    assert redact("nothing secret here") == "nothing secret here"
