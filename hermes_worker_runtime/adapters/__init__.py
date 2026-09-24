"""Adapter factory: lane.adapter -> WorkerAdapter."""

from __future__ import annotations

from ..config import Lane
from .base import LaunchContext, WorkerAdapter


def create_adapter(lane: Lane) -> WorkerAdapter:
    if lane.adapter == "command":
        from .command import CommandAdapter
        return CommandAdapter(lane)
    if lane.adapter == "acp":
        from .acp import AcpAdapter
        return AcpAdapter(lane)
    raise ValueError(f"unknown adapter {lane.adapter!r}")  # config validation prevents this


__all__ = ["LaunchContext", "WorkerAdapter", "create_adapter"]
