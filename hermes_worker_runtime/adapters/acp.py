"""Generic Agent Client Protocol (ACP) worker.

One adapter covers every ACP-speaking coding agent; per-agent differences are
reduced to a launch argv (a *preset*). The runtime is the ACP *client*: it
opens a session rooted at the Hermes workspace, sends the task prompt, streams
the agent's messages into the Hermes task log, and normalizes the outcome.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from typing import Any, Optional

from .. import __version__, procutil
from ..result import (RESULT_INSTRUCTIONS, Status, WorkerResult, looks_auth_failure,
                      looks_rate_limited, parse_agent_result, strip_result_block)
from .base import LaunchContext, WorkerAdapter

# Pinned so adapter upgrades are deliberate. Version drift is contained here.
PRESETS: dict[str, tuple[str, ...]] = {
    "codex": ("npx", "-y", "@zed-industries/codex-acp@0.16.0"),
    "claude": ("npx", "-y", "@agentclientprotocol/claude-agent-acp@0.81.2"),
    "opencode": ("opencode", "acp"),
}

_AUTH_REQUIRED = -32000
_STDIO_LIMIT = 16 * 1024 * 1024
_EXIT_DRAIN_SECONDS = 1.0


def agent_argv(lane) -> tuple[str, ...]:
    return tuple(lane.agent_command) if lane.agent_command else PRESETS[lane.agent]


class _Client:
    """ACP client callbacks: collect output, answer permission requests."""

    def __init__(self, adapter: "AcpAdapter"):
        self._a = adapter

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse
        wanted = ("allow_once", "allow_always") if self._a.lane.permission_policy == "allow" \
            else ("reject_once", "reject_always")
        title = getattr(tool_call, "title", None) or getattr(tool_call, "tool_call_id", "?")
        for kind in wanted:
            for opt in options:
                if opt.kind == kind:
                    self._a._log(f"[permission] {title}: {kind}")
                    return RequestPermissionResponse(
                        outcome=AllowedOutcome(outcome="selected", option_id=opt.option_id))
        self._a._log(f"[permission] {title}: no matching option, cancelled")
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(self, session_id, update, **kwargs):
        kind = getattr(update, "session_update", "")
        if kind == "agent_message_chunk":
            text = getattr(getattr(update, "content", None), "text", None)
            if text:
                self._a._message.append(text)
        elif kind == "tool_call":
            self._a._log(f"[tool] {getattr(update, 'title', '')}")
            self._a._tool_calls += 1
        elif kind == "plan":
            entries = getattr(update, "entries", []) or []
            self._a._log(f"[plan] {len(entries)} step(s)")

    # The runtime doesn't advertise fs/terminal capabilities; agents use their own tools.
    async def write_text_file(self, *a, **k):
        raise _method_not_supported("fs/write_text_file")

    async def read_text_file(self, *a, **k):
        raise _method_not_supported("fs/read_text_file")

    async def create_terminal(self, *a, **k):
        raise _method_not_supported("terminal/create")

    async def ext_method(self, method: str, params: dict) -> dict:
        raise _method_not_supported(method)

    async def ext_notification(self, method: str, params: dict) -> None:
        return None

    def on_connect(self, conn) -> None:
        pass


def _method_not_supported(method: str):
    from acp import RequestError
    return RequestError.method_not_found(method)


class AcpAdapter(WorkerAdapter):
    def __init__(self, lane):
        super().__init__(lane)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._conn: Any = None
        self._session_id: Optional[str] = None
        self._message: list[str] = []
        self._tool_calls = 0
        self._stderr_tail: list[str] = []
        self._stop_reason: Optional[str] = None
        self._error: Optional[BaseException] = None
        self._cancelled: Optional[str] = None
        self._log = lambda line: None

    # -- contract -----------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        try:
            import acp  # noqa: F401
        except ImportError:
            return (False, "python package 'agent-client-protocol' is not installed")
        exe = agent_argv(self.lane)[0]
        if shutil.which(exe) is None:
            return (False, f"ACP agent executable not found: {exe}")
        return (True, "")

    def launch(self, ctx: LaunchContext) -> None:
        self._log = ctx.log
        prompt = "\n\n".join(p for p in (ctx.prompt, self.lane.instructions, RESULT_INSTRUCTIONS) if p)
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._run(ctx, prompt)), name="wr-acp", daemon=True)
        self._thread.start()

    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def poll(self) -> bool:
        return self._thread is None or not self._thread.is_alive()

    def cancel(self, reason: str, *, grace: Optional[float] = None) -> None:
        grace = self.lane.kill_grace_seconds if grace is None else grace
        self._cancelled = reason
        loop, conn, sid = self._loop, self._conn, self._session_id
        polite = min(1.0, grace / 3)  # the whole budget must fit Hermes' SIGTERM->SIGKILL window
        if loop and conn and sid and loop.is_running():
            try:  # polite first: give the agent a chance to stop its own tools
                asyncio.run_coroutine_threadsafe(conn.cancel(session_id=sid), loop).result(
                    timeout=polite)
            except Exception:
                pass
        if self._proc is not None:
            procutil.terminate_group(self._proc.pid, grace_seconds=max(0.5, grace - polite))
        if self._thread is not None:
            self._thread.join(timeout=_EXIT_DRAIN_SECONDS + 2)

    def collect_result(self) -> WorkerResult:
        text = "".join(self._message).strip()
        stderr = "\n".join(self._stderr_tail[-40:])
        meta: dict[str, Any] = {"agent": self.lane.agent or "custom",
                                "stop_reason": self._stop_reason,
                                "tool_calls": self._tool_calls}
        if self._cancelled:
            return WorkerResult(Status.CANCELLED, self._cancelled, meta)
        if self._error is not None:
            err = f"{type(self._error).__name__}: {self._error}"
            code = getattr(self._error, "code", None)
            blob = f"{err}\n{getattr(self._error, 'data', '')}\n{stderr}"
            meta["error"] = err[:2000]
            if code == _AUTH_REQUIRED or looks_auth_failure(blob):
                return WorkerResult(Status.UNAVAILABLE, f"agent authentication failed: {err}", meta)
            if looks_rate_limited(blob):
                return WorkerResult(Status.RATE_LIMITED, f"agent rate-limited: {err}", meta)
            if isinstance(self._error, FileNotFoundError):
                return WorkerResult(Status.UNAVAILABLE, f"agent could not start: {err}", meta)
            last = next((l for l in reversed(self._stderr_tail) if l.strip()), "")
            return WorkerResult(Status.FAILED, f"agent error: {err}" + (
                f". Last stderr: {last[:500]}" if last else ""), meta)

        parsed = parse_agent_result(text)
        if self._stop_reason == "refusal":
            return WorkerResult(Status.BLOCKED, (parsed.summary if parsed else text)[:4000]
                                or "agent refused the task", meta)
        if self._stop_reason in ("max_tokens", "max_turn_requests") and parsed is None:
            return WorkerResult(Status.FAILED, f"agent stopped early ({self._stop_reason})", meta)
        if parsed is not None:
            parsed.metadata = {**meta, **parsed.metadata}
            return parsed
        if not text:
            if looks_rate_limited(stderr):
                return WorkerResult(Status.RATE_LIMITED, "agent rate-limited", meta)
            return WorkerResult(Status.FAILED, "agent finished without any output", meta)
        # No structured block: a human decides whether the prose is a result.
        meta["structured_result"] = False
        return WorkerResult(Status.NEEDS_REVIEW, strip_result_block(text)[-4000:], meta)

    # -- ACP session ----------------------------------------------------------------
    async def _run(self, ctx: LaunchContext, prompt: str) -> None:
        from acp import PROTOCOL_VERSION, text_block
        from acp.client.connection import ClientSideConnection
        from acp.schema import ClientCapabilities, FileSystemCapabilities, Implementation

        self._loop = asyncio.get_running_loop()
        argv = agent_argv(self.lane)
        stderr_task = None
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(ctx.workspace), env=ctx.env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=_STDIO_LIMIT,
            )
            ctx.log(f"[acp] started {' '.join(argv)} pid={self._proc.pid}")
            stderr_task = asyncio.create_task(self._pump_stderr())
            self._conn = ClientSideConnection(_Client(self), self._proc.stdin, self._proc.stdout)
            init = await self._request(self._conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
                    terminal=False),
                client_info=Implementation(name="hermes-worker-runtime", version=__version__),
            ))
            agent_info = getattr(init, "agent_info", None)
            if agent_info is not None:
                ctx.log(f"[acp] agent {getattr(agent_info, 'name', '?')} "
                        f"{getattr(agent_info, 'version', '')}")
            session = await self._request(
                self._conn.new_session(cwd=str(ctx.workspace), mcp_servers=[]))
            self._session_id = session.session_id
            resp = await self._request(
                self._conn.prompt(session_id=self._session_id, prompt=[text_block(prompt)]))
            self._stop_reason = getattr(resp, "stop_reason", None)
            ctx.log(f"[acp] stop_reason={self._stop_reason}")
            text = "".join(self._message).strip()
            if text:
                ctx.log("[acp] final message:\n" + text[-8000:])
        except BaseException as exc:  # noqa: BLE001 - surfaced via collect_result
            if not self._cancelled:
                self._error = exc
                ctx.log(f"[acp] error: {type(exc).__name__}: {exc}")
        finally:
            try:
                if self._conn is not None:
                    await self._conn.close()
            except Exception:
                pass
            if self._proc is not None and self._proc.returncode is None:
                try:
                    self._proc.stdin.close()
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except Exception:
                    procutil.terminate_group(self._proc.pid, grace_seconds=self.lane.kill_grace_seconds)
            if stderr_task is not None:
                stderr_task.cancel()

    async def _request(self, coro):
        """Await an ACP request, failing fast if the agent process exits first.

        The ACP connection does not reject in-flight requests when the agent's
        stdout reaches EOF, so a crashed agent would otherwise hang the run until
        the lane timeout."""
        request = asyncio.ensure_future(coro)
        exited = asyncio.ensure_future(self._proc.wait())
        try:
            await asyncio.wait({request, exited}, return_when=asyncio.FIRST_COMPLETED)
            if not request.done():
                # A response written just before exit may still be in the pipe.
                await asyncio.wait({request}, timeout=_EXIT_DRAIN_SECONDS)
            if request.done():
                return request.result()
            request.cancel()
            raise ConnectionError(f"agent exited with code {self._proc.returncode} "
                                  "before answering")
        finally:
            exited.cancel()

    async def _pump_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            self._stderr_tail.append(text)
            del self._stderr_tail[:-200]
            self._log(f"[agent-stderr] {text}")
