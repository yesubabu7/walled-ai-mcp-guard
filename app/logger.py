"""Logging setup.

stdout is reserved for JSON-RPC traffic to the MCP client, so logs must never
go there. They go to logs/proxy.log and to stderr (Claude Desktop captures a
server's stderr into its own MCP log files).
"""

import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(process)d] %(name)s: %(message)s")

    root = logging.getLogger("mcp_guard")
    root.setLevel(level)
    if not root.handlers:
        file_handler = logging.FileHandler(LOG_DIR / "proxy.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        root.addHandler(stderr_handler)
    return root
