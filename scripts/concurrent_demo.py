"""Fire several tool calls at once through the proxy and print results as they arrive.

Usage:  python scripts/concurrent_demo.py [proxy options...]
        python scripts/concurrent_demo.py                    # one popup per call
        python scripts/concurrent_demo.py --approval-timeout 20
"""

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CALLS = [
    (101, "echo", {"text": "first request"}),
    (102, "add", {"a": 19, "b": 23}),
    (103, "slow_echo", {"text": "third request (2s of work)", "seconds": 2}),
]


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "app" / "main.py"), "--server", "demo", *sys.argv[1:]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg).encode() + b"\n")
        proc.stdin.flush()

    def recv() -> dict:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("proxy closed stdout")
        return json.loads(line)

    send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "concurrent-demo", "version": "0.1"}}})
    recv()
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    start = time.monotonic()
    for request_id, tool, arguments in CALLS:
        send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}})
        print(f"sent  id={request_id} {tool}({arguments})")

    send({"jsonrpc": "2.0", "id": 200, "method": "ping"})
    print("sent  id=200 ping  (answered immediately even while tool calls wait)")

    for _ in range(len(CALLS) + 1):
        resp = recv()
        elapsed = time.monotonic() - start
        if resp["id"] == 200:
            print(f"[{elapsed:5.1f}s] id=200 ping -> pong")
            continue
        result = resp.get("result", {})
        status = "BLOCKED" if result.get("isError") else "ALLOWED"
        text = result.get("content", [{}])[0].get("text", resp.get("error"))
        print(f"[{elapsed:5.1f}s] id={resp['id']} {status}: {text}")

    proc.stdin.close()
    return proc.wait(timeout=20)


if __name__ == "__main__":
    sys.exit(main())
