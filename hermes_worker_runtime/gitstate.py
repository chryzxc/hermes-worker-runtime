"""Deterministic change evidence: ``git status`` before and after a run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def porcelain(workspace: Path) -> Optional[set[str]]:
    """Changed paths in ``workspace``, or None when it is not a git work tree."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=str(workspace), capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    paths = set()
    entries = out.stdout.decode("utf-8", "replace").split("\0")
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        paths.add(path)
        if "R" in code or "C" in code:
            skip_next = True  # -z emits the rename source as the next entry
    return paths


def change_metadata(before: Optional[set[str]], after: Optional[set[str]]) -> dict:
    if after is None:
        return {}
    meta = {"changed_files": sorted(after - (before or set()))[:500]}
    if before:
        meta["preexisting_changes"] = sorted(before)[:200]
    return meta
