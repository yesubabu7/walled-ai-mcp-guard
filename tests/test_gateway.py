"""Streamable HTTP gateway tests (Claude Web path), run in-process against the demo server."""

import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from app.approval import Decision, PolicyApprover
from app.gateway import SESSION_HEADER, McpGateway

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-123"
URL = f"/mcp/{TOKEN}"
SSE = {"Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def gateway(allow=("echo", "slow_echo", "debug_state"), block=("add",), delay=0.0, **kwargs):
    gw = McpGateway(
        server_name="demo",
        command=sys.executable,
        args=[str(ROOT / "examples" / "demo_server.py")],
        approver=PolicyApprover(allow=allow, block=block, default=Decision.BLOCK, delay=delay),
        token=TOKEN,
        **kwargs,
    )
    client = TestClient(TestServer(gw.make_app()))
    await client.start_server()
    try:
        yield gw, client
    finally:
        await client.close()


def parse_sse(text: str) -> dict:
    data = [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]
    assert len(data) == 1, text
    return json.loads(data[0])


async def rpc(client, session_id, request_id, method, params=None, headers=SSE):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    hdrs = dict(headers)
    if session_id:
        hdrs[SESSION_HEADER] = session_id
    resp = await client.post(URL, json=body, headers=hdrs)
    return resp, (parse_sse(await resp.text()) if resp.content_type == "text/event-stream" else await resp.json())


async def open_session(client) -> str:
    resp, reply = await rpc(client, None, 0, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest-web", "version": "0"}})
    assert resp.status == 200 and reply["result"]["serverInfo"]["name"] == "demo-server"
    session_id = resp.headers[SESSION_HEADER]
    note = await client.post(URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                             headers={**SSE, SESSION_HEADER: session_id})
    assert note.status == 202
    return session_id


def call(name, arguments):
    return {"name": name, "arguments": arguments}


def text_of(reply):
    return reply["result"]["content"][0]["text"]


def run(coro):
    return asyncio.run(coro)


def test_initialize_list_allow_and_block():
    async def scenario():
        async with gateway() as (_gw, client):
            sid = await open_session(client)
            _, tools = await rpc(client, sid, 1, "tools/list")
            assert {t["name"] for t in tools["result"]["tools"]} >= {"echo", "add"}

            _, allowed = await rpc(client, sid, 2, "tools/call", call("echo", {"text": "from the web"}))
            assert allowed["result"]["isError"] is False and text_of(allowed) == "from the web"

            _, blocked = await rpc(client, sid, 3, "tools/call", call("add", {"a": 1, "b": 2}))
            assert blocked["result"]["isError"] is True
            assert "blocked by MCP Guard" in text_of(blocked) and "demo@web" in text_of(blocked)

    run(scenario())


def test_plain_json_response_when_sse_not_accepted():
    async def scenario():
        async with gateway() as (_gw, client):
            sid = await open_session(client)
            resp, reply = await rpc(client, sid, 1, "tools/call", call("echo", {"text": "json"}),
                                    headers={"Accept": "application/json"})
            assert resp.content_type == "application/json" and text_of(reply) == "json"

    run(scenario())


def test_wrong_token_and_unknown_session_are_rejected():
    async def scenario():
        async with gateway() as (_gw, client):
            resp = await client.post("/mcp/wrong", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert resp.status == 404
            resp, _ = await rpc(client, "no-such-session", 1, "ping")
            assert resp.status == 404

    run(scenario())


def test_foreign_browser_origin_is_rejected():
    async def scenario():
        async with gateway() as (_gw, client):
            resp = await client.post(URL, json={"jsonrpc": "2.0", "id": 0, "method": "initialize"},
                                     headers={"Origin": "https://evil.example"})
            assert resp.status == 403

    run(scenario())


def test_batches_and_bad_json_are_rejected():
    async def scenario():
        async with gateway() as (_gw, client):
            resp = await client.post(URL, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
            assert resp.status == 400
            resp = await client.post(URL, data=b"{not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400 and (await resp.json())["error"]["code"] == -32700

    run(scenario())


def test_concurrent_calls_in_one_session():
    async def scenario():
        async with gateway(delay=1.0) as (_gw, client):
            sid = await open_session(client)
            start = time.monotonic()
            results = await asyncio.gather(
                rpc(client, sid, 10, "tools/call", call("echo", {"text": "a"})),
                rpc(client, sid, 11, "tools/call", call("add", {"a": 1, "b": 1})),
                rpc(client, sid, 12, "tools/call", call("echo", {"text": "c"})),
            )
            elapsed = time.monotonic() - start
            replies = {r["id"]: r for _, r in results}
            assert text_of(replies[10]) == "a" and text_of(replies[12]) == "c"
            assert replies[11]["result"]["isError"] is True
            assert elapsed < 2.5, f"approvals ran sequentially ({elapsed:.1f}s)"

    run(scenario())


def test_sessions_are_isolated():
    async def scenario():
        async with gateway() as (gw, client):
            first, second = await open_session(client), await open_session(client)
            assert first != second and len(gw.sessions) == 2
            # Same request id in two sessions must not collide.
            (_, r1), (_, r2) = await asyncio.gather(
                rpc(client, first, 1, "tools/call", call("echo", {"text": "one"})),
                rpc(client, second, 1, "tools/call", call("echo", {"text": "two"})),
            )
            assert (text_of(r1), text_of(r2)) == ("one", "two")

    run(scenario())


def test_keepalives_sent_while_waiting_for_approval():
    async def scenario():
        async with gateway(delay=0.8, keepalive=0.2) as (_gw, client):
            sid = await open_session(client)
            resp = await client.post(URL, json={"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                                "params": call("echo", {"text": "slow"})},
                                     headers={**SSE, SESSION_HEADER: sid})
            body = await resp.text()
            assert body.count(": waiting for MCP Guard approval") >= 2
            assert text_of(parse_sse(body)) == "slow"

    run(scenario())


def test_client_disconnect_cancels_pending_call():
    async def scenario():
        async with gateway(delay=2.0, keepalive=0.1) as (gw, client):
            sid = await open_session(client)
            request = asyncio.ensure_future(rpc(client, sid, 7, "tools/call", call("echo", {"text": "gone"})))
            await asyncio.sleep(0.4)
            request.cancel()  # Claude Web gave up / closed the connection
            await asyncio.gather(request, return_exceptions=True)
            proxy = gw.sessions[sid].proxy
            for _ in range(50):
                if not proxy._pending:
                    break
                await asyncio.sleep(0.05)
            assert proxy._pending == {}, "pending call should be cancelled after disconnect"

    run(scenario())


def test_delete_ends_session():
    async def scenario():
        async with gateway() as (gw, client):
            sid = await open_session(client)
            resp = await client.delete(URL, headers={SESSION_HEADER: sid})
            assert resp.status == 200 and sid not in gw.sessions
            resp, _ = await rpc(client, sid, 1, "ping")
            assert resp.status == 404

    run(scenario())


def test_get_stream_not_offered():
    async def scenario():
        async with gateway() as (_gw, client):
            resp = await client.get(URL)
            assert resp.status == 405

    run(scenario())
