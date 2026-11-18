"""Tray icon state logic (no real system tray needed)."""

from pathlib import Path

from app.tray import ICON_SIZE, IDLE_COLOR, PENDING_COLOR, TrayIcon, make_image


class StubIcon:
    def __init__(self):
        self.icon = None
        self.title = ""
        self.menu_updates = 0

    def update_menu(self):
        self.menu_updates += 1


def make_tray():
    tray = TrayIcon("filesystem (Claude Desktop)", on_show=lambda: None, on_quit=lambda: None,
                    log_path=Path("logs/proxy.log"))
    tray._icon = StubIcon()
    return tray


def test_images_are_distinct_shields():
    idle, pending = make_image(IDLE_COLOR), make_image(PENDING_COLOR)
    assert idle.size == pending.size == (ICON_SIZE, ICON_SIZE)
    assert idle.getpixel((32, 10))[:3] == IDLE_COLOR[:3]
    assert pending.getpixel((32, 10))[:3] == PENDING_COLOR[:3]
    assert idle.getpixel((1, 1))[3] == 0  # transparent corner


def test_pending_count_switches_icon_and_tooltip():
    tray = make_tray()
    assert "nothing pending" in tray.title()

    tray.set_pending(2)
    assert tray._icon.icon is tray._images[True]
    assert tray._icon.title == "MCP Guard - filesystem (Claude Desktop): 2 pending approval(s)"

    tray.set_pending(0)
    assert tray._icon.icon is tray._images[False]
    assert "nothing pending" in tray._icon.title
    assert tray._icon.menu_updates == 2


def test_set_pending_before_start_is_safe():
    tray = TrayIcon("x", on_show=lambda: None, on_quit=lambda: None, log_path=Path("x.log"))
    tray.set_pending(3)
    assert "3 pending" in tray.title()
