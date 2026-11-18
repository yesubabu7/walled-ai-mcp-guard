"""System-tray icon so MCP Guard is visibly running in the background.

  * green shield  = running, nothing pending
  * amber shield  = N tool calls waiting for a decision (count in the tooltip)
  * menu: status, "Show pending popups", "Open log", "Quit MCP Guard"

pystray runs its own OS message loop on a daemon thread. Its menu callbacks fire on that
thread, so they must never touch Tk; they only call the thread-safe hooks we pass in
(`on_show`, `on_quit`), which post commands to the GUI thread.
"""

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

log = logging.getLogger("mcp_guard.tray")

IDLE_COLOR = (46, 125, 50, 255)      # green
PENDING_COLOR = (249, 168, 37, 255)  # amber
ICON_SIZE = 64


def make_image(color: tuple[int, int, int, int]) -> Image.Image:
    """A simple shield with a check mark, drawn at runtime (no asset files)."""
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.polygon([(32, 4), (58, 14), (54, 40), (32, 60), (10, 40), (6, 14)], fill=color)
    draw.line([(20, 32), (29, 42), (45, 22)], fill=(255, 255, 255, 255), width=6)
    return img


class TrayIcon:
    def __init__(self, label: str, on_show: Callable[[], None], on_quit: Callable[[], None], log_path: Path):
        self.label = label
        self._on_show = on_show
        self._on_quit = on_quit
        self._log_path = log_path
        self._pending = 0
        self._icon = None
        self._images = {False: make_image(IDLE_COLOR), True: make_image(PENDING_COLOR)}

    def title(self) -> str:
        state = f"{self._pending} pending approval(s)" if self._pending else "watching, nothing pending"
        return f"MCP Guard - {self.label}: {state}"

    def start(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem(lambda _item: self.title(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show pending popups", lambda: self._on_show(), default=True),
            pystray.MenuItem("Open log", lambda: self.open_log()),
            pystray.MenuItem("Quit MCP Guard (blocks pending calls)", lambda: self._on_quit()),
        )
        self._icon = pystray.Icon("mcp-guard", self._images[False], self.title(), menu)
        threading.Thread(target=self._icon.run, name="tray", daemon=True).start()
        log.info("tray icon started (%s)", self.label)

    def set_pending(self, count: int) -> None:
        """Safe to call from any thread."""
        self._pending = count
        if self._icon is not None:
            self._icon.icon = self._images[count > 0]
            self._icon.title = self.title()
            self._icon.update_menu()

    def open_log(self) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(self._log_path)  # noqa: S606 - opening our own log file
            else:
                webbrowser.open(self._log_path.as_uri())
        except OSError:
            log.exception("could not open log %s", self._log_path)

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
            self._icon = None
