"""Entry point: `python app/main.py --server <name>` (or `python -m app.main ...`).

Looks up <name> in config/servers.json and proxies stdio between the MCP client
that launched us and that backend server, asking an Approver about every tools/call.

Run modes:
  * popup (default): Tk mainloop on the main thread, asyncio proxy on a worker thread.
  * console / policy: asyncio proxy on the main thread.
"""

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
import threading
from pathlib import Path
from typing import Callable, Coroutine

ROOT = Path(__file__).resolve().parent.parent

# Allow running as a plain script (Claude Desktop launches us by file path).
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from app.approval import Approver, ConsoleApprover, Decision, PolicyApprover  # noqa: E402
from app.logger import setup_logging  # noqa: E402
from app.proxy import DEFAULT_APPROVAL_TIMEOUT, StdioProxy  # noqa: E402

DEFAULT_CONFIG = ROOT / "config" / "servers.json"
TOKEN_FILE = ROOT / "config" / "gateway_token.txt"
WORKER_JOIN_TIMEOUT = 15.0


def expand(value: str) -> str:
    """Substitute {python} (this interpreter) and {root} (project folder) placeholders."""
    return value.replace("{python}", sys.executable).replace("{root}", str(ROOT))


def csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build_approver(args: argparse.Namespace) -> Approver:
    if args.approver == "popup":
        from app.tk_approver import TkinterApprover

        return TkinterApprover(timeout=args.approval_timeout)
    if args.approver == "console":
        return ConsoleApprover()
    return PolicyApprover(
        allow=args.allow,
        block=args.block,
        default=Decision(args.default),
        delay=args.decision_delay,
    )


def run_with_popup(make_coro: Callable[[], Coroutine], request_stop: Callable[[], None], approver, log) -> int:
    """Tk owns the main thread; the asyncio side (stdio proxy or HTTP gateway) runs on a worker thread."""
    outcome = {"code": 1}

    def worker_thread() -> None:
        try:
            outcome["code"] = asyncio.run(make_coro())
        except Exception:
            log.exception("MCP Guard crashed")
        finally:
            approver.stop()  # asyncio side finished (client disconnected or crash): end the GUI loop

    def on_sigint(*_args) -> None:
        log.info("interrupted")
        outcome["code"] = 130
        approver.stop()

    signal.signal(signal.SIGINT, on_sigint)  # delivered on the main thread between Tk polls
    worker = threading.Thread(target=worker_thread, name="asyncio-loop")
    worker.start()
    approver.run()
    request_stop()  # no-op if the asyncio side already finished
    worker.join(WORKER_JOIN_TIMEOUT)
    if worker.is_alive():
        log.error("asyncio thread did not stop within %.0fs", WORKER_JOIN_TIMEOUT)
    return outcome["code"]


def start_tray(args: argparse.Namespace, approver, log):
    """Tray icon for popup mode. Optional: MCP Guard keeps working without pystray."""
    try:
        from app.logger import LOG_DIR
        from app.tray import TrayIcon

        mode = "web gateway" if args.http else "Claude Desktop"
        tray = TrayIcon(f"{args.server} ({mode})", on_show=approver.show_all, on_quit=approver.stop,
                        log_path=LOG_DIR / "proxy.log")
        approver.on_change = tray.set_pending
        tray.start()
        return tray
    except Exception as exc:  # ImportError (pystray/Pillow missing) or no tray support
        log.warning("tray icon unavailable (%s); continuing without it", exc)
        return None


def load_token(explicit: str | None) -> str:
    """Gateway credential: --token, else $MCP_GUARD_TOKEN, else a persisted random token."""
    token = explicit or os.environ.get("MCP_GUARD_TOKEN")
    if token:
        return token
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP Guard stdio proxy")
    parser.add_argument("--server", required=True, help="server name from servers.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to servers.json")
    parser.add_argument("--approver", choices=["popup", "console", "policy"], default="popup",
                        help="how tool calls are approved: Tkinter popup, console prompt, or static policy")
    parser.add_argument("--allow", type=csv, default=[], help="policy: comma-separated tools to allow")
    parser.add_argument("--block", type=csv, default=[], help="policy: comma-separated tools to block")
    parser.add_argument("--default", choices=["allow", "block"], default="block",
                        help="policy: decision for unlisted tools")
    parser.add_argument("--decision-delay", type=float, default=0.0,
                        help="policy: seconds to wait before deciding (simulates a human)")
    parser.add_argument("--approval-timeout", type=float, default=DEFAULT_APPROVAL_TIMEOUT,
                        help="seconds before an undecided tool call is blocked")
    parser.add_argument("--no-tray", action="store_true", help="popup mode: don't show a system-tray icon")
    http = parser.add_argument_group("HTTP gateway (Claude Web)")
    http.add_argument("--http", action="store_true",
                      help="serve MCP over Streamable HTTP instead of stdio")
    http.add_argument("--host", default="127.0.0.1", help="listen address (keep 127.0.0.1; expose via a tunnel)")
    http.add_argument("--port", type=int, default=8765)
    http.add_argument("--token", help=f"URL secret (default: $MCP_GUARD_TOKEN or {TOKEN_FILE.name})")
    http.add_argument("--allow-origin", action="append", default=[],
                      help="browser Origin to accept (requests with any other Origin are rejected)")
    args = parser.parse_args()

    # stdout belongs to the MCP protocol. Keep the real stream for the proxy and send
    # anything else that prints (libraries, accidental print()) to stderr instead.
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr

    log = setup_logging()

    try:
        servers = json.loads(Path(args.config).read_text(encoding="utf-8"))["servers"]
        cfg = servers[args.server]
    except FileNotFoundError:
        log.error("config file not found: %s", args.config)
        return 2
    except KeyError:
        log.error("server %r not defined in %s", args.server, args.config)
        return 2

    try:
        approver = build_approver(args)
    except Exception as exc:  # OSError (no console) or tkinter.TclError (no display)
        log.error("cannot start %s approver: %s", args.approver, exc)
        return 2

    command = expand(cfg["command"])
    backend_args = [expand(a) for a in cfg.get("args", [])]
    ignore_roots = bool(cfg.get("ignore_client_roots", False))

    if args.http:
        from app.gateway import McpGateway

        token = load_token(args.token)
        service = McpGateway(
            server_name=args.server, command=command, args=backend_args, approver=approver,
            token=token, env=cfg.get("env"), approval_timeout=args.approval_timeout,
            ignore_client_roots=ignore_roots, allowed_origins=tuple(args.allow_origin),
        )
        log.info("Claude Web connector URL: https://<your-tunnel-host>/mcp/%s", token)
        make_coro = lambda: service.serve(args.host, args.port)  # noqa: E731
    else:
        service = StdioProxy(
            name=args.server, command=command, args=backend_args, approver=approver,
            env=cfg.get("env"), approval_timeout=args.approval_timeout,
            client_out=protocol_out, ignore_client_roots=ignore_roots,
        )
        make_coro = service.run

    if args.approver == "popup":
        tray = None if args.no_tray else start_tray(args, approver, log)
        try:
            return run_with_popup(make_coro, service.stop, approver, log)
        finally:
            if tray is not None:
                tray.stop()

    try:
        return asyncio.run(make_coro())
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130
    except Exception:
        log.exception("proxy crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
