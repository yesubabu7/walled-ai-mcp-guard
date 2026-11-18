"""Approval decisions for intercepted `tools/call` requests.

The proxy only depends on the `Approver` interface: `await approver.decide(request)`.
Implementations:
  * PolicyApprover  - rule-based (allow/block lists); used by automated tests.
  * ConsoleApprover - asks in the terminal window; a dev tool until the Tkinter popup (step 5).

The proxy's own stdin/stdout carry MCP traffic, so the console approver talks to the
terminal device directly (CONIN$/CONOUT$ on Windows, /dev/tty elsewhere).
"""

import asyncio
import enum
import json
import sys
import threading
from dataclasses import dataclass, field


class Decision(enum.Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class ToolCallRequest:
    server: str
    request_id: object
    tool: str
    arguments: dict = field(default_factory=dict)

    def arguments_preview(self, limit: int = 2000) -> str:
        text = json.dumps(self.arguments, indent=2, ensure_ascii=False)
        return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


class Approver:
    async def decide(self, request: ToolCallRequest) -> Decision:
        raise NotImplementedError


class PolicyApprover(Approver):
    """Static rules. Block list wins over allow list; anything unlisted gets `default`."""

    def __init__(self, allow=(), block=(), default: Decision = Decision.BLOCK, delay: float = 0.0):
        self.allow = set(allow)
        self.block = set(block)
        self.default = default
        self.delay = delay  # simulates a human taking time to decide

    async def decide(self, request: ToolCallRequest) -> Decision:
        if self.delay:
            await asyncio.sleep(self.delay)
        if request.tool in self.block:
            return Decision.BLOCK
        if request.tool in self.allow:
            return Decision.ALLOW
        return self.default


class ConsoleApprover(Approver):
    """Prompts in the controlling terminal. Prompts are serialized, one request at a time."""

    def __init__(self):
        # Raises OSError when there is no terminal (e.g. launched by Claude Desktop).
        if sys.platform == "win32":
            self._in = open("CONIN$", "r", encoding="utf-8")
            self._out = open("CONOUT$", "w", encoding="utf-8")
        else:
            self._in = self._out = open("/dev/tty", "r+", encoding="utf-8")
        self._lock = threading.Lock()

    async def decide(self, request: ToolCallRequest) -> Decision:
        return await asyncio.to_thread(self._prompt, request)

    def _prompt(self, request: ToolCallRequest) -> Decision:
        with self._lock:
            self._out.write(
                "\n==== MCP Guard: tool call pending ====\n"
                f"server : {request.server}\n"
                f"tool   : {request.tool}\n"
                f"id     : {request.request_id}\n"
                f"args   :\n{request.arguments_preview()}\n"
            )
            while True:
                self._out.write("[A]llow / [B]lock? ")
                self._out.flush()
                answer = self._in.readline()
                if not answer:  # terminal closed: fail closed
                    return Decision.BLOCK
                answer = answer.strip().lower()
                if answer in ("a", "allow"):
                    return Decision.ALLOW
                if answer in ("b", "block"):
                    return Decision.BLOCK
