"""End-to-end tests for tools/call interception (Step 4)."""

import time


def text_of(response: dict) -> str:
    return response["result"]["content"][0]["text"]


def test_allowed_call_reaches_backend(proxy):
    s = proxy("--allow", "echo")
    s.call_tool(1, "echo", {"text": "hello"})
    resp = s.recv()
    assert resp["id"] == 1
    assert resp["result"]["isError"] is False
    assert text_of(resp) == "hello"


def test_blocked_call_returns_tool_error(proxy):
    s = proxy("--block", "add")
    s.call_tool(2, "add", {"a": 1, "b": 2})
    resp = s.recv()
    assert resp["id"] == 2
    assert resp["result"]["isError"] is True
    assert "blocked by MCP Guard" in text_of(resp)
    assert "'add'" in text_of(resp)


def test_unlisted_tool_blocked_by_default(proxy):
    s = proxy("--allow", "echo")
    s.call_tool(3, "slow_echo", {"text": "x", "seconds": 0})
    assert s.recv()["result"]["isError"] is True


def test_non_tool_traffic_flows_while_call_is_pending(proxy):
    s = proxy("--allow", "echo", "--decision-delay", "1.5")
    s.call_tool(10, "echo", {"text": "held"})
    s.request(11, "tools/list")
    s.request(12, "ping")
    # tools/list and ping are answered while the tool call waits for approval.
    first, second = s.recv(timeout=1.0), s.recv(timeout=1.0)
    assert {first["id"], second["id"]} == {11, 12}
    held = s.recv(timeout=5)
    assert held["id"] == 10 and text_of(held) == "held"


def test_concurrent_calls_are_decided_independently(proxy):
    s = proxy("--allow", "echo", "--block", "add", "--decision-delay", "1.0")
    start = time.monotonic()
    s.call_tool("a", "echo", {"text": "one"})
    s.call_tool("b", "add", {"a": 1, "b": 1})
    s.call_tool("c", "echo", {"text": "three"})
    responses = {r["id"]: r for r in (s.recv(), s.recv(), s.recv())}
    elapsed = time.monotonic() - start

    assert text_of(responses["a"]) == "one"
    assert responses["b"]["result"]["isError"] is True
    assert text_of(responses["c"]) == "three"
    assert elapsed < 2.5, f"approvals ran sequentially ({elapsed:.1f}s)"


def test_numeric_and_string_ids_are_distinct(proxy):
    s = proxy("--allow", "echo", "--decision-delay", "0.3")
    s.call_tool(7, "echo", {"text": "int"})
    s.call_tool("7", "echo", {"text": "str"})
    responses = {repr(r["id"]): text_of(r) for r in (s.recv(), s.recv())}
    assert responses == {"7": "int", "'7'": "str"}


def test_cancelled_pending_call_gets_no_response(proxy):
    s = proxy("--allow", "echo", "--decision-delay", "1.0")
    s.call_tool(30, "echo", {"text": "never"})
    s.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": 30, "reason": "user aborted"}})
    s.request(31, "ping")
    assert s.recv()["id"] == 31
    s.assert_silent(1.5)


def test_approval_timeout_blocks(proxy):
    s = proxy("--allow", "echo", "--decision-delay", "3", "--approval-timeout", "0.5")
    s.call_tool(40, "echo", {"text": "too slow"})
    resp = s.recv(timeout=3)
    assert resp["result"]["isError"] is True
    assert "no decision within 0.5s" in text_of(resp)


def test_batch_with_tool_call_is_rejected(proxy):
    s = proxy("--default", "allow")
    s.send([
        {"jsonrpc": "2.0", "id": 50, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "x"}}},
        {"jsonrpc": "2.0", "id": 51, "method": "ping"},
    ])
    errors = s.recv()
    assert isinstance(errors, list)
    assert {e["id"] for e in errors} == {50, 51}
    assert all(e["error"]["code"] == -32600 for e in errors)


def test_unparseable_line_is_not_forwarded(proxy):
    s = proxy()
    s.send_raw(b'{"jsonrpc": "2.0", "id": 60, "method": "tools/call"')  # truncated JSON
    resp = s.recv()
    assert resp["error"]["code"] == -32700


def test_proxy_exits_cleanly_with_pending_calls(proxy):
    s = proxy("--allow", "echo", "--decision-delay", "30")
    s.call_tool(70, "echo", {"text": "pending at shutdown"})
    time.sleep(0.2)
    start = time.monotonic()
    assert s.close() == 0
    assert time.monotonic() - start < 5
