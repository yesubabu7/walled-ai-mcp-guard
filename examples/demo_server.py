"""Minimal MCP server over stdio, used to test the proxy without Node.

Implements just enough of MCP: initialize, ping, tools/list, tools/call.
"""

import json
import sys
import time

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the given text.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "slow_echo",
        "description": "Echo text after a delay (seconds). Useful for concurrency tests.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "seconds": {"type": "number"}},
            "required": ["text"],
        },
    },
    {
        "name": "debug_state",
        "description": "Return the client capabilities and notifications this server has received.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

STATE = {"client_capabilities": None, "notifications": []}


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def call_tool(name, args):
    if name == "echo":
        return str(args.get("text", ""))
    if name == "add":
        return str(args["a"] + args["b"])
    if name == "slow_echo":
        time.sleep(float(args.get("seconds", 2)))
        return str(args.get("text", ""))
    if name == "debug_state":
        return json.dumps(STATE)
    raise KeyError(name)


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:
        if method:
            STATE["notifications"].append(method)
        return None  # notification (e.g. notifications/initialized): no reply
    if method == "initialize":
        STATE["client_capabilities"] = msg.get("params", {}).get("capabilities")
        return ok(mid, {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "demo-server", "version": "0.1.0"},
        })
    if method == "ping":
        return ok(mid, {})
    if method == "tools/list":
        return ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params", {})
        try:
            text = call_tool(params.get("name"), params.get("arguments") or {})
        except KeyError:
            return err(mid, -32602, f"Unknown tool: {params.get('name')}")
        return ok(mid, {"content": [{"type": "text", "text": text}], "isError": False})
    return err(mid, -32601, f"Method not found: {method}")


def main():
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = err(None, -32700, "Parse error")
        else:
            resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp).encode() + b"\n")
            out.flush()


if __name__ == "__main__":
    main()
