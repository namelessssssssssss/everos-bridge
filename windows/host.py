"""Windows Codex host orchestration; never imports or edits the Unix installer."""
from contextlib import contextmanager
import getpass
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
from config_helpers import backup, write_text, env_values
import win_rules as rulebook


class WindowsHost:
    def __init__(self, repo, dry=False):
        self.repo, self.dry = Path(repo).resolve(), dry
        # absolute() keeps lexical reparse-point components for private_home's
        # validation; resolve() here could hide a junction before it is checked.
        self.home = Path(os.environ.get("EVEROS_INSTALL_HOME", str(Path.home()))).expanduser().absolute()
        self.everos = Path(os.environ.get("EVEROS_HOME", str(self.home / ".everos"))).expanduser().absolute()
        self.environment = dict(os.environ, EVEROS_HOME=str(self.everos), PYTHONUTF8="1")
        if "EVEROS_INSTALL_HOME" in os.environ:
            self.environment.setdefault("CODEX_HOME", str(self.home / ".codex"))
        if self.environment.get("CODEX_HOME"):
            self.environment["CODEX_HOME"] = str(Path(self.environment["CODEX_HOME"]).expanduser().absolute())
        self.hook = self.repo / "plugin/everos/hook.py"

    def mcp_timeout_seconds(self):
        path = self.everos / "config.env"
        values = env_values(path.read_text(encoding="utf-8")) if path.exists() else {}
        value = self.environment.get("EVEROS_AGENTIC_TIMEOUT") or values.get("EVEROS_AGENTIC_TIMEOUT", "180")
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            raise ValueError("EVEROS_AGENTIC_TIMEOUT must be positive and finite") from None
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("EVEROS_AGENTIC_TIMEOUT must be positive and finite")
        return math.ceil(seconds + 10)

    def sync_rules(self):
        from platform_runtime import windows_rules
        documents = rulebook.bundle(self.repo / "plugin")
        documents = {name: windows_rules(text) for name, text in documents.items()}
        state = self.everos / "state/rules"
        previous = rulebook.load(state / "current.json")
        codex = Path(self.environment.get("CODEX_HOME", str(self.home / ".codex")))
        override = codex / "AGENTS.override.md"
        path = override if override.exists() and override.read_text(encoding="utf-8").strip() else codex / "AGENTS.md"
        if path.is_symlink():
            raise ValueError("Resolve the native instruction symlink before installing: " + str(path))
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        text = rulebook.managed_text(old, documents)
        self.edit(path, text)
        if not self.dry and path.read_text(encoding="utf-8") != text:
            raise ValueError("Native rule readback mismatch: " + str(path))
        current = rulebook.transition(documents, previous)
        self.edit(state / "current.json", json.dumps(current, ensure_ascii=False, indent=2) + "\n", 0o600)
        targets = {"revision": current["revision"], "scope": ["codex"], "targets": [{"host": "codex", "path": str(path), "status": "preview" if self.dry else "file_verified", "changed": old != text}]}
        self.edit(state / "targets.json", json.dumps(targets, ensure_ascii=False, indent=2) + "\n", 0o600)
        print("  Windows native rules " + current["revision"][:12] + "; read back " + str(path))

    def finish(self):
        self.sync_rules()
        self.run([sys.executable, "-X", "utf8", self.hook, "--doctor"])
        print("Preview only; no writes or API calls." if self.dry else "Installed. Review/trust hooks and validate in a new Codex chat.")

    def run(self, args, capture=False):
        if self.dry:
            print("  [dry-run] " + shlex.join([str(arg) for arg in args]))
            return ""
        command = [str(arg) for arg in args]
        environment = self.environment
        if command[0] == "codex":
            command[0] = self.codex_path()
            if Path(command[0]).suffix.lower() in (".cmd", ".bat"):
                # cmd expands these variables once. Data stays inside quotes;
                # percent signs are not recursively expanded and delayed !
                # expansion is disabled. Installer arguments cannot contain
                # quotes/newlines (neither can Windows paths).
                environment = dict(self.environment)
                for index, argument in enumerate(command):
                    if any(char in argument for char in ('"', '\r', '\n', '\0')):
                        raise ValueError("Unsupported quote/newline in Codex batch argument")
                    environment["EVEROS_CLI_ARG_" + str(index)] = argument
                quoted = ' '.join('"%EVEROS_CLI_ARG_{}%"'.format(i) for i in range(len(command)))
                shell = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
                command = '"{}" /d /v:off /s /c "{}"'.format(shell, quoted)
        result = subprocess.run(command, env=environment, text=True,
                                stdout=subprocess.PIPE if capture else None, check=True)
        return result.stdout if capture else ""

    def codex_path(self):
        path = shutil.which("codex", path=self.environment.get("PATH"))
        if not path:
            raise ValueError("Codex CLI is required on PATH")
        return str(Path(path).absolute())

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
        old = path.read_text(encoding="utf-8") if path.exists() else ""
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

    def marketplace(self, cli):
        if self.dry:
            print("  [dry-run] 检查并迁移 {} 的 everos-bridge 市场到 {}".format(cli, self.repo))
            return
        listing = json.loads(self.run([cli, "plugin", "marketplace", "list", "--json"], capture=True))
        entries = listing["marketplaces"] if cli == "codex" else listing
        existing = next((entry for entry in entries if entry["name"] == "everos-bridge"), None)
        source = (existing or {}).get("marketplaceSource", {})
        current = (source.get("source") or (existing or {}).get("root")) if cli == "codex" else (existing or {}).get("path")
        local = (source.get("sourceType") == "local" or bool((existing or {}).get("root"))) if cli == "codex" else (existing or {}).get("source") == "directory"
        if existing and local and current and Path(current).resolve() == self.repo:
            return
        if existing:
            self.run([cli, "plugin", "marketplace", "remove", "everos-bridge"])
        self.run([cli, "plugin", "marketplace", "add", self.repo])
        if cli == "codex":
            listing = json.loads(self.run([cli, "plugin", "marketplace", "list", "--json"], capture=True))
            entry = next((entry for entry in listing["marketplaces"] if entry["name"] == "everos-bridge"), {})
            source = entry.get("marketplaceSource", {})
            current = source.get("source") or entry.get("root", "")
            if not current or Path(current).resolve() != self.repo:
                raise ValueError("Codex everos-bridge marketplace migration did not take effect")
