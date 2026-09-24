"""Normalized worker result model."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Status(str, Enum):
    SUCCESS = "success"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    # The executor itself can't run here (missing binary, expired auth):
    # retrying won't help until an operator fixes the configuration.
    UNAVAILABLE = "unavailable"


@dataclass
class WorkerResult:
    status: Status
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    exit_code: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "summary": self.summary,
                "metadata": self.metadata, "artifacts": self.artifacts,
                "exit_code": self.exit_code}


# Deliberately specific: a false positive on a failing test run ("429 passed,
# 1 failed") would requeue it forever without spending the failure budget.
_RATE_LIMIT_RE = re.compile(
    r"(rate.?limit(ed)?\b|too many requests|(http|status|error|code)[ :=]*429\b|"
    r"quota (exceeded|exhausted)|usage limit|insufficient_quota|overloaded_error)", re.I)
_AUTH_RE = re.compile(
    r"(not (logged|signed) in|authentication (failed|required)|invalid api key|"
    r"unauthori[sz]ed|\b401\b|please (log|sign) ?in|auth(entication)? expired|login required)", re.I)


def looks_rate_limited(text: str) -> bool:
    return bool(text and _RATE_LIMIT_RE.search(text))


def looks_auth_failure(text: str) -> bool:
    return bool(text and _AUTH_RE.search(text))


# Agents are asked to end with:
# ```hermes-result
# {"status": "needs_review", "summary": "...", "metadata": {...}}
# ```
_BLOCK_RE = re.compile(r"```(?:hermes-result|json)\s*\n(.*?)\n```", re.S)
_AGENT_STATUSES = {Status.SUCCESS, Status.NEEDS_REVIEW, Status.BLOCKED, Status.FAILED}

RESULT_INSTRUCTIONS = """\
When you are finished, end your final message with exactly one fenced block:

```hermes-result
{"status": "<success|needs_review|blocked|failed>", "summary": "<one paragraph>", "metadata": {}}
```

Use "blocked" only when you genuinely need human input (put the question in summary).
Do not call any Hermes Kanban tools; the runtime records the outcome for you."""


def parse_agent_result(text: str) -> Optional[WorkerResult]:
    """The last well-formed ``hermes-result`` block in ``text``, else None."""
    for raw in reversed(_BLOCK_RE.findall(text or "")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        try:
            status = Status(str(data.get("status", "")).strip().lower())
        except ValueError:
            continue
        if status not in _AGENT_STATUSES:
            continue
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        artifacts = [a for a in data.get("artifacts", []) if isinstance(a, str)] \
            if isinstance(data.get("artifacts"), list) else []
        return WorkerResult(status, str(data.get("summary") or "").strip(), metadata, artifacts)
    return None


def strip_result_block(text: str) -> str:
    return _BLOCK_RE.sub("", text or "").strip()
