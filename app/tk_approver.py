"""Tkinter Allow/Block popup approver.

Threading model
---------------
Tk must only be touched from the thread that created it, and on some platforms that
must be the main thread. So in popup mode:

    main thread   : Tk mainloop (TkinterApprover.run)       - owns every widget
    proxy thread  : asyncio event loop (StdioProxy.run)     - calls approver.decide()

They communicate only through ApprovalBridge:
  * decide()  -> puts an ("open", ticket, request, resolve) command on a thread-safe queue
                 and awaits an asyncio Future.
  * GUI       -> polls the queue every 50 ms, opens one popup per request.
  * click     -> resolve(decision) hands the result back with loop.call_soon_threadsafe.
  * timeout / cancellation on the asyncio side -> a ("close", ticket) command removes the popup.

Nothing here writes to stdout (reserved for MCP JSON-RPC); logs go to the logger.
"""

import asyncio
import itertools
import json
import logging
import queue
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from app.approval import Approver, Decision, ToolCallRequest

log = logging.getLogger("mcp_guard.ui")

Resolver = Callable[[Decision], None]

ARGUMENTS_LIMIT = 20_000


def format_arguments(arguments: dict) -> str:
    text = json.dumps(arguments, indent=2, ensure_ascii=False, sort_keys=True)
    if len(text) > ARGUMENTS_LIMIT:
        text = text[:ARGUMENTS_LIMIT] + f"\n... (truncated, {len(text)} characters total)"
    return text or "{}"


class ApprovalBridge:
    """Thread-safe hand-off between asyncio (requests) and the GUI thread (decisions)."""

    def __init__(self):
        self._commands: queue.Queue = queue.Queue()
        self._tickets = itertools.count(1)

    async def request(self, request: ToolCallRequest) -> Decision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        ticket = next(self._tickets)

        def resolve(decision: Decision) -> None:  # called on the GUI thread
            def settle() -> None:
                if not future.done():
                    future.set_result(decision)

            try:
                loop.call_soon_threadsafe(settle)
            except RuntimeError:
                pass  # event loop already closed: the proxy is shutting down

        self._commands.put(("open", ticket, request, resolve))
        try:
            return await future
        finally:
            if future.cancelled() or not future.done():
                # Timed out or cancelled by the client: take the popup down.
                self._commands.put(("close", ticket))

    def shutdown(self) -> None:
        self._commands.put(("shutdown",))

    def post(self, command: tuple) -> None:
        self._commands.put(command)

    def drain(self) -> list[tuple]:
        commands = []
        while True:
            try:
                commands.append(self._commands.get_nowait())
            except queue.Empty:
                return commands


class ApprovalPopup(tk.Toplevel):
    """One always-on-top window per pending tool call."""

    def __init__(self, master: tk.Misc, request: ToolCallRequest, on_decision: Resolver,
                 timeout: float | None = None, offset: int = 0):
        super().__init__(master)
        self.request = request
        self._on_decision = on_decision
        self._decided = False
        self._tick_job: str | None = None
        self._deadline = time.monotonic() + timeout if timeout else None

        self.title(f"MCP Guard - approve '{request.tool}'?")
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.block)  # closing the window = Block
        self.bind("<Escape>", lambda _e: self.block())

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=1)

        ttk.Label(frame, text="An MCP tool call is waiting for your approval",
                  font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        for row, (label, value) in enumerate(
            [("Server", request.server), ("Tool", request.tool), ("Request ID", str(request.request_id))], start=1
        ):
            ttk.Label(frame, text=f"{label}:").grid(row=row, column=0, sticky="nw", padx=(0, 10))
            ttk.Label(frame, text=value, font=("Consolas", 10, "bold")).grid(row=row, column=1, sticky="w")

        ttk.Label(frame, text="Arguments:").grid(row=4, column=0, sticky="nw", pady=(10, 2))
        args_frame = ttk.Frame(frame)
        args_frame.grid(row=5, column=0, columnspan=2, sticky="nsew")
        self.arguments_box = tk.Text(args_frame, width=72, height=14, wrap="word", font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(args_frame, command=self.arguments_box.yview)
        self.arguments_box.configure(yscrollcommand=scrollbar.set)
        self.arguments_box.insert("1.0", format_arguments(request.arguments))
        self.arguments_box.configure(state="disabled")
        self.arguments_box.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.countdown = ttk.Label(frame, text="", foreground="#666666")
        self.countdown.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.block_button = tk.Button(buttons, text="Block", width=12, command=self.block,
                                      bg="#c62828", fg="white", activebackground="#8e0000", activeforeground="white")
        self.allow_button = tk.Button(buttons, text="Allow", width=12, command=self.allow,
                                      bg="#2e7d32", fg="white", activebackground="#005005", activeforeground="white")
        self.block_button.pack(side="left", padx=(0, 8))
        self.allow_button.pack(side="left")

        self.geometry(f"+{140 + offset}+{120 + offset}")
        self.lift()
        self.focus_force()
        self.block_button.focus_set()  # safe default: Space on a fresh popup blocks
        self.bell()
        if self._deadline is not None:
            self._tick()

    def allow(self) -> None:
        self._decide(Decision.ALLOW)

    def block(self) -> None:
        self._decide(Decision.BLOCK)

    def _decide(self, decision: Decision) -> None:
        if self._decided:
            return
        self._decided = True
        self._on_decision(decision)

    def _tick(self) -> None:
        remaining = max(0, int(self._deadline - time.monotonic()))
        self.countdown.configure(text=f"Blocks automatically in {remaining}s if no decision is made.")
        self._tick_job = self.after(1000, self._tick)

    def destroy(self) -> None:
        if self._tick_job is not None:
            self.after_cancel(self._tick_job)
            self._tick_job = None
        super().destroy()


class TkinterApprover(Approver):
    POLL_MS = 50

    def __init__(self, timeout: float | None = None, root: tk.Tk | None = None):
        # Must be constructed on the GUI (main) thread. Raises tk.TclError without a display.
        self.bridge = ApprovalBridge()
        self.timeout = timeout
        self.root = root if root is not None else tk.Tk()
        self.root.withdraw()  # hidden root; only the popups are shown
        self.popups: dict[int, ApprovalPopup] = {}
        self._stopping = False
        # Called on the GUI thread with the number of open popups whenever it changes (tray icon).
        self.on_change: Callable[[int], None] | None = None

    # ---- called from the asyncio thread ----

    async def decide(self, request: ToolCallRequest) -> Decision:
        return await self.bridge.request(request)

    def stop(self) -> None:
        """Thread-safe: close all popups (as Block) and end the GUI loop."""
        self.bridge.shutdown()

    def show_all(self) -> None:
        """Thread-safe: bring every pending popup back to the front."""
        self.bridge.post(("show_all",))

    # ---- GUI thread ----

    def run(self) -> None:
        self.root.after(self.POLL_MS, self._poll)
        try:
            self.root.mainloop()
        finally:
            self.root.destroy()

    def pump(self) -> None:
        """Process queued commands once (used by the poll loop and by tests)."""
        before = len(self.popups)
        for command in self.bridge.drain():
            self._handle(command)
        self._notify_if_changed(before)

    def _notify_if_changed(self, before: int) -> None:
        if self.on_change is not None and len(self.popups) != before:
            try:
                self.on_change(len(self.popups))
            except Exception:
                log.exception("on_change callback failed")

    def _poll(self) -> None:
        self.pump()
        if not self._stopping:
            self.root.after(self.POLL_MS, self._poll)

    def _handle(self, command: tuple) -> None:
        kind = command[0]
        if kind == "open":
            _, ticket, request, resolve = command
            if self._stopping:
                resolve(Decision.BLOCK)
                return
            offset = (len(self.popups) % 8) * 30  # cascade simultaneous popups
            self.popups[ticket] = ApprovalPopup(
                self.root, request, lambda d: self._finish(ticket, resolve, d), self.timeout, offset
            )
            log.info("popup opened for %s/%s id=%s", request.server, request.tool, request.request_id)
        elif kind == "close":
            popup = self.popups.pop(command[1], None)
            if popup is not None:
                popup.destroy()
                log.info("popup for id=%s withdrawn (timed out or cancelled)", popup.request.request_id)
        elif kind == "show_all":
            for popup in self.popups.values():
                popup.deiconify()
                popup.lift()
                popup.attributes("-topmost", True)
                popup.focus_force()
        elif kind == "shutdown":
            self._stopping = True
            for popup in list(self.popups.values()):
                popup.block()
            self.root.quit()

    def _finish(self, ticket: int, resolve: Resolver, decision: Decision) -> None:
        before = len(self.popups)
        popup = self.popups.pop(ticket, None)
        if popup is not None:
            log.info("user chose %s for id=%s", decision.value.upper(), popup.request.request_id)
            popup.destroy()
        resolve(decision)
        self._notify_if_changed(before)
