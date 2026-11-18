"""Streamable HTTP gateway: lets Claude Web (remote MCP connectors) go through MCP Guard.

Claude Web can't launch local processes. It connects to *remote* MCP servers over HTTPS
(MCP "Streamable HTTP" transport), and those requests come from Anthropic's cloud. So:

    Claude Web ──HTTPS──▶ tunnel (cloudflared/ngrok) ──HTTP──▶ McpGateway (127.0.0.1)
                                                              │  one GatewaySession per MCP session
                                                              ▼
                                                     StdioProxy (same interception + popup)
                                                              │ stdio
                                                              ▼
                                                     real MCP server (e.g. filesystem)

Transport details (MCP spec 2025-06-18, Streamable HTTP):
  * POST /mcp/<token>  one JSON-RPC message per request.
      - notification/response -> 202 Accepted
      - request               -> SSE stream (text/event-stream) carrying the response; while a
                                 tool call waits for the popup we send SSE comments as keep-alives
                                 so proxies/clients don't time out. JSON is returned instead if the
                                 client doesn't accept SSE.
  * `initialize` creates a session; its id is returned in the Mcp-Session-Id header and must be
    sent on every later request (unknown id -> 404, which tells the client to re-initialize).
  * DELETE ends a session (stops its backend). GET (server->client stream) -> 405, allowed by spec.

Security:
  * The secret token in the URL path is the credential (compared in constant time; wrong token -> 404).
  * Listens on 127.0.0.1 only; the tunnel is the only way in.
  * Requests carrying a browser Origin header are rejected unless allow-listed (DNS-rebinding defence).
  * If Claude Web disconnects while a call is pending, the call is cancelled and its popup withdrawn.
"""

import asyncio
import json
import logging
import re
import secrets
import time
from typing import Callable

from aiohttp import web

from app.approval import Approver
from app.proxy import StdioProxy, id_key

log = logging.getLogger("mcp_guard.gateway")

SESSION_HEADER = "Mcp-Session-Id"
DEFAULT_KEEPALIVE = 15.0
DEFAULT_IDLE_TIMEOUT = 30 * 60.0
REAP_INTERVAL = 60.0


def rpc_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class _ResponseRouter:
    """Binary 'stdout' handed to StdioProxy: routes each outgoing message to its waiting HTTP request."""

    def __init__(self, session: "GatewaySession"):
        self._session = session

    def write(self, raw: bytes) -> None:
        msg = json.loads(raw)
        for item in msg if isinstance(msg, list) else [msg]:
            self._session.deliver(item)

    def flush(self) -> None:
        pass


class GatewaySession:
    """One MCP session from Claude Web = one guarded backend process."""

    def __init__(self, session_id: str, proxy_factory: Callable[["_ResponseRouter", str], StdioProxy]):
        self.id = session_id
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.waiters: dict[str, asyncio.Future] = {}
        self.last_seen = time.monotonic()
        self.proxy = proxy_factory(_ResponseRouter(self), session_id)
        self.task = asyncio.create_task(self.proxy.run(client_lines=self.inbox), name=f"session {session_id[:8]}")
        self.task.add_done_callback(self._on_proxy_exit)

    @property
    def alive(self) -> bool:
        return not self.task.done()

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def submit(self, msg: dict) -> None:
        self.inbox.put_nowait(json.dumps(msg).encode())

    def expect(self, request_id) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self.waiters[id_key(request_id)] = future
        return future

    def deliver(self, msg: object) -> None:
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            future = self.waiters.pop(id_key(msg.get("id")), None)
            if future is not None and not future.done():
                future.set_result(msg)
                return
        # Server-initiated requests/notifications would need the optional GET stream, which we don't offer.
        log.info("[session %s] dropping unroutable server message: %s", self.id[:8], str(msg)[:200])

    def cancel(self, request_id, reason: str) -> None:
        """Client went away mid-request: withdraw the pending call (closes its popup)."""
        self.waiters.pop(id_key(request_id), None)
        self.submit({"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": request_id, "reason": reason}})

    async def close(self) -> None:
        if self.alive:
            self.inbox.put_nowait(None)  # = client disconnected: proxy stops its backend
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 10)
            except asyncio.TimeoutError:
                self.task.cancel()

    def _on_proxy_exit(self, _task: asyncio.Task) -> None:
        for key, future in list(self.waiters.items()):
            if not future.done():
                future.set_result(rpc_error(json.loads(key), -32603, "MCP server session ended"))
        self.waiters.clear()


class McpGateway:
    def __init__(
        self,
        server_name: str,
        command: str,
        args: list[str],
        approver: Approver,
        token: str,
        env: dict | None = None,
        approval_timeout: float = 300.0,
        ignore_client_roots: bool = False,
        allowed_origins: tuple[str, ...] = (),
        keepalive: float = DEFAULT_KEEPALIVE,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ):
        self.server_name = server_name
        self.command = command
        self.args = args
        self.env = env
        self.approver = approver
        self.token = token
        self.approval_timeout = approval_timeout
        self.ignore_client_roots = ignore_client_roots
        self.allowed_origins = set(allowed_origins)
        self.keepalive = keepalive
        self.idle_timeout = idle_timeout
        self.sessions: dict[str, GatewaySession] = {}
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- app wiring ----

    @web.middleware
    async def _access_log(self, request: web.Request, handler):
        path = re.sub(r"^/mcp/[^/]+", "/mcp/<token>", request.path)  # never log the secret
        status = "error"
        try:
            response = await handler(request)
            status = response.status
            return response
        except web.HTTPException as exc:
            status = exc.status
            raise
        finally:
            log.info("HTTP %s %s -> %s (ua=%s)", request.method, path, status,
                     request.headers.get("User-Agent", "-")[:80])

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=16 * 1024 * 1024, middlewares=[self._access_log])
        app.router.add_route("POST", "/mcp/{token}", self.handle_post)
        app.router.add_route("DELETE", "/mcp/{token}", self.handle_delete)
        app.router.add_route("GET", "/mcp/{token}", self.handle_get)
        app.on_cleanup.append(self._close_all)
        return app

    async def serve(self, host: str = "127.0.0.1", port: int = 8765) -> int:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        app = self.make_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("gateway for %r listening on http://%s:%d/mcp/<token>", self.server_name, host, port)
        reaper = asyncio.create_task(self._reap_idle())
        try:
            await self._stop.wait()
        finally:
            reaper.cancel()
            await runner.cleanup()
        return 0

    def stop(self) -> None:
        """Thread-safe request to shut the gateway down."""
        loop, event = self._loop, self._stop
        if loop is not None and event is not None and not loop.is_closed():
            loop.call_soon_threadsafe(event.set)

    # ---- request handling ----

    def _check(self, request: web.Request) -> web.Response | None:
        if not secrets.compare_digest(request.match_info["token"].encode(), self.token.encode()):
            log.warning("rejected request with wrong token from %s", request.remote)
            return web.Response(status=404)
        origin = request.headers.get("Origin")
        if origin and origin not in self.allowed_origins:
            log.warning("rejected request with Origin %s", origin)
            return web.Response(status=403, text="origin not allowed")
        return None

    async def handle_post(self, request: web.Request) -> web.StreamResponse:
        if (denied := self._check(request)) is not None:
            return denied
        try:
            msg = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response(rpc_error(None, -32700, "Parse error"), status=400)
        if isinstance(msg, list):
            return web.json_response(rpc_error(None, -32600, "JSON-RPC batching is not supported"), status=400)
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return web.json_response(rpc_error(None, -32600, "Invalid Request"), status=400)

        if msg.get("method") == "initialize":
            session = self._new_session()
        else:
            session = self.sessions.get(request.headers.get(SESSION_HEADER, ""))
            if session is None or not session.alive:
                return web.json_response(rpc_error(msg.get("id"), -32001, "Session not found"), status=404)
        session.touch()

        if "method" not in msg or "id" not in msg:  # notification or response to a server request
            session.submit(msg)
            return web.Response(status=202, headers={SESSION_HEADER: session.id})

        future = session.expect(msg["id"])
        session.submit(msg)
        if "text/event-stream" in request.headers.get("Accept", ""):
            return await self._respond_sse(request, session, msg, future)
        return web.json_response(await future, headers={SESSION_HEADER: session.id})

    async def _respond_sse(self, request, session, msg, future) -> web.StreamResponse:
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            SESSION_HEADER: session.id,
        })
        await resp.prepare(request)
        try:
            while True:
                try:
                    reply = await asyncio.wait_for(asyncio.shield(future), self.keepalive)
                    break
                except asyncio.TimeoutError:
                    await resp.write(b": waiting for MCP Guard approval\n\n")
            await resp.write(f"event: message\ndata: {json.dumps(reply)}\n\n".encode())
            await resp.write_eof()
        except (ConnectionResetError, asyncio.CancelledError) as exc:
            log.info("[session %s] client disconnected while id=%s was pending", session.id[:8], msg.get("id"))
            session.cancel(msg["id"], "HTTP client disconnected")
            if isinstance(exc, asyncio.CancelledError):
                raise
        return resp

    async def handle_delete(self, request: web.Request) -> web.Response:
        if (denied := self._check(request)) is not None:
            return denied
        session = self.sessions.pop(request.headers.get(SESSION_HEADER, ""), None)
        if session is None:
            return web.Response(status=404)
        log.info("[session %s] closed by client", session.id[:8])
        await session.close()
        return web.Response(status=200)

    async def handle_get(self, request: web.Request) -> web.Response:
        if (denied := self._check(request)) is not None:
            return denied
        return web.Response(status=405, headers={"Allow": "POST, DELETE"})

    # ---- sessions ----

    def _new_session(self) -> GatewaySession:
        session_id = secrets.token_urlsafe(24)

        def factory(router: _ResponseRouter, sid: str) -> StdioProxy:
            return StdioProxy(
                name=f"{self.server_name}@web",
                command=self.command,
                args=self.args,
                approver=self.approver,
                env=self.env,
                approval_timeout=self.approval_timeout,
                client_out=router,
                ignore_client_roots=self.ignore_client_roots,
            )

        session = GatewaySession(session_id, factory)
        self.sessions[session_id] = session
        log.info("[session %s] created (%d active)", session_id[:8], len(self.sessions))
        return session

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL)
            now = time.monotonic()
            for sid, session in list(self.sessions.items()):
                if not session.alive or (now - session.last_seen > self.idle_timeout and not session.waiters):
                    self.sessions.pop(sid, None)
                    log.info("[session %s] reaped (idle or ended)", sid[:8])
                    await session.close()

    async def _close_all(self, _app=None) -> None:
        sessions, self.sessions = list(self.sessions.values()), {}
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
