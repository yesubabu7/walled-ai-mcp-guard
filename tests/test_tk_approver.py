"""Tests for the Tkinter approver.

* ApprovalBridge / formatting tests need no display and always run.
* Popup tests drive real Tk widgets (button.invoke, WM_DELETE_WINDOW) and are skipped
  when no display is available (headless CI).
"""

import asyncio
import json
import threading
import time

import pytest

from app.approval import Decision, ToolCallRequest
from app.tk_approver import ARGUMENTS_LIMIT, ApprovalBridge, TkinterApprover, format_arguments


def make_request(request_id=1, tool="read_file", **arguments) -> ToolCallRequest:
    return ToolCallRequest(server="filesystem", request_id=request_id, tool=tool,
                           arguments=arguments or {"path": "C:/secret/.env"})


# ---------------------------------------------------------------- no display needed

def test_format_arguments_is_pretty_json():
    text = format_arguments({"path": "a.txt", "options": {"lines": 5}})
    assert json.loads(text) == {"path": "a.txt", "options": {"lines": 5}}
    assert "\n  " in text


def test_format_arguments_truncates_huge_payloads():
    text = format_arguments({"content": "x" * (ARGUMENTS_LIMIT * 2)})
    assert len(text) < ARGUMENTS_LIMIT + 100
    assert "truncated" in text


def fake_gui(bridge: ApprovalBridge, expected: int, choose) -> threading.Thread:
    """Plays the GUI thread: waits for `expected` popups, then answers them in reverse order."""

    def run():
        opened = []
        deadline = time.monotonic() + 5
        while len(opened) < expected and time.monotonic() < deadline:
            opened += [c for c in bridge.drain() if c[0] == "open"]
            time.sleep(0.01)
        for _, _ticket, request, resolve in reversed(opened):
            resolve(choose(request))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_bridge_returns_gui_decision():
    async def scenario():
        bridge = ApprovalBridge()
        fake_gui(bridge, 1, lambda _r: Decision.ALLOW)
        return await bridge.request(make_request())

    assert asyncio.run(scenario()) is Decision.ALLOW


def test_bridge_handles_concurrent_requests_answered_out_of_order():
    async def scenario():
        bridge = ApprovalBridge()
        fake_gui(bridge, 3, lambda r: Decision.ALLOW if r.tool == "read_file" else Decision.BLOCK)
        return await asyncio.gather(
            bridge.request(make_request(1, "read_file")),
            bridge.request(make_request(2, "write_file")),
            bridge.request(make_request(3, "read_file")),
        )

    assert asyncio.run(scenario()) == [Decision.ALLOW, Decision.BLOCK, Decision.ALLOW]


def test_bridge_timeout_withdraws_popup():
    bridge = ApprovalBridge()

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bridge.request(make_request()), 0.05)

    asyncio.run(scenario())
    commands = bridge.drain()
    assert [c[0] for c in commands] == ["open", "close"]
    assert commands[0][1] == commands[1][1]  # same ticket


def test_late_decision_after_loop_closed_is_ignored():
    bridge = ApprovalBridge()

    async def scenario():
        task = asyncio.ensure_future(bridge.request(make_request()))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    open_cmd = bridge.drain()[0]
    open_cmd[3](Decision.ALLOW)  # user clicks after the proxy shut down: must not raise


# ---------------------------------------------------------------- real Tk widgets

@pytest.fixture(scope="session")
def tk_root():
    # One root for the whole session, like the real app: creating many Tk() instances in
    # one process intermittently fails on Windows CPython (init.tcl load error).
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available: {exc}")
    yield root
    root.destroy()


@pytest.fixture
def approver(tk_root):
    instance = TkinterApprover(timeout=60, root=tk_root)
    yield instance
    for popup in list(instance.popups.values()):
        popup.destroy()


def decide_in_background(approver: TkinterApprover, request: ToolCallRequest, timeout: float | None = None):
    """Run approver.decide() on its own asyncio loop/thread, like the proxy does."""
    result: dict = {}

    def worker():
        async def go():
            if timeout is None:
                return await approver.decide(request)
            return await asyncio.wait_for(approver.decide(request), timeout)

        try:
            result["decision"] = asyncio.run(go())
        except asyncio.TimeoutError:
            result["decision"] = "timeout"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, result


def pump_until(approver: TkinterApprover, condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        approver.pump()
        approver.root.update()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def finish(thread: threading.Thread, result: dict, approver: TkinterApprover):
    pump_until(approver, lambda: not thread.is_alive())
    return result["decision"]


def test_popup_shows_request_details_and_is_topmost(approver):
    thread, result = decide_in_background(approver, make_request(42, "read_file", path="C:/secret/.env"))
    pump_until(approver, lambda: len(approver.popups) == 1)
    popup = next(iter(approver.popups.values()))

    assert int(popup.attributes("-topmost")) == 1
    assert "read_file" in popup.title()
    args_text = popup.arguments_box.get("1.0", "end")
    assert '"path": "C:/secret/.env"' in args_text
    labels = [w.cget("text") for w in popup.winfo_children()[0].winfo_children() if w.winfo_class() == "TLabel"]
    assert {"filesystem", "read_file", "42"} <= set(labels)

    popup.block_button.invoke()
    assert finish(thread, result, approver) is Decision.BLOCK


def test_allow_button(approver):
    thread, result = decide_in_background(approver, make_request())
    pump_until(approver, lambda: approver.popups)
    next(iter(approver.popups.values())).allow_button.invoke()
    assert finish(thread, result, approver) is Decision.ALLOW
    assert approver.popups == {}


def test_block_button(approver):
    thread, result = decide_in_background(approver, make_request())
    pump_until(approver, lambda: approver.popups)
    next(iter(approver.popups.values())).block_button.invoke()
    assert finish(thread, result, approver) is Decision.BLOCK


def test_closing_window_counts_as_block(approver):
    thread, result = decide_in_background(approver, make_request())
    pump_until(approver, lambda: approver.popups)
    popup = next(iter(approver.popups.values()))
    approver.root.tk.call(popup.protocol("WM_DELETE_WINDOW"))  # what the X button triggers
    assert finish(thread, result, approver) is Decision.BLOCK


def test_each_request_gets_its_own_popup(approver):
    t1, r1 = decide_in_background(approver, make_request(1, "read_file"))
    t2, r2 = decide_in_background(approver, make_request(2, "write_file"))
    pump_until(approver, lambda: len(approver.popups) == 2)

    by_tool = {p.request.tool: p for p in approver.popups.values()}
    by_tool["write_file"].block_button.invoke()
    by_tool["read_file"].allow_button.invoke()

    assert finish(t1, r1, approver) is Decision.ALLOW
    assert finish(t2, r2, approver) is Decision.BLOCK


def test_timeout_removes_popup(approver):
    thread, result = decide_in_background(approver, make_request(), timeout=0.3)
    pump_until(approver, lambda: approver.popups)
    assert finish(thread, result, approver) == "timeout"
    pump_until(approver, lambda: approver.popups == {})


def test_pending_count_reported_for_tray(approver):
    counts: list[int] = []
    approver.on_change = counts.append
    t1, r1 = decide_in_background(approver, make_request(1))
    t2, r2 = decide_in_background(approver, make_request(2))
    pump_until(approver, lambda: len(approver.popups) == 2)

    approver.show_all()  # tray "Show pending popups": must not disturb anything
    approver.pump()
    assert len(approver.popups) == 2

    for popup in list(approver.popups.values()):
        popup.allow_button.invoke()
    assert finish(t1, r1, approver) is Decision.ALLOW
    assert finish(t2, r2, approver) is Decision.ALLOW
    assert counts[-1] == 0 and max(counts) == 2


def test_stop_blocks_all_pending_popups(approver):
    t1, r1 = decide_in_background(approver, make_request(1))
    t2, r2 = decide_in_background(approver, make_request(2))
    pump_until(approver, lambda: len(approver.popups) == 2)
    approver.stop()
    assert finish(t1, r1, approver) is Decision.BLOCK
    assert finish(t2, r2, approver) is Decision.BLOCK
    assert approver.popups == {}
