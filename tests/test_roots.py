"""MCP Roots pinning: with ignore_client_roots the client can't change the backend's scope."""

import json

ROOTS_CAPS = {"roots": {"listChanged": True}, "sampling": {}}


def backend_state(session) -> dict:
    session.call_tool(99, "debug_state", {})
    return json.loads(session.recv()["result"]["content"][0]["text"])


def test_pinned_server_never_sees_client_roots(proxy):
    s = proxy("--allow", "debug_state", server="demo_pinned", capabilities=ROOTS_CAPS)
    s.send({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
    state = backend_state(s)
    assert state["client_capabilities"] == {"sampling": {}}  # roots removed, rest untouched
    assert "notifications/roots/list_changed" not in state["notifications"]
    assert "notifications/initialized" in state["notifications"]


def test_unpinned_server_is_transparent(proxy):
    s = proxy("--allow", "debug_state", server="demo", capabilities=ROOTS_CAPS)
    s.send({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
    state = backend_state(s)
    assert state["client_capabilities"] == ROOTS_CAPS
    assert "notifications/roots/list_changed" in state["notifications"]
