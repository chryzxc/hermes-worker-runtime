"""Worker adapter contract.

An adapter turns one claimed card into one supervised execution. It never
touches Kanban: the supervisor owns every lifecycle write.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..config import Lane
from ..result import WorkerResult


@dataclass
class LaunchContext:
    task_id: str
    run_id: int
    title: str
    prompt: str               # Hermes build_worker_context() + lane/result instructions
    workspace: Path
    env: dict[str, str]
    log: Callable[[str], None]  # redacting line logger into the Hermes task log
    extra: dict = field(default_factory=dict)


class WorkerAdapter(abc.ABC):
    def __init__(self, lane: Lane):
        self.lane = lane

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """Can this executor run on this host? ``(ok, reason)``; no side effects."""

    @abc.abstractmethod
    def launch(self, ctx: LaunchContext) -> None:
        """Start the execution; must return promptly."""

    @abc.abstractmethod
    def poll(self) -> bool:
        """True once the execution has finished (for any reason)."""

    @abc.abstractmethod
    def cancel(self, reason: str, *, grace: Optional[float] = None) -> None:
        """Stop the execution and everything it spawned; blocks until gone.
        ``grace`` overrides the lane's TERM->KILL grace period."""

    @abc.abstractmethod
    def collect_result(self) -> WorkerResult:
        """Normalized outcome; only valid after ``poll()`` is True."""

    def pid(self) -> Optional[int]:
        return None
