"""Scriptable ACP agent for tests. Behaviour comes from FAKE_ACP_MODE:

result      stream a message ending with a hermes-result block (status from FAKE_ACP_STATUS)
prose       stream plain text, no structured block
permission  ask for permission, then report the chosen option as the result summary
hang        never answer the prompt (until cancelled / killed)
crash       exit mid-prompt without answering (agent process dies)
auth        fail the prompt with ACP auth_required (-32000)
refusal     stop with stop_reason=refusal
env         report the names of the environment variables it received
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

from acp import PROTOCOL_VERSION, RequestError, run_agent, text_block
from acp.helpers import update_agent_message_text
from acp.schema import (AgentCapabilities, InitializeResponse, NewSessionResponse,
                        PermissionOption, PromptResponse, ToolCallUpdate)

MODE = os.environ.get("FAKE_ACP_MODE", "result")


def _block(status: str, summary: str) -> str:
    return "\n```hermes-result\n" + json.dumps(
        {"status": status, "summary": summary, "metadata": {"fake": True}}) + "\n```\n"


class FakeAgent:
    def on_connect(self, conn) -> None:
        self._conn = conn

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kw):
        return InitializeResponse(protocol_version=PROTOCOL_VERSION,
                                  agent_capabilities=AgentCapabilities())

    async def new_session(self, cwd, mcp_servers=None, **kw):
        self.cwd = cwd
        return NewSessionResponse(session_id=uuid.uuid4().hex)

    async def cancel(self, session_id, **kw):
        return None

    async def authenticate(self, method_id, **kw):
        return None

    async def _say(self, sid, text):
        await self._conn.session_update(session_id=sid, update=update_agent_message_text(text))

    async def prompt(self, prompt, session_id, **kw):
        sid = session_id
        if MODE == "hang":
            await asyncio.sleep(3600)
        if MODE == "crash":
            print("fake agent: simulated crash", file=sys.stderr, flush=True)
            os._exit(3)
        if MODE == "auth":
            raise RequestError(-32000, "Authentication required")
        if MODE == "refusal":
            await self._say(sid, "I will not do that.")
            return PromptResponse(stop_reason="refusal")
        if MODE == "prose":
            await self._say(sid, "I looked at it. ")
            await self._say(sid, "Everything seems fine.")
            return PromptResponse(stop_reason="end_turn")
        if MODE == "permission":
            resp = await self._conn.request_permission(
                session_id=sid,
                tool_call=ToolCallUpdate(tool_call_id="t1", title="rm -rf build"),
                options=[PermissionOption(option_id="yes", name="Allow", kind="allow_once"),
                         PermissionOption(option_id="no", name="Reject", kind="reject_once")])
            chosen = getattr(resp.outcome, "option_id", None) or resp.outcome.outcome
            await self._say(sid, _block("success", f"permission={chosen}"))
            return PromptResponse(stop_reason="end_turn")
        if MODE == "env":
            await self._say(sid, _block("success", ",".join(sorted(os.environ))))
            return PromptResponse(stop_reason="end_turn")
        text = "".join(getattr(b, "text", "") for b in prompt)
        status = os.environ.get("FAKE_ACP_STATUS", "success")
        await self._say(sid, "Working on it.\n")
        await self._say(sid, _block(status, f"did it in {os.path.basename(self.cwd)} "
                                            f"(prompt {len(text)} chars)"))
        return PromptResponse(stop_reason="end_turn")


if __name__ == "__main__":
    try:
        asyncio.run(run_agent(FakeAgent()))
    except KeyboardInterrupt:
        sys.exit(130)
