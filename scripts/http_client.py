"""Act like Claude Web against a running MCP Guard HTTP gateway.

Usage:  python scripts/http_client.py http://127.0.0.1:8765/mcp/<token> [tool] [json-arguments]
        python scripts/http_client.py https://<tunnel-host>/mcp/<token> read_text_file "{\"path\": \"notes.txt\"}"
"""

import json
import sys
import urllib.request

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def post(url: str, body: dict, session_id: str | None = None):
    headers = dict(HEADERS)
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        sid = resp.headers.get("Mcp-Session-Id", session_id)
        text = resp.read().decode()
        if resp.status == 202 or not text:
            return sid, None
        if resp.headers.get_content_type() == "text/event-stream":
            text = next(line[6:] for line in text.splitlines() if line.startswith("data: "))
        return sid, json.loads(text)


def main() -> None:
    url = sys.argv[1]
    tool = sys.argv[2] if len(sys.argv) > 2 else "list_allowed_directories"
    arguments = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    sid, init = post(url, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {"roots": {"listChanged": True}},
        "clientInfo": {"name": "http-client", "version": "0"}}})
    print("session:", sid, "| server:", init["result"]["serverInfo"])
    post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

    _, tools = post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, sid)
    print("tools:", [t["name"] for t in tools["result"]["tools"]])

    print(f"calling {tool}({arguments}) - waiting for MCP Guard decision...")
    _, result = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}}, sid)
    status = "BLOCKED" if result["result"].get("isError") else "ALLOWED"
    print(f"{status}: {result['result']['content'][0]['text']}")

    req = urllib.request.Request(url, headers={"Mcp-Session-Id": sid}, method="DELETE")
    urllib.request.urlopen(req, timeout=30).close()


if __name__ == "__main__":
    main()
