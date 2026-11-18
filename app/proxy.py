"""Bidirectional stdio proxy between an MCP client and a backend MCP server.

    MCP client (Claude Desktop)                       backend MCP server
            |  stdin  -->  [ StdioProxy ]  --> proc.stdin   |
            |  stdout <--  [ StdioProxy ]  <-- proc.stdout  |

MCP's stdio transport is newline-delimited JSON-RPC 2.0: one message per line.

Every message passes through unchanged except `tools/call` requests. Each of those
is held in its own asyncio task until the Approver decides:
  * ALLOW -> the original bytes are forwarded to the backend; its response flows back
             normally (responses are matched to requests by JSON-RPC id, so order
             doesn't matter).
  * BLOCK -> the backend never sees the request; the proxy answers the client itself
             with an MCP tool error result (`isError: true`).
The read loop never awaits an approval, so other traffic and further tool calls keep
flowing while requests are pending. Anything ambiguous fails closed (blocked).
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import threading

from app.approval import Approver, Decision, ToolCallRequest

log = logging.getLogger("mcp_guard.proxy")

# asyncio's default line limit is 64 KiB; tool results (e.g. file contents) can be larger.
STREAM_LIMIT = 16 * 1024 * 1024
SHUTDOWN_TIMEOUT = 5.0
DEFAULT_APPROVAL_TIMEOUT = 300.0

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603


def describe(msg: object) -> str:
    """One-line summary of a JSON-RPC message for logs."""
    if not isinstance(msg, dict):
        return f"batch/other ({type(msg).__name__})"
    if "method" in msg:
        kind = "request" if "id" in msg else "notification"
        return f"{kind} method={msg['method']} id={msg.get('id')}"
    if "error" in msg:
        return f"error-response id={msg.get('id')} error={msg['error']}"
    return f"response id={msg.get('id')}"


def is_tool_call(msg: object) -> bool:
    return isinstance(msg, dict) and msg.get("method") == "tools/call"


def id_key(request_id: object) -> str:
    # JSON-RPC ids may be numbers or strings; 1 and "1" are different ids.
    return json.dumps(request_id)


def tool_error_result(request_id: object, text: str) -> dict:
    """An MCP tool-execution error: a normal result the model can read and explain."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def jsonrpc_error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class StdioProxy:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str],
        approver: Approver,
        env: dict[str, str] | None = None,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT,
        client_out=None,
        ignore_client_roots: bool = False,
    ):
        # When set, the client can't widen the backend's scope via MCP Roots: we hide the
        # client's `roots` capability and drop roots/list_changed, so the server keeps the
        # directories from its own command line (e.g. the filesystem server's sandbox).
        self.ignore_client_roots = ignore_client_roots
        # Binary stream to the MCP client. main.py captures the real stdout here and points
        # sys.stdout at stderr, so a stray print() can never corrupt the protocol stream.
        self.client_out = client_out if client_out is not None else sys.stdout.buffer
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self.approver = approver
        self.approval_timeout = approval_timeout
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested: asyncio.Event | None = None

    def stop(self) -> None:
        """Thread-safe request to shut down (e.g. Ctrl+C handled on the GUI thread)."""
        loop, event = self._loop, self._stop_requested
        if loop is not None and event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    async def run(self) -> int:
        # On Windows, "npx" is really "npx.cmd"; shutil.which resolves that.
        exe = shutil.which(self.command) or self.command
        log.info("[%s] starting backend: %s %s", self.name, exe, " ".join(self.args))
        self.proc = await asyncio.create_subprocess_exec(
            exe,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self.env},
            limit=STREAM_LIMIT,
        )
        log.info("[%s] backend pid=%s", self.name, self.proc.pid)

        client_lines: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._start_stdin_reader(asyncio.get_running_loop(), client_lines)

        self._loop = asyncio.get_running_loop()
        self._stop_requested = asyncio.Event()

        client_task = asyncio.create_task(self._client_to_server(client_lines), name="client->server")
        server_task = asyncio.create_task(self._server_to_client(), name="server->client")
        stderr_task = asyncio.create_task(self._drain_backend_stderr(), name="backend-stderr")
        stop_task = asyncio.create_task(self._stop_requested.wait(), name="stop-request")

        try:
            done, _ = await asyncio.wait(
                {client_task, server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if client_task in done:
                log.info("[%s] client closed stdin; shutting down backend", self.name)
            elif server_task in done:
                log.warning("[%s] backend closed its stdout (exited?)", self.name)
            else:
                log.info("[%s] stop requested; shutting down", self.name)
        finally:
            pending = list(self._pending.values())
            if pending:
                log.info("[%s] discarding %d pending tool call(s)", self.name, len(pending))
            for task in (client_task, server_task, stderr_task, stop_task, *pending):
                task.cancel()
            await self._stop_backend()

        return self.proc.returncode or 0

    # ---- client -> server -------------------------------------------------

    @staticmethod
    def _start_stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        """Read our stdin on a thread.

        asyncio can't reliably read stdin on Windows (pipes vs console), so a
        daemon thread does blocking reads and hands lines to the event loop.
        """

        def reader() -> None:
            try:
                for line in sys.stdin.buffer:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except Exception:
                log.exception("stdin reader failed")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=reader, name="stdin-reader", daemon=True).start()

    async def _client_to_server(self, queue: asyncio.Queue) -> None:
        while True:
            line = await queue.get()
            if line is None:
                return
            if not line.strip():
                continue
            msg = self._parse(line, "client->server")
            await self._on_client_message(msg, line)

    async def _on_client_message(self, msg: object, raw: bytes) -> None:
        if msg is None:
            # Never forward bytes we couldn't inspect: they might hide a tool call.
            self._send_to_client(jsonrpc_error(None, PARSE_ERROR, "Parse error (rejected by MCP Guard)"))
            return

        if isinstance(msg, list):
            await self._on_client_batch(msg, raw)
            return

        if is_tool_call(msg):
            if "id" not in msg:
                log.warning("[%s] dropping tools/call sent as a notification (no id)", self.name)
                return
            self._hold_tool_call(msg, raw)
            return

        if self.ignore_client_roots and isinstance(msg, dict):
            if msg.get("method") == "notifications/roots/list_changed":
                log.info("[%s] dropped roots/list_changed (client roots are ignored)", self.name)
                return
            if msg.get("method") == "initialize":
                capabilities = (msg.get("params") or {}).get("capabilities")
                if isinstance(capabilities, dict) and "roots" in capabilities:
                    del capabilities["roots"]
                    raw = json.dumps(msg).encode()
                    log.info("[%s] removed client 'roots' capability from initialize", self.name)

        if isinstance(msg, dict) and msg.get("method") == "notifications/cancelled":
            request_id = (msg.get("params") or {}).get("requestId")
            task = self._pending.get(id_key(request_id))
            if task is not None:
                # The backend never saw this request, so the cancellation stays with us.
                log.info("[%s] client cancelled pending tool call id=%s", self.name, request_id)
                task.cancel()
                return

        await self._send_to_server(raw)

    async def _on_client_batch(self, batch: list, raw: bytes) -> None:
        """JSON-RPC batches (allowed by older MCP revisions) must not smuggle tool calls past approval."""
        if not any(is_tool_call(m) for m in batch):
            await self._send_to_server(raw)
            return
        log.warning("[%s] rejecting batch containing tools/call", self.name)
        errors = [
            jsonrpc_error(m["id"], INVALID_REQUEST, "MCP Guard does not accept tools/call inside a batch")
            for m in batch
            if isinstance(m, dict) and "id" in m and "method" in m
        ]
        if errors:
            self._send_to_client(errors)

    def _hold_tool_call(self, msg: dict, raw: bytes) -> None:
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        request = ToolCallRequest(
            server=self.name,
            request_id=msg["id"],
            tool=str(params.get("name", "<missing>")),
            arguments=arguments,
        )
        key = id_key(request.request_id)
        if key in self._pending:
            self._send_to_client(jsonrpc_error(request.request_id, INVALID_REQUEST, "Duplicate request id"))
            return

        log.info("[%s] HOLD tool=%s id=%s (pending=%d)", self.name, request.tool, request.request_id, len(self._pending) + 1)
        task = asyncio.create_task(self._resolve_tool_call(request, raw), name=f"tool-call {key}")
        self._pending[key] = task
        task.add_done_callback(lambda _t: self._pending.pop(key, None))

    async def _resolve_tool_call(self, request: ToolCallRequest, raw: bytes) -> None:
        reason = "denied by the user"
        try:
            decision = await asyncio.wait_for(self.approver.decide(request), self.approval_timeout)
        except asyncio.TimeoutError:
            decision, reason = Decision.BLOCK, f"no decision within {self.approval_timeout:g}s"
        except asyncio.CancelledError:
            log.info("[%s] tool call id=%s cancelled before a decision", self.name, request.request_id)
            raise
        except Exception:
            log.exception("[%s] approver failed for id=%s", self.name, request.request_id)
            decision, reason = Decision.BLOCK, "the approval prompt failed"

        log.info("[%s] %s tool=%s id=%s", self.name, decision.value.upper(), request.tool, request.request_id)

        if decision is Decision.ALLOW:
            try:
                await self._send_to_server(raw)
            except (ConnectionError, OSError):
                log.exception("[%s] backend unavailable for id=%s", self.name, request.request_id)
                self._send_to_client(jsonrpc_error(request.request_id, INTERNAL_ERROR, "MCP server is not available"))
        else:
            self._send_to_client(tool_error_result(
                request.request_id,
                f"Tool call '{request.tool}' on server '{self.name}' was blocked by MCP Guard ({reason}).",
            ))

    async def _send_to_server(self, raw: bytes) -> None:
        assert self.proc and self.proc.stdin
        if not raw.endswith(b"\n"):
            raw += b"\n"
        self.proc.stdin.write(raw)
        await self.proc.stdin.drain()

    # ---- server -> client -------------------------------------------------

    async def _server_to_client(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            if not line.strip():
                continue
            if self._parse(line, "server->client") is None:
                continue  # stray non-protocol output from the backend (already logged); keep the channel clean
            self._send_to_client(line)

    def _send_to_client(self, message: bytes | dict | list) -> None:
        # All writers run on the event-loop thread and do write+flush with no await in
        # between, so concurrent tasks can never interleave partial messages.
        raw = message if isinstance(message, bytes) else json.dumps(message).encode()
        if not raw.endswith(b"\n"):
            raw += b"\n"
        self.client_out.write(raw)
        self.client_out.flush()

    async def _drain_backend_stderr(self) -> None:
        """Relay backend stderr into our log; an undrained pipe can fill and block the backend."""
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            log.info("[%s] backend stderr: %s", self.name, line.decode("utf-8", "replace").rstrip())

    # ---- helpers ----------------------------------------------------------

    def _parse(self, line: bytes, direction: str) -> object:
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("[%s] %s non-JSON line: %r", self.name, direction, line[:200])
            return None
        log.info("[%s] %s %s", self.name, direction, describe(msg))
        log.debug("[%s] %s payload: %s", self.name, direction, line.decode("utf-8", "replace").rstrip())
        return msg

    async def _stop_backend(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()  # polite shutdown: MCP servers exit on stdin EOF
            await asyncio.wait_for(proc.wait(), SHUTDOWN_TIMEOUT)
        except (asyncio.TimeoutError, ProcessLookupError):
            log.warning("[%s] backend did not exit in %.0fs; killing", self.name, SHUTDOWN_TIMEOUT)
            proc.kill()
            await proc.wait()
        log.info("[%s] backend exited with code %s", self.name, proc.returncode)
