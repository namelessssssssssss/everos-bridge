"""Native Windows Kimi Work desktop wiring over the shared runtime snapshot."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import sys

from config_helpers import table_headers
from host import WindowsHost
from platform_runtime import windows_rules
import win_rules as rulebook


def decoded_command(command):
    """Decode our PowerShell hook wrapper; accept older plaintext hook commands."""
    parts = command.split()
    lowered = [part.lower() for part in parts]
    if parts and "powershell.exe" in parts[0].lower() and "-encodedcommand" in lowered:
        try:
            token = parts[lowered.index("-encodedcommand") + 1]
            return base64.b64decode(token, validate=True).decode("utf-16le")
        except (IndexError, ValueError, UnicodeDecodeError):
            return ""
    return command


def is_everos_hook(command):
    text = decoded_command(command)
    return bool(re.search(r"(?:^|[/\\\s'\"])everos_hook\.py(?:$|[\s'\"])", text)
                or re.search(r"(?:^|[/\\]+)everos[/\\]+hook\.py(?:$|[\s'\"])", text))


def _toml_command(lines, start, end):
    for line in lines[start + 1:end]:
        if re.match(r"^\s*command\s*=", line):
            raw = line.split("=", 1)[1].strip()
            try:
                return json.loads(raw)
            except ValueError:
                return raw.strip("\"'")
    return ""


def kimi_hooks(text, command, recall_timeout=190):
    """Replace only EverOS handlers and preserve all unrelated Kimi hooks."""
    lines = text.splitlines(keepends=True)
    headers = table_headers(text)
    remove = set()
    for offset, (start, header) in enumerate(headers):
        if header != "[[hooks]]":
            continue
        end = headers[offset + 1][0] if offset + 1 < len(headers) else len(lines)
        if is_everos_hook(_toml_command(lines, start, end)):
            remove.update(range(start, end))
    existing = "".join(line for index, line in enumerate(lines) if index not in remove).rstrip()
    blocks = []
    for event, timeout in (("UserPromptSubmit", recall_timeout), ("Stop", 10), ("PreCompact", 10), ("SessionEnd", 5)):
        blocks.append('[[hooks]]\nevent = "{}"\ncommand = {}\ntimeout = {}'.format(
            event, json.dumps(command), timeout))
    return existing + ("\n\n" if existing else "") + "\n\n".join(blocks) + "\n"


class KimiDesktopHost(WindowsHost):
    def __init__(self, repo, dry=False):
        super().__init__(repo, dry)
        self.python = Path(sys.executable).resolve()
        self.runtime = self.repo

    def desktop_config(self):
        override = self.environment.get("EVEROS_KIMI_DESKTOP_CONFIG")
        if override:
            return Path(override)
        share = self.environment.get("KIMI_SHARE_DIR")
        if share:
            return Path(share) / "daimon/runtime/kimi-code/config.toml"
        appdata = Path(self.environment.get("APPDATA") or (self.home / "AppData/Roaming"))
        return appdata / "kimi-desktop/daimon-share/daimon/runtime/kimi-code/config.toml"

    def desktop_home(self):
        return Path(self.environment.get("EVEROS_KIMI_DESKTOP_HOME") or (self.desktop_config().parent / "home"))

    def install(self, command):
        config = self.desktop_config()
        if not config.is_file():
            raise ValueError("未找到 Kimi Work 桌面端 config.toml；请先启动桌面端或设置 EVEROS_KIMI_DESKTOP_CONFIG：" + str(config))
        print("== Kimi Work 桌面端（原生 Windows）")
        self.mcp_json()
        text = config.read_text(encoding="utf-8")
        self.edit(config, kimi_hooks(text, command, self.mcp_timeout_seconds()))
        self.sync_daimon_skill(config)
        self.sync_rules()
        print("== 自检")
        self.run([self.python, "-X", "utf8", self.runtime / "plugin/everos/hook.py", "--doctor"])
        print("预览完成；未写配置或执行 doctor。" if self.dry else
              "接线与自检完成。请在新会话验证自动注入和对话采集。")

    def mcp_json(self):
        path = self.desktop_home() / "mcp.json"
        data = json.loads(path.read_text(encoding="utf-8").strip() or "{}") if path.exists() else {}
        data.setdefault("mcpServers", {})["everos"] = {
            "command": str(self.python),
            "args": ["-X", "utf8", str(self.runtime / "plugin/everos/mcp_server.py")],
            "env": {"EVEROS_HOME": str(self.everos), "PYTHONUTF8": "1"},
        }
        self.edit(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def sync_daimon_skill(self, desktop):
        source = self.repo / "plugin/skills/everos-memory/SKILL.md"
        target = desktop.parents[2] / "skills/everos-memory/SKILL.md"
        marker = self.everos / "state/install/everos-memory-daimon.sha256"
        text = source.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = target.read_text(encoding="utf-8") if target.exists() else None
        old_digest = hashlib.sha256(old.encode()).hexdigest() if old is not None else None
        last = marker.read_text(encoding="utf-8").strip() if marker.exists() else None
        if old_digest == digest:
            print("  daimon skill everos-memory 已是最新")
        else:
            if old is None:
                note = "将安装" if self.dry else "已安装"
            elif last == old_digest:
                note = "将同步更新" if self.dry else "已同步更新"
            else:
                note = "将备份并覆盖外部修改" if self.dry else "已备份并覆盖外部修改"
            print("  " + note + "：" + str(target))
            self.edit(target, text)
        if not self.dry:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(digest + "\n", encoding="utf-8")

    def sync_rules(self):
        documents = rulebook.bundle(self.repo / "plugin")
        documents = {name: windows_rules(text) for name, text in documents.items()}
        state = self.everos / "state/rules"
        previous = rulebook.load(state / "current.json")
        path = self.desktop_home() / "AGENTS.md"
        if path.is_symlink():
            raise ValueError("Resolve the Kimi Work instruction symlink before installing: " + str(path))
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        text = rulebook.managed_text(old, documents)
        self.edit(path, text)
        if not self.dry and path.read_text(encoding="utf-8") != text:
            raise ValueError("Kimi Work native rule readback mismatch: " + str(path))
        current = rulebook.transition(documents, previous)
        target = {"host": "kimi_desktop", "path": str(path),
                  "status": "preview" if self.dry else "file_verified", "changed": old != text}
        report = rulebook.load(state / "targets.json")
        retained = report.get("targets", []) if report.get("revision") == current["revision"] else []
        targets = [row for row in retained if row.get("host") != "kimi_desktop"] + [target]
        scope = sorted({row["host"] for row in targets if row.get("host")})
        self.edit(state / "current.json", json.dumps(current, ensure_ascii=False, indent=2) + "\n", 0o600)
        self.edit(state / "targets.json", json.dumps({"revision": current["revision"], "scope": scope,
                                                       "targets": targets}, ensure_ascii=False, indent=2) + "\n")
        print("  Windows native rules " + current["revision"][:12] + "; read back " + str(path))
