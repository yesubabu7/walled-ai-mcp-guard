"""Fake MCP client: launches the proxy and runs a short MCP session through it.

Usage:  python scripts/test_client.py [server-name] [proxy options...]
        python scripts/test_client.py demo                      # console Allow/Block prompts
        python scripts/test_client.py demo --approver policy --allow echo --block add
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    server = sys.argv[1] if len(sys.argv) > 1 else "demo"
    proxy_options = sys.argv[2:]
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app" / "main.py"), "--server", server, *proxy_options],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # proxy logs go to logs/proxy.log
    )

    def send(msg: dict) -> None:
        print(">>", json.dumps(msg))
        proc.stdin.write(json.dumps(msg).encode() + b"\n")
        proc.stdin.flush()

    def recv() -> dict:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("proxy closed stdout")
        msg = json.loads(line)
        print("<<", json.dumps(msg)[:300])
        return msg

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.1"}}})
    recv()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = recv()["result"]["tools"]
    print("   tools:", [t["name"] for t in tools])

    if server == "demo":
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "echo", "arguments": {"text": "hello through the proxy"}}})
        recv()
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "add", "arguments": {"a": 2, "b": 40}}})
        recv()
    else:
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "list_allowed_directories", "arguments": {}}})
        recv()

    proc.stdin.close()
    code = proc.wait(timeout=15)
    print(f"proxy exited with code {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
