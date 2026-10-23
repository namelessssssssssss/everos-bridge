#!/usr/bin/env python3
"""Installer config editing and host CLI orchestration (standard library only)."""

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime


def backup(path):
    """Create a unique backup before any write, including host CLI writes."""
    path = Path(path)
    if not path.is_file():
        return
    suffix = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex
    destination = path.with_name(path.name + ".bak-everos-" + suffix)
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output, path.open("rb") as source:
        shutil.copyfileobj(source, output)
    original = path.stat()
    os.utime(destination, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.chmod(destination, original.st_mode & 0o600)


def write_text(path, text, mode=None):
    """Keep permissions and replace atomically; never truncate an existing file."""
    path = Path(path)
    old = path.read_text() if path.exists() else None
    if old == text and (mode is None or path.stat().st_mode & 0o777 == mode):
        return
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    permissions = mode if mode is not None else (path.stat().st_mode & 0o777 if path.exists() else 0o600)
    fd, temporary = tempfile.mkstemp(prefix=".everos-install-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
        os.chmod(temporary, permissions)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def env_values(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


# Table headers are scanned outside multiline TOML strings. Configs remain in
# their original formatting; Python 3.9 does not need a third-party TOML writer.
def active_toml_lines(text):
    result, multiline = [], None
    for index, line in enumerate(text.splitlines(keepends=True)):
        if multiline is None:
            result.append((index, line))
        # Track delimiters outside comments and basic strings.
        position, quote = 0, None
        while position < len(line):
            if multiline:
                end = line.find(multiline, position)
                if end < 0:
                    break
                position, multiline = end + 3, None
            elif quote:
                if line[position] == "\\" and quote == '"':
                    position += 2
                elif line[position] == quote:
                    quote, position = None, position + 1
                else:
                    position += 1
            elif line[position] == "#":
                break
            elif line[position:position + 3] in ('"""', "'''"):
                multiline, position = line[position:position + 3], position + 3
            elif line[position] in "\"'":
                quote, position = line[position], position + 1
            else:
                position += 1
    return result


def table_headers(text):
    result = []
    for index, line in active_toml_lines(text):
        match = re.match(r"^\s*(\[\[?.*?\]\]?)\s*(?:#.*)?$", line.rstrip())
        if match:
            header = match.group(1)
            size = 2 if header.startswith("[[") else 1
            header = "[" * size + header[size:-size].strip() + "]" * size
            result.append((index, header))
    return result


def enable_codex_hooks(text):
    lines = text.splitlines(keepends=True)
    headers = table_headers(text)
    active = {index for index, _ in active_toml_lines(text)}
    features = [index for index, header in headers if header in ("[features]", '["features"]', "['features']")]
    if len(features) > 1:
        raise ValueError("Codex config has duplicate [features] tables; repair it before installing")
    if features:
        start = features[0]
        end = next((index for index, _ in headers if index > start), len(lines))
        key = re.compile(r"^\s*(?:hooks|\"hooks\"|'hooks')\s*=")
        body = [line for index, line in enumerate(lines[start + 1:end], start + 1)
                if index not in active or not key.match(line)]
        if not lines[start].endswith("\n"):
            lines[start] += "\n"
        lines[start + 1:end] = ["hooks = true\n"] + body
        return "".join(lines)
    # Root-level dotted assignments cannot coexist with a newly added table.
    root_end = headers[0][0] if headers else len(lines)
    feature_key = r'(?:features|"features"|\'features\')'
    hook_key = r'(?:hooks|"hooks"|\'hooks\')'
    dotted = re.compile(r"^\s*" + feature_key + r"\s*\.\s*" + hook_key + r"\s*=")
    matches = [index for index in range(root_end) if index in active and dotted.match(lines[index])]
    if matches:
        first = matches[0]
        lines = [line for index, line in enumerate(lines) if index not in matches[1:]]
        lines[first] = "features.hooks = true\n"
        return "".join(lines)
    if any(index in active and re.match(r"^\s*" + feature_key + r"(?:\s*=|\s*\.)", line)
           for index, line in enumerate(lines[:root_end])):
        raise ValueError("Use a [features] table in Codex config before enabling hooks")
    return text.rstrip() + ("\n\n" if text.strip() else "") + "[features]\nhooks = true\n"


def _codex_everos_table(text):
    lines = text.splitlines(keepends=True)
    headers = table_headers(text)
    table = re.compile(r"^\[(?:mcp_servers|\"mcp_servers\"|'mcp_servers')\s*\.\s*(?:everos|\"everos\"|'everos')\]$")
    matches = [index for index, header in headers if table.match(header)]
    if len(matches) > 1:
        raise ValueError("Codex config has duplicate [mcp_servers.everos] tables")
    if not matches:
        return lines, None, None, []
    start = matches[0]
    end = next((index for index, _ in headers if index > start), len(lines))
    active = {index for index, _ in active_toml_lines(text)}
    key = re.compile(r"^\s*(?:tool_timeout_sec|\"tool_timeout_sec\"|'tool_timeout_sec')\s*=")
    keys = [index for index in range(start + 1, end) if index in active and key.match(lines[index])]
    if len(keys) > 1:
        raise ValueError("Codex everos tool_timeout_sec is duplicated")
    return lines, start, end, keys


def _positive_timeout(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " must be a positive finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(label + " must be a positive finite number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(label + " must be a positive finite number")
    return number


def codex_tool_timeout(text):
    lines, _, _, keys = _codex_everos_table(text)
    if not keys:
        return 0
    value = lines[keys[0]].split("=", 1)[1].split("#", 1)[0].strip().replace("_", "")
    return _positive_timeout(value, "Codex everos tool_timeout_sec")


def set_codex_tool_timeout(text, minimum):
    """Raise only EverOS's tool timeout, preserving larger user settings."""
    lines, start, _, keys = _codex_everos_table(text)
    if codex_tool_timeout(text) >= minimum:
        return text
    entry = "tool_timeout_sec = {:g}\n".format(minimum)
    if start is None:
        return text.rstrip() + ("\n\n" if text.strip() else "") + "[mcp_servers.everos]\n" + entry
    if keys:
        lines[keys[0]] = entry
    else:
        if not lines[start].endswith("\n"):
            lines[start] += "\n"
        lines.insert(start + 1, entry)
    return "".join(lines)


def is_everos_hook(command):
    return bool(re.search(r"(?:^|[/\\\s'\"])everos_hook\.py(?:$|[\s'\"])", command)
                or re.search(r"(?:^|[/\\])everos[/\\]hook\.py(?:$|[\s'\"])", command))


def kimi_hooks(text, hook, desktop=False, recall_timeout=190):
    lines = text.splitlines(keepends=True)
    headers = table_headers(text)
    remove = set()
    for offset, (start, header) in enumerate(headers):
        if header != "[[hooks]]":
            continue
        end = headers[offset + 1][0] if offset + 1 < len(headers) else len(lines)
        command = next((line.split("=", 1)[1].strip() for line in lines[start + 1:end]
                        if re.match(r"^\s*command\s*=", line)), "")
        if is_everos_hook(command):
            remove.update(range(start, end))
    existing = "".join(line for index, line in enumerate(lines) if index not in remove).rstrip()
    command = ("EVEROS_HOST=kimi_desktop " if desktop else "") + "python3 " + shlex.quote(str(hook))
    blocks = []
    for event, timeout in (("UserPromptSubmit", recall_timeout), ("Stop", 10), ("PreCompact", 10), ("SessionEnd", 5)):
        blocks.append('[[hooks]]\nevent = "{}"\ncommand = {}\ntimeout = {}'.format(event, json.dumps(command), timeout))
    return existing + ("\n\n" if existing else "") + "\n\n".join(blocks) + "\n"


def trae_hooks(text, hook, runtime, recall_timeout=190, tool_events=False):
    """Merge only EverOS handlers; preserve unrelated hooks and top-level fields."""
    data = json.loads(text or "{}")
    events = (("SessionStart", 10), ("UserPromptSubmit", recall_timeout), ("Stop", 10),
              ("PreCompact", 10), ("PostCompact", 5), ("SessionEnd", 5))
    if tool_events:
        events += (("PreToolUse", 3), ("PostToolUse", 3))
    canonical = {event.lower(): event for event, _ in events}
    merged = {}
    for event, groups in data.get("hooks", {}).items():
        event = canonical.get(re.sub(r"[_-]", "", event).lower(), event)
        retained = merged.setdefault(event, [])
        for group in groups:
            hooks = [h for h in group.get("hooks", []) if not is_everos_hook(h.get("command", ""))]
            if hooks or not group.get("hooks"):
                retained.append(dict(group, hooks=hooks))
    command = "EVEROS_HOST=trae TRAECLI_HOME={} python3 {}".format(
        shlex.quote(str(runtime)), shlex.quote(str(hook)))
    for event, timeout in events:
        merged.setdefault(event, []).append({"hooks": [{"type": "command", "command": command,
                                                       "timeout": timeout}]})
    data["hooks"] = merged
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


class Installer:
    def __init__(self, repo, dry=False, only=None):
        self.repo, self.dry = Path(repo).resolve(), dry
        self.only = only
        # Explicit test/alternate host root: do not overwrite the system HOME.
        self.home = Path(os.environ.get("EVEROS_INSTALL_HOME", str(Path.home())))
        self.everos = Path(os.environ.get("EVEROS_HOME", str(self.home / ".everos")))
        self.hook = self.repo / "plugin/everos/hook.py"
        self.mcp = self.repo / "plugin/everos/mcp_server.py"
        self.environment = dict(os.environ, EVEROS_HOME=str(self.everos))
        if "EVEROS_INSTALL_HOME" in os.environ:
            for key, suffix in (("CODEX_HOME", ".codex"), ("CLAUDE_CONFIG_DIR", ".claude"),
                                ("TRAE_HOME", ".trae"), ("TRAECLI_HOME", ".trae/cli"),
                                ("KIMI_CODE_HOME", ".kimi-code")):
                self.environment.setdefault(key, str(self.home / suffix))

    def run(self, args, capture=False):
        if self.dry:
            print("  [dry-run] " + shlex.join([str(arg) for arg in args]))
            return ""
        result = subprocess.run([str(arg) for arg in args], env=self.environment, text=True,
                                stdout=subprocess.PIPE if capture else None, check=True)
        return result.stdout if capture else ""

    def edit(self, path, text, mode=None):
        if self.dry:
            print("  [dry-run] 更新 " + str(path))
        else:
            write_text(path, text, mode)

    def before_cli(self, *paths):
        for path in paths:
            if self.dry:
                print("  [dry-run] CLI 写入前备份 " + str(path))
            else:
                backup(path)

    def mcp_timeout_seconds(self):
        """Use the runtime's global agentic budget plus host transport headroom."""
        config = {}
        path = self.everos / "config.env"
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    if key.strip() == "EVEROS_AGENTIC_TIMEOUT":
                        config[key.strip()] = value.strip()
        if self.environment.get("EVEROS_AGENTIC_TIMEOUT"):
            config["EVEROS_AGENTIC_TIMEOUT"] = self.environment["EVEROS_AGENTIC_TIMEOUT"]
        spec = importlib.util.spec_from_file_location("everos_install_core", self.repo / "plugin/everos/core.py")
        core = importlib.util.module_from_spec(spec)
        previous_bytecode = sys.dont_write_bytecode
        previous_path = sys.path[:]
        try:
            # Reading the shared validator must keep --dry-run free of cache writes.
            sys.dont_write_bytecode = True
            sys.path.insert(0, str(self.repo / "plugin/everos"))
            spec.loader.exec_module(core)
        finally:
            sys.dont_write_bytecode = previous_bytecode
            sys.path[:] = previous_path
        return math.ceil(core.agentic_timeout(config) + 10)

    @contextmanager
    def preserve_codex_cache(self):
        """Keep exact old hook paths usable for chats running during an upgrade."""
        codex = Path(self.environment.get("CODEX_HOME", str(self.home / ".codex")))
        cache = codex / "plugins/cache/everos-bridge/everos"
        if self.dry:
            print("  [dry-run] 保存并恢复旧版 Codex EverOS 缓存，供运行中的聊天使用")
            yield
            return
        versions = [path for path in cache.iterdir()
                    if path.is_dir() and (path / "everos/hook.py").is_file()] if cache.is_dir() else []
        if not versions:
            yield
            return
        saved = Path(tempfile.mkdtemp(prefix="everos-codex-cache-"))
        keep_saved = False
        try:
            for version in versions:
                shutil.copytree(version, saved / version.name, symlinks=True)
            try:
                yield
            finally:
                try:
                    for version in versions:
                        if version.exists() or version.is_symlink():
                            continue
                        cache.mkdir(parents=True, exist_ok=True)
                        with tempfile.TemporaryDirectory(prefix=".restore-", dir=str(cache)) as staging:
                            restored = Path(staging) / version.name
                            shutil.copytree(saved / version.name, restored, symlinks=True)
                            os.replace(restored, version)
                        print("  已恢复旧版缓存，供运行中的聊天使用：" + str(version))
                except OSError:
                    keep_saved = True
                    print("旧版缓存恢复失败，备份保留在 " + str(saved), file=sys.stderr)
                    raise
        finally:
            if not keep_saved:
                shutil.rmtree(saved)

    def config(self):
        path = self.everos / "config.env"
        print("== 配置（{}）".format(self.everos))
        old = path.read_text() if path.exists() else ""
        values, additions = env_values(old), {}
        for key in ("EVEROS_USER_ID", "EVEROS_API_KEY"):
            if not values.get(key):
                value = os.environ.get(key, "")
                if not value and self.dry:
                    print("  [dry-run] 缺少 {}，真实安装前须补齐".format(key))
                    continue
                if not value and sys.stdin.isatty():
                    value = getpass.getpass(key + ": ") if key.endswith("API_KEY") else input(key + ": ")
                if not value:
                    raise ValueError("缺少 {}；请补齐 {} 或环境变量后重跑".format(key, path))
                if "\n" in value or "\r" in value:
                    raise ValueError(key + " must be a single line")
                additions[key] = value
        if not old.strip():
            additions = dict(EVEROS_API_BASE=os.environ.get("EVEROS_API_BASE", "https://api.evermind.ai"),
                             **additions, EVEROS_OWNER_ALIASES=os.environ.get("EVEROS_OWNER_ALIASES", ""),
                             EVEROS_API_VERSION=os.environ.get("EVEROS_API_VERSION", "v2"),
                             EVEROS_READ_V1=os.environ.get("EVEROS_READ_V1", "0"))
        if any("\n" in value or "\r" in value for value in additions.values()):
            raise ValueError("EverOS config values must be single lines")
        text = old.rstrip() + ("\n" if old.strip() else "")
        text += "".join("{}={}\n".format(key, value) for key, value in additions.items())
        self.edit(path, text, 0o600)

    def mcp_json(self, path, create=False):
        path = Path(path)
        if not path.exists() and not create:
            return
        data = json.loads(path.read_text().strip() or "{}") if path.exists() else {}
        data.setdefault("mcpServers", {})["everos"] = {"command": "python3", "args": [str(self.mcp)], "env": {"EVEROS_HOME": str(self.everos)}}
        self.edit(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def rules(self, path):
        path = Path(path)
        content = (self.repo / "plugin/rules/everos-rules.md").read_text().strip()
        block = "<!-- everos-bridge:begin -->\n" + content + "\n<!-- everos-bridge:end -->"
        old = path.read_text() if path.exists() else ""
        pattern = re.compile(r"<!-- everos-bridge:begin -->.*?<!-- everos-bridge:end -->", re.S)
        text = pattern.sub(lambda _: block, old) if pattern.search(old) else (old.rstrip() + "\n\n" + block + "\n").lstrip()
        self.edit(path, text)

    @staticmethod
    def strip_jsonc(text):
        out, i, n = [], 0, len(text)
        while i < n:
            ch = text[i]
            if ch == '"':
                out.append(ch)
                i += 1
                while i < n:
                    out.append(text[i])
                    if text[i] == "\\" and i + 1 < n:
                        out.append(text[i + 1])
                        i += 2
                        continue
                    if text[i] == '"':
                        i += 1
                        break
                    i += 1
                continue
            if ch == "/" and i + 1 < n and text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
                continue
            if ch == "/" and i + 1 < n and text[i + 1] == "*":
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def mimocode(self):
        """MiMo Desktop / MiMoCode：MCP + L3/L4 插件 + 指令，写 ~/.config/mimocode/mimocode.jsonc。"""
        print("== MiMoCode / MiMo Desktop")
        config_dir = Path(self.environment.get("XDG_CONFIG_HOME") or (self.home / ".config")) / "mimocode"
        path = config_dir / "mimocode.jsonc"
        plugin = self.repo / "plugin/mimo/everos.mjs"
        plugin_url = "file://" + str(plugin).replace(" ", "%20")
        try:
            data = json.loads(self.strip_jsonc(path.read_text())) if path.exists() else {}
        except ValueError:
            raise ValueError(f"无法解析 {path}；请手工合并 EverOS 的 mcp/plugin/instructions")
        if not isinstance(data, dict):
            raise ValueError(f"{path} 顶层必须是对象")
        data.setdefault("mcp", {})
        if not isinstance(data["mcp"], dict):
            raise ValueError(f"{path} 的 mcp 必须是对象")
        previous = data["mcp"].get("everos", {})
        minimum = self.mcp_timeout_seconds() * 1000
        timeout = max(minimum, _positive_timeout(previous["timeout"], "MiMo everos timeout")
                      if isinstance(previous, dict) and "timeout" in previous else 0)
        data["mcp"]["everos"] = {
            "type": "local",
            "command": ["python3", str(self.mcp)],
            "environment": {"EVEROS_HOME": str(self.everos)},
            "timeout": timeout,
        }
        rules = str(self.repo / "plugin/rules/everos-rules.md")
        instructions = data.get("instructions")
        if not isinstance(instructions, list):
            instructions = []
        if rules not in instructions:
            instructions.append(rules)
        data["instructions"] = instructions
        plugins = data.get("plugin")
        if not isinstance(plugins, list):
            plugins = []
        entry = [plugin_url, {"hook": str(self.hook), "recallTimeoutMs": minimum}]
        plugins = [p for p in plugins
                   if not (isinstance(p, str) and "everos.mjs" in p)
                   and not (isinstance(p, list) and p and "everos.mjs" in str(p[0]))]
        plugins.append(entry)
        data["plugin"] = plugins
        if not self.dry:
            config_dir.mkdir(parents=True, exist_ok=True)
        self.edit(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print("  L3/L4 插件 " + str(plugin) + "；新会话生效。")

    def protocol_link(self):
        """~/.everos/PROTOCOL.md always points at the repo's PROTOCOL.md, wherever the clone lives."""
        link = self.everos / "PROTOCOL.md"
        target = self.repo / "PROTOCOL.md"
        if link.is_symlink():
            try:
                if link.resolve() == target:
                    return
            except OSError:
                pass
        if self.dry:
            print("  [dry-run] 把 {} 指向 {}".format(link, target))
            return
        self.everos.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            backup(link)
            link.unlink()
        link.symlink_to(target)
        print("  已把 {} 指向仓库副本（{}）".format(link, target))

    def install_bin(self, name):
        """Symlink a repo bin entry into ~/.local/bin; never overwrite a foreign file."""
        target = self.repo / "bin" / name
        link = self.home / ".local/bin" / name
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError("可执行入口不存在或不可执行：{}".format(target))
        if link.is_symlink():
            try:
                if link.resolve() == target:
                    return
            except (OSError, RuntimeError):
                pass  # Broken/cyclic links are conflicts, never replace them.
        if link.exists() or link.is_symlink():
            raise ValueError("路径已被其他文件占用，未覆盖：{}".format(link))
        if self.dry:
            print("  [dry-run] 把 {} 指向 {}".format(link, target))
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        print("  命令已安装：{}".format(link))
        if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            print("  使用短命令前，请把 {} 加入 PATH。".format(link.parent))

    def install_cli(self):
        """Install the manual search CLI; no credentials, host changes or API calls."""
        target = self.repo / "bin/everos"
        link = self.home / ".local/bin/everos"
        if not target.is_file() or not os.access(target, os.X_OK):
            raise ValueError("CLI 入口不存在或不可执行：{}".format(target))
        if link.is_symlink():
            try:
                if link.resolve() == target:
                    print("  CLI 已安装：{}".format(link))
                    return
            except (OSError, RuntimeError):
                pass  # Broken/cyclic links are conflicts, never replace them.
        if link.exists() or link.is_symlink():
            raise ValueError("CLI 路径已被其他文件占用，未覆盖：{}".format(link))
        if self.dry:
            print("  [dry-run] 把 {} 指向 {}".format(link, target))
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        print("  CLI 已安装：{}；终端可运行 everos search \"问题\"".format(link))
        if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            print("  使用短命令前，请把 {} 加入 PATH。".format(link.parent))

    def doubao(self):
        """Doubao Work: no hook/config surface exists, so only bins + user skill are wired."""
        print("== Doubao Work")
        self.install_bin("everos-mcp")
        self.install_bin("everos-doubao-ingest")
        skills_root = Path(os.environ.get(
            "DOUBAO_USER_SKILLS",
            str(self.home / ".doubao/agent_mode/workspace/.user_skills")))
        source_dir = self.repo / "plugin/skills/everos-memory-doubao"
        target_dir = skills_root / "everos-memory"
        if not skills_root.is_dir():
            print("  用户 skill 目录不存在，已跳过 skill 同步：{}".format(skills_root))
            print("  需要时创建该目录后重跑，或手动复制 plugin/skills/everos-memory-doubao 为 everos-memory。")
            return
        for entry in ("SKILL.md",):
            self.sync_doubao_skill_file(source_dir / entry, target_dir / entry, entry)
        self.sync_doubao_skill_file(source_dir.parent.parent / "doubao/check.sh",
                                    target_dir / "scripts/check.sh", "check.sh", executable=True)

    def sync_doubao_skill_file(self, source: Path, target: Path, label: str, executable: bool = False):
        """Copy one skill file (with backup), matching the daimon skill three-way policy."""
        if not source.is_file():
            raise ValueError("skill 源文件不存在：{}".format(source))
        text = source.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = target.read_text(encoding="utf-8") if target.exists() else None
        marker = self.everos / "state/install/doubao-everos-memory-{}.sha256".format(
            hashlib.sha256(label.encode()).hexdigest()[:12])
        last = marker.read_text().strip() if marker.exists() else None
        old_digest = hashlib.sha256(old.encode()).hexdigest() if old is not None else None
        if old_digest == digest:
            print("  Doubao skill {} 已是最新".format(label))
            return
        note_old = ("  [dry-run] 将安装 Doubao skill {}".format(label) if self.dry
                    else "  已安装 Doubao skill {}".format(label))
        note_chg = ("  [dry-run] 将更新 Doubao skill {}".format(label) if self.dry
                    else "  Doubao skill {} 有更新，已覆盖".format(label))
        note_foreign = ("  [dry-run] Doubao skill {} 被外部改过，将备份并覆盖".format(label) if self.dry
                        else "  Doubao skill {} 被外部改过，已备份并覆盖".format(label))
        if old is None:
            print(note_old + "：{}".format(target))
        elif last == old_digest:
            print(note_chg + "：{}".format(target))
        else:
            print(note_foreign + "：{}".format(target))
        if self.dry:
            return
        self.edit(target, text, mode=0o755 if executable else None)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(digest)

    def sync_daimon_skill(self, desktop):
        """Keep the daimon-managed everos-memory skill identical to the repo version.

        Three-way check against the hash recorded at the last sync: identical is a
        no-op, repo upgrade is a fast-forward, anything else means the daimon copy
        was edited out-of-band and is overwritten loudly (with backup).
        """
        skills_root = desktop.parents[2] / "skills"
        if not skills_root.is_dir():
            return
        source = self.repo / "plugin/skills/everos-memory/SKILL.md"
        target = skills_root / "everos-memory/SKILL.md"
        marker = self.everos / "state/install/everos-memory-daimon.sha256"
        text = source.read_text()
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = target.read_text() if target.exists() else None
        old_digest = hashlib.sha256(old.encode()).hexdigest() if old is not None else None
        last = marker.read_text().strip() if marker.exists() else None
        if old_digest == digest:
            print("  daimon skill everos-memory 已是最新")
        elif old is None:
            self.edit(target, text)
            if not self.dry:
                print("  已安装 everos-memory skill 到 " + str(target))
        elif last == old_digest:
            self.edit(target, text)
            if not self.dry:
                print("  仓库版 skill 有更新，已同步到 " + str(target))
        else:
            note = "daimon 副本与仓库版不一致且不是上次同步的内容（被手改过或为历史旧版）"
            print(("  [dry-run] 注意：" + note + "，将备份并覆盖：" if self.dry
                   else "  注意：" + note + "，已备份并覆盖：") + str(target))
            self.edit(target, text)
        if not self.dry:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(digest + "\n")

    def marketplace(self, cli):
        if self.dry:
            print("  [dry-run] 检查并迁移 {} 的 everos-bridge 市场到 {}".format(cli, self.repo))
            return
        listing = json.loads(self.run([cli, "plugin", "marketplace", "list", "--json"], capture=True))
        entries = listing["marketplaces"] if cli == "codex" else listing
        existing = next((entry for entry in entries if entry["name"] == "everos-bridge"), None)
        source = (existing or {}).get("marketplaceSource", {})
        current = source.get("source") if cli == "codex" else (existing or {}).get("path")
        local = source.get("sourceType") == "local" if cli == "codex" else (existing or {}).get("source") == "directory"
        if existing and local and current and Path(current).resolve() == self.repo:
            return
        if existing:
            self.run([cli, "plugin", "marketplace", "remove", "everos-bridge"])
        self.run([cli, "plugin", "marketplace", "add", self.repo])
        if cli == "codex":
            listing = json.loads(self.run([cli, "plugin", "marketplace", "list", "--json"], capture=True))
            source = next((entry.get("marketplaceSource", {}) for entry in listing["marketplaces"]
                           if entry["name"] == "everos-bridge"), {})
            if source.get("sourceType") != "local" or Path(source.get("source", "")).resolve() != self.repo:
                raise ValueError("Codex everos-bridge marketplace migration did not take effect")

    def mcp_cli(self, cli):
        remove = False
        if self.dry:
            print("  [dry-run] 检查并更新 {} 的 everos MCP 旧路径".format(cli))
        else:
            current = subprocess.run([cli, "mcp", "get", "everos"], env=self.environment,
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if current.returncode == 0:
                remove = True
            elif current.returncode != 1 or "No MCP server named 'everos' found" not in current.stderr:
                if current.stderr:
                    print(current.stderr, end="", file=sys.stderr)
                raise subprocess.CalledProcessError(current.returncode, current.args,
                                                    current.stdout, current.stderr)
        help_text = self.run([cli, "mcp", "add", "--help"], capture=True)
        command = [cli, "mcp", "add", "everos"]
        if re.search(r"(?<![\w-])--env(?:[\s=,]|$)", help_text) or self.dry:
            command += ["--env", "EVEROS_HOME=" + str(self.everos)]
        elif self.everos != self.home / ".everos":
            raise ValueError(f"{cli} 不支持显式MCP环境变量，无法绑定自定义EVEROS_HOME")
        if remove:
            self.run([cli, "mcp", "remove", "everos"])
        self.run(command + ["--", "python3", self.mcp])

    def install(self):
        self.mcp_timeout_seconds()
        self.config()
        self.protocol_link()
        self.install_cli()
        claude_home = Path(self.environment.get("CLAUDE_CONFIG_DIR", str(self.home / ".claude")))
        if self.only is None and shutil.which("claude"):
            print("== Claude Code")
            settings = claude_home / "settings.json"
            self.before_cli(settings, claude_home / "plugins/known_marketplaces.json", claude_home / "plugins/installed_plugins.json")
            self.marketplace("claude")
            self.run(["claude", "plugin", "install", "everos@everos-bridge", "--scope", "user"])
            # Reinstalling an existing local plugin does not refresh its
            # registered version/cache. Explicitly refresh after install.
            self.run(["claude", "plugin", "update", "everos@everos-bridge", "--scope", "user"])
            if settings.exists():
                data = json.loads(settings.read_text())
                for event, groups in data.get("hooks", {}).items():
                    retained = []
                    for group in groups:
                        hooks = [hook for hook in group.get("hooks", []) if not is_everos_hook(hook.get("command", ""))]
                        if hooks:
                            retained.append(dict(group, hooks=hooks))
                        elif not group.get("hooks"):
                            retained.append(group)
                    data["hooks"][event] = retained
                self.edit(settings, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        if self.only is None and shutil.which("codex"):
            print("== Codex")
            config = Path(self.environment.get("CODEX_HOME", str(self.home / ".codex"))) / "config.toml"
            timeout = max(self.mcp_timeout_seconds(), codex_tool_timeout(config.read_text() if config.exists() else ""))
            self.before_cli(config)
            if not self.dry:
                config.parent.mkdir(parents=True, exist_ok=True)
            with self.preserve_codex_cache():
                self.marketplace("codex")
                self.run(["codex", "plugin", "add", "everos@everos-bridge"])
            self.mcp_cli("codex")
            self.edit(config, set_codex_tool_timeout(
                enable_codex_hooks(config.read_text() if config.exists() else ""), timeout))
            print("  新或更新的 hook 定义需在新会话中按 Codex 提示审阅并信任，之后自动读写才生效。")
        for cli in ("traecli", "traex", "coco"):
            if shutil.which(cli):
                print("== " + cli)
                shared = Path(self.environment.get("TRAE_HOME", str(self.home / ".trae"))).expanduser()
                runtime = Path(self.environment.get("TRAECLI_HOME", str(shared / "cli"))).expanduser()
                self.before_cli(shared / "traecli.toml")
                self.mcp_cli(cli)
                self.rules(shared / "AGENTS.md")
                # Old Coco has different hook/storage contracts. Do not silently
                # claim L3/L4 support there; keep its existing MCP + rules path.
                version = self.run([cli, "--version"], capture=True)
                match = re.search(r"(?:traecli|traex)\s+(\d+)\.(\d+)\.(\d+)", version, re.I)
                if self.dry or (match and tuple(map(int, match.groups())) >= (0, 209, 1)):
                    path = runtime / "hooks.json"
                    self.edit(path, trae_hooks(path.read_text() if path.exists() else "", self.hook, runtime, self.mcp_timeout_seconds(),
                                               tool_events=bool(match and tuple(map(int, match.groups())) >= (0, 210, 1))))
                    print("  TraeX L3/L4 已配置；新会话在 /hooks 审阅并信任 EverOS 当前定义后生效。")
                else:
                    print("  此宿主仅配置 MCP + 规则；L3/L4 需要 TraeX >= 0.209.1。")
                break
        else:
            if self.only == "trae":
                raise ValueError("未找到 traecli、traex 或 coco")
        if self.only == "trae":
            self.finish()
            return
        self.doubao()
        print("== 其他 MCP 客户端")
        process = subprocess.run(["ps", "-axo", "command"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if "Claude.app/Contents/MacOS/Claude" in process.stdout:
            print("  Claude Desktop 正在运行，本次跳过其配置；退出 App 后重跑，或在设置中手动添加 MCP。")
        else:
            desktop_root = self.home / "Library/Application Support/Claude"
            desktop_present = ((self.home / "Applications/Claude.app").is_dir() or
                               (self.home == Path.home() and Path("/Applications/Claude.app").is_dir()))
            self.mcp_json(desktop_root / "claude_desktop_config.json",
                          create=desktop_present or desktop_root.is_dir())
            linux_desktop = self.home / ".config/Claude"
            self.mcp_json(linux_desktop / "claude_desktop_config.json", create=linux_desktop.is_dir())
        cursor_home = self.home / ".cursor"
        cursor_installed = (cursor_home.is_dir() or shutil.which("cursor") or
                            (self.home / "Applications/Cursor.app").is_dir() or
                            (self.home == Path.home() and Path("/Applications/Cursor.app").is_dir()))
        self.mcp_json(cursor_home / "mcp.json", create=bool(cursor_installed))
        if cursor_installed:
            self.edit(cursor_home / "skills/everos-memory/SKILL.md",
                      (self.repo / "plugin/skills/everos-memory/SKILL.md").read_text())
        self.mcp_json(self.home / ".gemini/settings.json", create=bool(shutil.which("gemini")))
        if shutil.which("gemini"):
            self.rules(self.home / ".gemini/GEMINI.md")
        kimi_home = Path(self.environment.get("KIMI_CODE_HOME", str(self.home / ".kimi-code")))
        kimi_cli = shutil.which("kimi")
        if not kimi_cli:
            # kimi 官方安装器把 CLI 放在 ~/.kimi-code/bin，不一定加过 PATH；
            # 只按 PATH 探测会把已安装的宿主静默漏掉。
            fallback = kimi_home / "bin" / "kimi"
            if fallback.is_file() and os.access(fallback, os.X_OK):
                kimi_cli = str(fallback)
        if kimi_cli:
            print("== Kimi Code")
            self.mcp_json(kimi_home / "mcp.json", create=True)
            config = kimi_home / "config.toml"
            self.edit(config, kimi_hooks(config.read_text() if config.exists() else "", self.hook, recall_timeout=self.mcp_timeout_seconds()))
            self.run([kimi_cli, "doctor"])
        elif kimi_home.is_dir():
            print("== Kimi Code（跳过：{} 已存在但找不到可执行的 kimi 命令；".format(kimi_home))
            print("  请修复安装或把 {} 加入 PATH 后重跑 install.sh）".format(kimi_home / "bin"))
        default_runtime = self.home / "Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code"
        desktop = Path(os.environ.get("EVEROS_KIMI_DESKTOP_CONFIG", str(default_runtime / "config.toml")))
        if desktop.exists():
            print("== Kimi Work 桌面端")
            desktop_home = Path(os.environ.get("EVEROS_KIMI_DESKTOP_HOME", str(desktop.parent / "home")))
            self.mcp_json(desktop_home / "mcp.json", create=True)
            self.edit(desktop, kimi_hooks(desktop.read_text(), self.hook, desktop=True, recall_timeout=self.mcp_timeout_seconds()))
            self.sync_daimon_skill(desktop)
        mimo_cfg = Path(self.environment.get("XDG_CONFIG_HOME") or (self.home / ".config")) / "mimocode" / "mimocode.jsonc"
        mimo_home = self.home / "Library/Application Support/Xiaomi MiMo"
        if shutil.which("mimo") or mimo_cfg.exists() or mimo_home.exists():
            self.mimocode()
        self.finish()

    def finish(self):
        print("== 自检")
        self.run([sys.executable, self.hook, "--doctor"])
        if self.dry:
            print("预览完成；未写配置、安装插件或执行 doctor。")
        else:
            print("接线与自检完成。请在新会话验证；新或更新的 hook 需按宿主提示审阅并信任。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--cli-only", action="store_true", help="只安装手动检索命令，不接线宿主或调用 API")
    scope.add_argument("--only", choices=["trae"], help="只刷新 TraeX，不改其他宿主")
    args = parser.parse_args()
    if sys.version_info < (3, 9):
        parser.error("需要 python3 >= 3.9")
    try:
        installer = Installer(Path(__file__).resolve().parents[1], args.dry_run, args.only)
        if args.cli_only:
            installer.install_cli()
            # Doubao Work 的两个命令不依赖凭据或宿主配置，--cli-only 也一并装上
            installer.install_bin("everos-mcp")
            installer.install_bin("everos-doubao-ingest")
        else:
            installer.install()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("安装未完成：{}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
