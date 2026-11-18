"""Safely add/remove the MCP Guard entry in Claude Desktop's config.

    python scripts/claude_config.py show        # dry run: path, current and proposed mcpServers
    python scripts/claude_config.py install     # backup, then add/replace ONLY our entry
    python scripts/claude_config.py uninstall   # backup, then remove ONLY our entry
    python scripts/claude_config.py restore [--from FILE]   # put a backup back (default: newest)

Every write is preceded by a timestamped copy in backups/ and is atomic (temp file +
replace). All other keys in the file (preferences etc.) are preserved untouched.
Quit Claude Desktop completely before install/uninstall/restore, then start it again.
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backups"
DEFAULT_ENTRY_NAME = "filesystem-guarded"
DEFAULT_BACKEND = "filesystem"


def candidate_paths() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming = Path(os.environ.get("APPDATA", ""))
    return [
        # Microsoft Store (MSIX) builds that relocated their data:
        *sorted(local.glob("Claude-Data/claude_*/claude_desktop_config.json")),
        # MSIX builds with virtualized AppData:
        *sorted(local.glob("Packages/Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json")),
        # Classic installer:
        roaming / "Claude" / "claude_desktop_config.json",
    ]


def find_config(override: str | None) -> Path:
    if override:
        return Path(override)
    for path in candidate_paths():
        if path.is_file():
            return path
    sys.exit("Claude Desktop config not found. Open Claude Desktop once, or pass --path.\nLooked in:\n  "
             + "\n  ".join(str(p) for p in candidate_paths()))


def guard_entry(backend: str) -> dict:
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python = venv_python if venv_python.exists() else Path(sys.executable)
    return {"command": str(python), "args": [str(ROOT / "app" / "main.py"), "--server", backend]}


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON ({exc}); refusing to modify it.")


def backup(path: Path, label: str) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / f"claude_desktop_config.{time.strftime('%Y%m%d-%H%M%S')}.{label}.json"
    shutil.copy2(path, target)
    print(f"backup written: {target}")
    return target


def write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".mcp-guard.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def print_servers(title: str, data: dict) -> None:
    print(f"\n{title}:")
    print(json.dumps({"mcpServers": data.get("mcpServers", {})}, indent=2))


def cmd_show(args) -> None:
    path = find_config(args.path)
    data = load(path)
    print(f"config file: {path}")
    print(f"other top-level keys (kept as-is): {[k for k in data if k != 'mcpServers']}")
    print_servers("current", data)
    proposed = dict(data)
    proposed["mcpServers"] = {**data.get("mcpServers", {}), args.name: guard_entry(args.backend)}
    print_servers("proposed", proposed)


def cmd_install(args) -> None:
    path = find_config(args.path)
    data = load(path)
    servers = data.get("mcpServers", {})
    entry = guard_entry(args.backend)
    if args.name in servers and servers[args.name] != entry and not args.force:
        sys.exit(f"an mcpServers entry named {args.name!r} already exists with different settings; "
                 "use --force to replace it or --name to pick another name.")
    backup(path, "pre-install")
    data["mcpServers"] = {**servers, args.name: entry}
    write_atomic(path, data)
    print(f"added mcpServers.{args.name} to {path}")
    print_servers("now", data)
    print("\nRestart Claude Desktop (quit from the tray icon, then reopen) to load it.")


def cmd_uninstall(args) -> None:
    path = find_config(args.path)
    data = load(path)
    servers = data.get("mcpServers", {})
    if args.name not in servers:
        print(f"no mcpServers.{args.name} entry; nothing to do.")
        return
    backup(path, "pre-uninstall")
    del servers[args.name]
    if servers:
        data["mcpServers"] = servers
    else:
        data.pop("mcpServers", None)
    write_atomic(path, data)
    print(f"removed mcpServers.{args.name}; all other settings unchanged.")


def cmd_restore(args) -> None:
    path = find_config(args.path)
    if args.source:
        source = Path(args.source)
    else:
        backups = sorted(BACKUP_DIR.glob("claude_desktop_config.*.json"))
        if not backups:
            sys.exit(f"no backups in {BACKUP_DIR}")
        source = backups[-1]
    load(source)  # validate before overwriting anything
    backup(path, "pre-restore")
    shutil.copy2(source, path)
    print(f"restored {source} -> {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", help="explicit path to claude_desktop_config.json")
    parser.add_argument("--name", default=DEFAULT_ENTRY_NAME, help="mcpServers entry name")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="backend name from config/servers.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show").set_defaults(func=cmd_show)
    p_install = sub.add_parser("install")
    p_install.add_argument("--force", action="store_true")
    p_install.set_defaults(func=cmd_install)
    sub.add_parser("uninstall").set_defaults(func=cmd_uninstall)
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("--from", dest="source", help="backup file to restore (default: newest)")
    p_restore.set_defaults(func=cmd_restore)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
