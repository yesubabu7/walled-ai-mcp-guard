# Walled AI MCP Guard

A Python proxy that sits between an MCP client (Claude Desktop) and real MCP servers,
monitors every message, and (in later steps) asks the user to Allow or Block each tool call.

> Work in progress: **Steps 1–5 done** (transparent proxy, `tools/call` interception, Tkinter Allow/Block popup).

## Approval popup (Step 5)

```
main thread:   Tk mainloop ── TkinterApprover ── one always-on-top ApprovalPopup per pending call
                    ▲  polls every 50 ms                 │ Allow / Block / X
                    │                                    ▼
               ApprovalBridge (thread-safe queue + loop.call_soon_threadsafe)
                    ▲                                    │
proxy thread:  asyncio loop ── StdioProxy ── await approver.decide(request)
```

- Tk must own the main thread, so in popup mode the asyncio proxy runs on a worker thread.
- Each pending `tools/call` gets its own popup showing server, tool, request id, and pretty-printed arguments, plus a countdown to the automatic block.
- Each popup has Allow and Block buttons. Closing the window (X) or pressing Escape counts as **Block**. Keyboard focus starts on Block.
- When the proxy's timeout expires, or the client cancels the call, the popup is withdrawn. On shutdown, every open popup resolves as Block.

Try it manually (each tool call opens a popup):

```bash
python scripts/test_client.py demo
python scripts/concurrent_demo.py
```

## Interception (Step 4)

- Only `tools/call` requests are held. Everything else (`initialize`, `tools/list`, notifications, `ping`, ...) is forwarded immediately.
- Each held call gets its own asyncio task that awaits `Approver.decide()`. The read loop never waits, so other traffic and further tool calls keep flowing.
- **Allow:** the original request bytes are forwarded unchanged. The backend's response is matched to the request by its JSON-RPC id.
- **Block:** the backend never sees the request. The client receives a tool error result (`isError: true`) explaining that the call was blocked.
- **Fails closed:** approval timeouts, approver crashes, unparseable lines, and batches containing `tools/call` are all blocked or rejected.
- If the client sends `notifications/cancelled` for a pending call, the proxy drops it without sending a response.

Approvers (`--approver`):
- `popup` (default): Tkinter popup (`app/tk_approver.py`).
- `console`: Allow/Block prompt in the terminal window (`app/approval.py`).
- `--approver policy --allow echo --block add --default block`: static rules, used by the tests.

## Claude Desktop integration (Windows)

Claude Desktop launches every entry under `mcpServers` in `claude_desktop_config.json` as a child process
and talks MCP over its stdin/stdout. We register MCP Guard there instead of the filesystem server. MCP Guard
then starts the real server itself, from the `filesystem` entry in `config/servers.json`.

Config file location:

| Install type | Path |
|---|---|
| Microsoft Store (MSIX), newer builds | `%LOCALAPPDATA%\Claude-Data\claude_<id>\claude_desktop_config.json` |
| Microsoft Store (MSIX), older builds | `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json` |
| Classic installer | `%APPDATA%\Claude\claude_desktop_config.json` |

`scripts/claude_config.py` finds the file, backs it up to `backups/` before every write, and only touches its own entry:

```bash
python scripts/claude_config.py show        # dry run
python scripts/claude_config.py install     # quit Claude Desktop first, then reopen it
python scripts/claude_config.py uninstall   # remove only the MCP Guard entry
python scripts/claude_config.py restore     # put the newest backup back (or --from <file>)
```

**Pinning the server's directories (MCP Roots).** Clients that support MCP Roots, such as Claude Desktop's Code
sessions, send their own working folder. The filesystem server then *replaces* its command-line directories with
it, so the client can widen what the server may access. Backends marked `"ignore_client_roots": true` in
`config/servers.json` are protected from this. MCP Guard removes the `roots` capability from `initialize` and drops
`notifications/roots/list_changed`, so the server keeps its configured `sandbox`.

Prefer `uninstall` over `restore` for rollback. Claude Desktop keeps its preferences in the same file, so
restoring an old backup also rolls those preferences back.

## Claude Web integration (HTTP gateway)

Claude Web can't launch local programs. It only connects to **remote** MCP servers over HTTPS ("custom
connectors"), and those requests come from Anthropic's cloud. So MCP Guard has a second front end: a
**Streamable HTTP gateway** that reuses the same interception, approvers, and popup.

```
Claude Web ──HTTPS──▶ Cloudflare tunnel ──▶ MCP Guard gateway (127.0.0.1:8765, app/gateway.py)
                                                 │ one session = one guarded backend process
                                                 ▼
                                    StdioProxy (same tools/call interception + popup)
                                                 ▼
                                    filesystem MCP server (sandbox only)
```

- `POST /mcp/<token>` carries one JSON-RPC message. Requests get an SSE stream with the response. While a call waits for the popup, keep-alive comments are sent every 15 s. Notifications get `202`.
- `initialize` creates a session (`Mcp-Session-Id` header). `DELETE` ends it. Idle sessions are cleaned up after 30 min.
- **Security:**
  - **Token:** a secret token in the URL path is the credential (compared in constant time; a wrong token gets 404).
  - **Local only:** the gateway listens on `127.0.0.1` only, so the tunnel is the only way in.
  - **Origin check:** requests with a foreign browser `Origin` header are rejected.
- If Claude Web disconnects while a call is pending, the call is cancelled and its popup closes.

Run it:

```bash
python app/main.py --server filesystem --http --port 8765
# -> logs "Claude Web connector URL: https://<your-tunnel-host>/mcp/<token>"
#    (token is generated once and kept in config/gateway_token.txt, which is gitignored)

winget install --id Cloudflare.cloudflared       # once
cloudflared tunnel --url http://127.0.0.1:8765   # prints https://<random>.trycloudflare.com
```

In claude.ai, go to **Settings → Connectors → Add custom connector**. Enter the URL
`https://<random>.trycloudflare.com/mcp/<token>`, then enable the connector in a chat.

To test without Claude Web: `python scripts/http_client.py http://127.0.0.1:8765/mcp/<token> read_text_file "{\"path\": \"notes.txt\"}"`

**Limitations:**
- There is no server-to-client GET stream (the spec allows this). Server-initiated notifications such as `tools/list_changed` are dropped.
- Quick tunnels get a new hostname on every restart; a named Cloudflare tunnel keeps it stable.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

## How it works (Step 1)

```
Claude Desktop ──stdin/stdout──▶ app/main.py (StdioProxy) ──stdin/stdout──▶ real MCP server
                                        │
                                        └──▶ logs/proxy.log
```

- MCP's stdio transport is newline-delimited JSON-RPC 2.0, one message per line.
- The proxy launches the backend server as a subprocess, forwards lines in both directions, and logs each one.
- stdout carries protocol traffic only. Logs go to `logs/proxy.log` and stderr.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Try it

```bash
python scripts/test_client.py demo
python scripts/test_client.py filesystem
```

Backends are defined in `config/servers.json`. `{python}` and `{root}` are replaced with
the current interpreter and the project folder.
