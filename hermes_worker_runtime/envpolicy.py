"""Child environment construction and secret redaction."""

from __future__ import annotations

import os
import re
from typing import Mapping, Optional

from .config import Lane

# Minimal base every child gets; everything else must be listed in lane.env.pass.
BASE_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR")

# Never forwarded, even when listed: Hermes claim material would let the
# external agent drive Kanban lifecycle writes the runtime owns.
_DENY_PREFIXES = ("HERMES_KANBAN_",)

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|COOKIE)", re.I)
_SECRET_VALUE_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)=(\S+)"),
]


def build_child_env(lane: Lane, *, extra: Optional[Mapping[str, str]] = None,
                    parent: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    parent = os.environ if parent is None else parent
    env: dict[str, str] = {}
    for name in (*BASE_ALLOWLIST, *lane.env.passthrough):
        if name.startswith(_DENY_PREFIXES):
            continue
        value = parent.get(name)
        if value is not None and not value.startswith("()"):
            env[name] = value
    for name, value in lane.env.set.items():
        if not name.startswith(_DENY_PREFIXES):
            env[name] = value
    if extra:
        env.update(extra)
    return env


class Redactor:
    """Masks secret-shaped substrings and the literal values of secret-named env vars."""

    def __init__(self, env: Optional[Mapping[str, str]] = None):
        values = []
        for name, value in (env or {}).items():
            if _SECRET_NAME.search(name) and value and len(value) >= 6:
                values.append(value)
        # Longest first so a value containing another is masked whole.
        self._literals = sorted(set(values), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for literal in self._literals:
            text = text.replace(literal, "[REDACTED]")
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.groups:
                text = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]" if "=" in m.group(0)
                                   else f"{m.group(1)} [REDACTED]", text)
            else:
                text = pattern.sub("[REDACTED]", text)
        return text


def redact(text: str, env: Optional[Mapping[str, str]] = None) -> str:
    return Redactor(env)(text)
