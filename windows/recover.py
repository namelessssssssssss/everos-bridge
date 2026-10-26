"""Check/repair an existing Windows Codex installation at user logon.

This optional entry point requires Python 3.11+ for strict TOML parsing. It does
not fetch source, upgrade Codex, grant hook trust, or run a standalone MCP server.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys

from config_helpers import write_text
from install import WindowsInstaller
from platform_runtime import windows_rules
import win_rules as rulebook
import windows_lock
from provenance import verify as verify_source


def expected_runtime(installer):
    preview = WindowsInstaller(installer.repo, dry=True)
    with redirect_stdout(io.StringIO()):
        return preview.snapshot(validate_only=True)


def inspect(installer):
    import tomllib
    runtime = expected_runtime(installer)
    codex = Path(installer.environment.get("CODEX_HOME", str(installer.home / ".codex")))
    path = codex / "config.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    issues = []
    mcp = config.get("mcp_servers", {}).get("everos", {})
    if (str(runtime / "plugin/everos/mcp_server.py") not in mcp.get("args", [])
            or mcp.get("command") != str(installer.python)
            or mcp.get("env", {}).get("EVEROS_HOME") != str(installer.everos)
            or mcp.get("enabled") is False
            or mcp.get("tool_timeout_sec", 0) < installer.mcp_timeout_seconds()):
        issues.append("mcp")
    market = config.get("marketplaces", {}).get("everos-bridge", {})
    source = market.get("source")
    try:
        same_marketplace = isinstance(source, str) and Path(source).samefile(runtime)
    except OSError:
        same_marketplace = False
    if not same_marketplace:
        issues.append("marketplace")
    if config.get("plugins", {}).get("everos@everos-bridge", {}).get("enabled") is not True:
        issues.append("plugin")
    if config.get("features", {}).get("hooks") is not True:
        issues.append("hooks")
    if not runtime.exists():
        issues.append("runtime")
    # install_cli resolves the runtime repo first (including Windows short paths).
    # Reuse its generator rather than compare against a different lexical alias.
    for name, text in WindowsInstaller(runtime, dry=True).cli_scripts().items():
        target = installer.home / ".local/bin" / name
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            issues.append("cli")
            break
    override = codex / "AGENTS.override.md"
    native = override if override.exists() and override.read_text(encoding="utf-8").strip() else codex / "AGENTS.md"
    if native.is_symlink():
        raise ValueError("Resolve native rule symlink before recovery: " + str(native))
    old = native.read_text(encoding="utf-8") if native.exists() else ""
    docs = {name: windows_rules(text) for name, text in rulebook.bundle(installer.repo / "plugin").items()}
    if rulebook.managed_text(old, docs) != old:
        issues.append("native_rules")
    protocol = installer.everos / "PROTOCOL.md"
    if not protocol.is_file() or protocol.read_text(encoding="utf-8") != (installer.repo / "PROTOCOL.md").read_text(encoding="utf-8"):
        issues.append("protocol")
    return issues, runtime


def recover(installer, check_only=False):
    verify_source(installer)
    issues, runtime = inspect(installer)
    mode = "none"
    if issues and not check_only:
        if set(issues) <= {"native_rules", "protocol"}:
            mode = "rules"
            installer.sync_rules()
            installer.protocol_link()
            installer.record_source(runtime)
        else:
            mode = "install"
            installer.install()
        remaining, runtime = inspect(installer)
        if remaining:
            raise ValueError("Recovery readback failed: " + ", ".join(remaining))
    return {"ok": not issues or not check_only, "issues": issues, "repair": mode, "runtime": str(runtime)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without repairing")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info < (3, 11):
        parser.error("Logon recovery requires native Windows and Python >= 3.11")
    installer = WindowsInstaller(Path(__file__).resolve().parents[1])
    folder = installer.everos / "state/autostart"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "recovery.lock").open("a+b") as lock:
        try:
            windows_lock.flock(lock, windows_lock.LOCK_EX | windows_lock.LOCK_NB)
        except BlockingIOError:
            print("Another EverOS recovery is already running.")
            return 0
        try:
            result = recover(installer, args.check)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        write_text(folder / "last-result.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
