from hermes_worker_runtime.result import (Status, looks_auth_failure, looks_rate_limited,
                                          parse_agent_result, strip_result_block)


def block(body):
    return f"```hermes-result\n{body}\n```"


def test_parses_last_valid_block():
    text = ("draft\n" + block('{"status": "failed", "summary": "old"}') + "\nmore\n"
            + block('{"status": "needs_review", "summary": "new", "metadata": {"pr": 1},'
                    ' "artifacts": ["a.txt", 3]}')
            + "\n" + block("{not json"))
    r = parse_agent_result(text)
    assert r.status is Status.NEEDS_REVIEW and r.summary == "new"
    assert r.metadata == {"pr": 1} and r.artifacts == ["a.txt"]


def test_rejects_runtime_only_statuses_and_garbage():
    assert parse_agent_result(block('{"status": "cancelled"}')) is None
    assert parse_agent_result(block('{"status": "rate_limited"}')) is None
    assert parse_agent_result(block('["success"]')) is None
    assert parse_agent_result("no block at all") is None
    assert parse_agent_result("") is None


def test_json_fence_accepted_and_stripped():
    text = "hello\n```json\n{\"status\": \"success\", \"summary\": \"ok\"}\n```"
    assert parse_agent_result(text).status is Status.SUCCESS
    assert strip_result_block(text) == "hello"


def test_detectors():
    assert looks_rate_limited("Error 429 Too Many Requests")
    assert looks_rate_limited("You've hit your usage limit")
    assert looks_rate_limited("HTTP 429: slow down")
    assert looks_rate_limited('{"type": "overloaded_error"}')
    assert not looks_rate_limited("429 passed, 1 failed in 3.2s")
    assert not looks_rate_limited("FAILED test_rate_limiter_config")
    assert looks_auth_failure("Not logged in. Please run login")
    assert looks_auth_failure("401 Unauthorized")
    assert not looks_auth_failure("tests passed")
