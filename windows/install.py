"""Native Windows Codex/Kimi installer. No Bash, symlinks, admin rights or pip needed."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

from config_helpers import (backup, codex_tool_timeout, enable_codex_hooks, merge_kimi_hooks,
                            merge_kimi_mcp, set_codex_tool_timeout)
from cli_launcher import launcher_scripts, ps_quote
from host import WindowsHost
from platform_runtime import adapt, replace


def hook_command(python, hook, home, host_name=None, environment=None, host=None, kimi_home=None):
    # EncodedCommand avoids nested cmd/PowerShell quoting and preserves Unicode,
    # spaces, apostrophes and shell metacharacters without executing their text.
    if host_name and host and host_name != host:
        raise ValueError("Conflicting EVEROS_HOST values")
    bindings = dict(environment or {})
    bindings["PYTHONUTF8"] = "1"
    selected_host = host_name or host
    if selected_host:
        bindings["EVEROS_HOST"] = selected_host
    if kimi_home:
        bindings["KIMI_CODE_HOME"] = str(kimi_home)
    bindings["EVEROS_HOME"] = str(home)
    script = "".join("$env:{}={}; ".format(name, ps_quote(value)) for name, value in bindings.items())
    script += "& {} -X utf8 {}; exit $LASTEXITCODE".format(ps_quote(python), ps_quote(hook))
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded


def private_acl(path, directory=False):
    """Replace the DACL, including pre-existing explicit grants, not just inheritance."""
    kind = "DirectorySecurity" if directory else "FileSecurity"
    inheritance = "OICI" if directory else ""
    script = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
              "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
              "$acl=New-Object System.Security.AccessControl." + kind + "; "
              "$acl.SetSecurityDescriptorSddlForm('D:P(A;" + inheritance + ";FA;;;'+$sid+')(A;" +
              inheritance + ";FA;;;SY)'); [System.IO." + ("Directory" if directory else "File") +
              "]::SetAccessControl(" + ps_quote(path) + ", $acl)")
    command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
               base64.b64encode(script.encode("utf-16le")).decode("ascii")]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


class WindowsInstaller(WindowsHost):
    def __init__(self, repo, dry=False, only=None):
        super().__init__(repo, dry, only)
        self.python = Path(sys.executable).resolve()
        self.environment["PYTHONUTF8"] = "1"

    def private_home(self):
        root = self.everos.absolute()
        for path in (root, *root.parents):
            if path.exists() and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("EVEROS_HOME must not traverse a Windows reparse point: " + str(path))
        if root.resolve() == self.repo or self.repo in root.resolve().parents:
            raise ValueError("EVEROS_HOME must be outside the source checkout")
        config = root / "config.env"
        if config.exists() and config.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("config.env must not be a Windows reparse point")
        if self.dry:
            print("  [dry-run] 限制 EverOS 目录 ACL 为当前用户和 SYSTEM：" + str(root))
            return
        root.mkdir(parents=True, exist_ok=True)
        private_acl(root, directory=True)
        if config.is_file():
            private_acl(config)

    def protocol_link(self):
        # Copying works without Windows Developer Mode; source remains canonical.
        path = self.everos / "PROTOCOL.md"
        if path.is_symlink() and not self.dry:
            backup(path)
            path.unlink()
        self.edit(path, (self.repo / "PROTOCOL.md").read_text(encoding="utf-8"))

    def install_cli(self):
        target = self.repo / "plugin/everos/cli.py"
        # These managed launchers can be refreshed, but unrelated commands survive.
        scripts = launcher_scripts(self.python, target)
        for name, text in scripts.items():
            path = self.home / ".local/bin" / name
            lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
            if path.is_symlink() or (path.exists() and (not lines or "everos-bridge managed launcher" not in lines[0])):
                raise ValueError("CLI 路径已被其他文件占用，未覆盖：" + str(path))
        for name, text in scripts.items():
            self.edit(self.home / ".local/bin" / name, text)
        print("  手动检索入口：" + str(self.home / ".local/bin/everos.cmd"))

    def snapshot(self, validate_only=False):
        """Immutable runtime/marketplace: old chats retain exact hook definitions."""
        files = sorted([path for path in (self.repo / "plugin").rglob("*")
                        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"] +
                       [self.repo / "PROTOCOL.md", self.repo / ".agents/plugins/marketplace.json"])
        digest = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob("*.py")):
            digest.update(source.name.encode() + b"\0" + source.read_bytes())
        for path in files:
            if path.is_symlink():
                raise ValueError("Plugin sources must be ordinary files: " + str(path))
            digest.update(path.relative_to(self.repo).as_posix().encode("utf-8") + b"\0" + path.read_bytes())
        budget = self.mcp_timeout_seconds()
        digest.update(json.dumps([str(self.python), str(self.everos), budget]).encode("utf-8"))
        target = self.everos / "runtime" / ("windows-" + digest.hexdigest()[:20])
        if self.dry and not validate_only:
            print("  [dry-run] 生成原生 Windows 插件快照：" + str(target))
            return target
        contents = {path.relative_to(self.repo): path.read_bytes() for path in files}
        contents = adapt(contents, (Path(__file__).parent / "windows_lock.py").read_bytes())
        hooks_path = Path("plugin/hooks/hooks.json")
        hooks = json.loads(contents[hooks_path])
        command = hook_command(self.python, target / "plugin/everos/hook.py", self.everos)
        for event, groups in hooks["hooks"].items():
            for group in groups:
                for handler in group.get("hooks", []):
                    handler["commandWindows"] = command
                    if event == "UserPromptSubmit":
                        handler["timeout"] = max(handler.get("timeout", 0), budget)
        contents[hooks_path] = (json.dumps(hooks, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        manifest_path = Path("plugin/.codex-plugin/plugin.json")
        manifest = json.loads(contents[manifest_path])
        source_version = manifest["version"]
        manifest["version"] = source_version.split("-")[0] + "-windows." + digest.hexdigest()[:12]
        contents[manifest_path] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        claude_path = Path("plugin/.claude-plugin/plugin.json")
        claude = json.loads(contents[claude_path])
        claude["version"] = manifest["version"]
        contents[claude_path] = (json.dumps(claude, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        mcp_path = Path("plugin/everos/mcp_server.py")
        contents[mcp_path] = replace(contents[mcp_path].decode("utf-8"),
                                     "VERSION = " + json.dumps(source_version),
                                     "VERSION = " + json.dumps(manifest["version"])).encode("utf-8")
        if target.exists():
            for relative, data in contents.items():
                path = target / relative
                if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                    raise ValueError("Existing runtime snapshot differs; preserve it for old chats: " + str(path))
            return target
        if validate_only:
            return target
        # Fully prepare before atomically making the snapshot visible.
        import tempfile
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".windows-install-", dir=target.parent) as temporary:
            stage = Path(temporary) / "snapshot"
            for relative, data in contents.items():
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            os.replace(stage, target)
        return target

    def install(self):
        if self.only == "kimi":
            self.install_kimi()
        else:
            self.install_codex()

    def install_kimi(self):
        if not self.kimi_detected():
            raise ValueError("Kimi Code/Desktop was not found; set KIMI_CODE_HOME or create ~/.kimi-code")
        timeout = self.mcp_timeout_seconds()
        kimi = self.kimi_home()
        mcp = kimi / "mcp.json"
        previous = mcp.read_text(encoding="utf-8") if mcp.exists() else ""
        merge_kimi_mcp(previous, self.python, self.repo / "plugin/everos/mcp_server.py",
                       self.everos, kimi, timeout * 1000)
        config = kimi / "config.toml"
        old = config.read_text(encoding="utf-8") if config.exists() else ""
        validation_command = hook_command(self.python, self.repo / "plugin/everos/hook.py", self.everos,
                                          host="kimi", kimi_home=kimi)
        merge_kimi_hooks(old, validation_command, timeout)
        self.private_home()
        self.config()
        self.protocol_link()
        runtime = self.snapshot()
        WindowsInstaller(runtime, self.dry, only="kimi").install_cli()
        server = runtime / "plugin/everos/mcp_server.py"
        self.edit(mcp, merge_kimi_mcp(previous, self.python, server, self.everos, kimi, timeout * 1000))
        command = hook_command(self.python, runtime / "plugin/everos/hook.py", self.everos,
                               host="kimi", kimi_home=kimi)
        self.edit(config, merge_kimi_hooks(old, command, timeout))
        self.hook = runtime / "plugin/everos/hook.py"
        self.finish()

    def install_codex(self):
        self.codex_path()  # Resolve .exe/.cmd before writing any configuration.
        self.mcp_timeout_seconds()  # Validate before writing credentials/config.
        self.private_home()
        self.config()
        self.protocol_link()
        runtime = self.snapshot()
        WindowsInstaller(runtime, self.dry).install_cli()
        codex = Path(self.environment.get("CODEX_HOME", str(self.home / ".codex")))
        config = codex / "config.toml"
        previous = config.read_text(encoding="utf-8") if config.exists() else ""
        timeout = max(self.mcp_timeout_seconds(), codex_tool_timeout(previous))
        self.before_cli(config)
        if not self.dry:
            codex.mkdir(parents=True, exist_ok=True)
        marketplace = WindowsHost(runtime, self.dry)
        marketplace.environment = self.environment
        with self.preserve_codex_cache():
            marketplace.marketplace("codex")
            self.run(["codex", "plugin", "add", "everos@everos-bridge"])
        self.run(["codex", "mcp", "add", "everos", "--env", "EVEROS_HOME=" + str(self.everos),
                  "--env", "PYTHONUTF8=1", "--", self.python, "-X", "utf8", runtime / "plugin/everos/mcp_server.py"])
        current = config.read_text(encoding="utf-8") if config.exists() else previous
        self.edit(config, set_codex_tool_timeout(enable_codex_hooks(current), timeout))
        self.hook = runtime / "plugin/everos/hook.py"
        self.finish()
        self.record_source(runtime)

    def record_source(self, runtime=None):
        from provenance import record
        record(self, runtime)

    def kimi_host(self, runtime=None):
        from kimi_desktop import KimiDesktopHost
        host = KimiDesktopHost(runtime or self.repo, self.dry)
        host.environment = self.environment
        host.python = self.python
        return host

    def sync_kimi_desktop_rules(self):
        self.kimi_host().sync_rules()

    def install_kimi_desktop(self):
        self.mcp_timeout_seconds()
        desktop = self.kimi_host()
        if not desktop.desktop_config().is_file():
            raise ValueError("未找到 Kimi Work 桌面端 config.toml；请先启动桌面端或设置 EVEROS_KIMI_DESKTOP_CONFIG")
        desktop_home = desktop.desktop_home()
        self.private_home()
        self.config()
        self.protocol_link()
        runtime = self.snapshot()
        WindowsInstaller(runtime, self.dry).install_cli()
        command = hook_command(self.python, runtime / "plugin/everos/hook.py", self.everos,
                               host_name="kimi_desktop",
                               environment={"EVEROS_KIMI_DESKTOP_HOME": desktop_home})
        host = self.kimi_host(self.repo if self.dry else runtime)
        host.runtime = runtime
        host.install(command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--kimi-desktop", action="store_true")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--cli-only", action="store_true")
    scope.add_argument("--rules-only", action="store_true")
    parser.add_argument("--only", choices=["codex", "kimi"], help="只接线指定宿主；也可限定 --rules-only")
    args = parser.parse_args()
    if args.kimi_desktop and args.only:
        parser.error("--kimi-desktop cannot be combined with --only; use --only kimi for ~/.kimi-code")
    if os.name != "nt" or sys.version_info < (3, 9):
        parser.error("Native Windows and Python >= 3.9 are required; use install.sh on Unix")
    try:
        installer = WindowsInstaller(Path(__file__).resolve().parents[1], args.dry_run, args.only)
        if args.cli_only:
            runtime = installer.snapshot()
            WindowsInstaller(runtime, args.dry_run).install_cli()
        elif args.rules_only:
            if args.kimi_desktop:
                installer.sync_kimi_desktop_rules()
            else:
                installer.sync_rules()
                if args.only != "kimi":
                    installer.record_source()
        elif args.kimi_desktop:
            installer.install_kimi_desktop()
        else:
            installer.install()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("安装未完成：" + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
