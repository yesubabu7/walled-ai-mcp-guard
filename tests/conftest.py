"""Test harness: runs the real proxy (app/main.py) in front of examples/demo_server.py."""

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class McpSession:
    """A fake MCP client talking to the proxy over stdio."""

    def __init__(self, proxy_args: list[str], server: str = "demo"):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "app" / "main.py"), "--server", server, *proxy_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._inbox: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self._inbox.put(json.loads(line))

    def send(self, msg: dict | list) -> None:
        self.send_raw(json.dumps(msg).encode())

    def send_raw(self, raw: bytes) -> None:
        self.proc.stdin.write(raw + b"\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 10.0):
        return self._inbox.get(timeout=timeout)

    def assert_silent(self, seconds: float) -> None:
        with pytest.raises(queue.Empty):
            self._inbox.get(timeout=seconds)

    def request(self, request_id, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def call_tool(self, request_id, name: str, arguments: dict) -> None:
        self.request(request_id, "tools/call", {"name": name, "arguments": arguments})

    def initialize(self, capabilities: dict | None = None) -> None:
        self.request(0, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": capabilities or {},
            "clientInfo": {"name": "pytest", "version": "0"},
        })
        assert self.recv()["id"] == 0
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> int:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise


@pytest.fixture
def proxy():
    """Factory: proxy(*args) -> initialized McpSession using the policy approver."""
    sessions: list[McpSession] = []

    def start(*args: str, server: str = "demo", capabilities: dict | None = None) -> McpSession:
        session = McpSession(["--approver", "policy", *args], server=server)
        sessions.append(session)
        session.initialize(capabilities)
        return session

    yield start
    for s in sessions:
        s.close()
