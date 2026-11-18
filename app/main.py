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
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Allow running as a plain script (Claude Desktop launches us by file path).
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from app.approval import Approver, ConsoleApprover, Decision, PolicyApprover  # noqa: E402
from app.logger import setup_logging  # noqa: E402
from app.proxy import DEFAULT_APPROVAL_TIMEOUT, StdioProxy  # noqa: E402

DEFAULT_CONFIG = ROOT / "config" / "servers.json"
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


def run_with_popup(proxy: StdioProxy, approver, log) -> int:
    """Tk owns the main thread; the asyncio proxy runs on a worker thread."""
    outcome = {"code": 1}

    def proxy_thread() -> None:
        try:
            outcome["code"] = asyncio.run(proxy.run())
        except Exception:
            log.exception("proxy crashed")
        finally:
            approver.stop()  # proxy finished (client disconnected or crash): end the GUI loop

    def on_sigint(*_args) -> None:
        log.info("interrupted")
        outcome["code"] = 130
        approver.stop()

    signal.signal(signal.SIGINT, on_sigint)  # delivered on the main thread between Tk polls
    worker = threading.Thread(target=proxy_thread, name="proxy-loop")
    worker.start()
    approver.run()
    proxy.stop()  # no-op if the proxy already finished
    worker.join(WORKER_JOIN_TIMEOUT)
    if worker.is_alive():
        log.error("proxy thread did not stop within %.0fs", WORKER_JOIN_TIMEOUT)
    return outcome["code"]


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

    proxy = StdioProxy(
        name=args.server,
        command=expand(cfg["command"]),
        args=[expand(a) for a in cfg.get("args", [])],
        approver=approver,
        env=cfg.get("env"),
        approval_timeout=args.approval_timeout,
        client_out=protocol_out,
        ignore_client_roots=bool(cfg.get("ignore_client_roots", False)),
    )

    if args.approver == "popup":
        return run_with_popup(proxy, approver, log)

    try:
        return asyncio.run(proxy.run())
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130
    except Exception:
        log.exception("proxy crashed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
