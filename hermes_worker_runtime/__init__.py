"""Hermes Worker Runtime — external execution lanes for Hermes Kanban."""

from __future__ import annotations

__version__ = "0.1.0"


def register(ctx) -> None:
    """Hermes plugin entrypoint: exposes ``hermes worker-runtime ...``.

    The daemon deliberately runs as its own process (``hermes worker-runtime run``)
    rather than inside the gateway, so it never holds the dispatcher's lock and a
    runtime crash cannot take the gateway down.
    """
    from .cli import handle, setup_parser

    ctx.register_cli_command(
        name="worker-runtime",
        help="External worker lanes (wr:*) for Hermes Kanban",
        setup_fn=setup_parser,
        handler_fn=handle,
        description="Claim Kanban cards assigned to configured wr:* lanes and execute them "
                    "with external coding agents (ACP) or deterministic commands.",
    )
