"""Native Windows Kimi Work setup contracts with isolated files and no real API."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import tomllib
except ImportError:
    tomllib = None

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "windows"))
from host import WindowsHost
from install import WindowsInstaller, hook_command
from kimi_desktop import KimiDesktopHost, is_everos_hook, kimi_hooks


class KimiDesktopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="everos Kimi Windows ")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home 空间 ' & $"
        self.desktop = self.home / "runtime/kimi-code/config.toml"
        self.desktop.parent.mkdir(parents=True)
        old = "PYTHONUTF8=1 EVEROS_HOST=kimi_desktop python3 'C:\\old\\plugin\\everos\\hook.py'"
        self.desktop.write_text(
            'model = "keep"\n\n[[hooks]]\nevent = "Stop"\ncommand = ' + json.dumps(old) + '\ntimeout = 10\n'
            '\n[[hooks]]\nevent = "Stop"\ncommand = "echo keep"\ntimeout = 1\n', encoding="utf-8")
        (self.home / "skills").mkdir()
        self.patch = patch.dict(os.environ, {
            "EVEROS_INSTALL_HOME": str(self.home),
            "EVEROS_HOME": str(self.home / ".everos"),
            "EVEROS_KIMI_DESKTOP_CONFIG": str(self.desktop),
            "EVEROS_KIMI_DESKTOP_HOME": str(self.desktop.parent / "home"),
            "EVEROS_AGENTIC_TIMEOUT": "240",
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def command(self):
        return hook_command(sys.executable, REPO / "plugin/everos/hook.py", self.home / ".everos",
                            host_name="kimi_desktop")

    def test_install_migrates_old_hooks_and_is_idempotent(self):
        host = KimiDesktopHost(REPO)
        command = self.command()
        with patch.object(host, "run") as doctor:
            host.install(command)
        text = self.desktop.read_text(encoding="utf-8")
        self.assertIn('command = "echo keep"', text)
        self.assertNotIn("C:\\\\old", text)
        if tomllib:
            hooks = tomllib.loads(text)["hooks"]
            managed = [hook for hook in hooks if hook["command"] == command]
            self.assertEqual(len(managed), 4)
            self.assertEqual({hook["event"] for hook in managed},
                             {"UserPromptSubmit", "Stop", "PreCompact", "SessionEnd"})
            prompt = next(hook for hook in managed if hook["event"] == "UserPromptSubmit")
            self.assertEqual(prompt["timeout"], 250)
        decoded = base64.b64decode(command.split()[-1]).decode("utf-16le")
        self.assertIn("EVEROS_HOST", decoded)
        self.assertIn("kimi_desktop", decoded)
        self.assertIn("-X utf8", decoded)
        mcp_path = self.desktop.parent / "home/mcp.json"
        mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        everos = mcp["mcpServers"]["everos"]
        self.assertEqual(everos["command"], str(host.python))
        self.assertEqual(everos["args"][:2], ["-X", "utf8"])
        self.assertEqual(everos["env"]["PYTHONUTF8"], "1")
        self.assertNotIn("EVEROS_API_KEY", everos["env"])
        agents = self.desktop.parent / "home/AGENTS.md"
        self.assertEqual(agents.read_text(encoding="utf-8").count("<!-- everos-bridge:begin -->"), 1)
        self.assertIn("install.ps1", agents.read_text(encoding="utf-8"))
        skill = self.home / "skills/everos-memory/SKILL.md"
        self.assertEqual(skill.read_text(encoding="utf-8"),
                         (REPO / "plugin/skills/everos-memory/SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(doctor.call_count, 1)

        config_before = self.desktop.read_bytes()
        mcp_before = mcp_path.read_bytes()
        agents_before = agents.read_bytes()
        with patch.object(host, "run"):
            host.install(command)
        self.assertEqual(self.desktop.read_bytes(), config_before)
        self.assertEqual(mcp_path.read_bytes(), mcp_before)
        self.assertEqual(agents.read_bytes(), agents_before)

    def test_encoded_hook_is_detected_and_never_duplicated(self):
        command = self.command()
        self.assertTrue(is_everos_hook(command))
        first = kimi_hooks("", command, 250)
        self.assertEqual(kimi_hooks(first, command, 250), first)
        self.assertEqual(first.count("[[hooks]]"), 4)
        self.assertIn("timeout = 250", first)

    def test_inline_toml_comment_does_not_duplicate_encoded_hook(self):
        command = self.command()
        first = kimi_hooks("", command, 250)
        lines = first.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("command = "):
                lines[index] = line + " # native EverOS handler"
        commented = "\n".join(lines) + "\n"
        self.assertEqual(kimi_hooks(commented, command, 250), first)
        self.assertEqual(kimi_hooks(commented, command, 250).count("[[hooks]]"), 4)

    @unittest.skipUnless(os.name == "nt", "native encoded hook and background worker")
    def test_stop_with_only_session_id_uses_bound_desktop_home(self):
        import time
        sys.path.insert(0, str(REPO / "tests"))
        from test_cli import FakeEverOS

        installer = WindowsInstaller(REPO)
        with patch.object(WindowsInstaller, "private_home"), \
                patch.object(WindowsInstaller, "config"), \
                patch.object(WindowsInstaller, "protocol_link"), \
                patch.object(WindowsInstaller, "install_cli"), \
                patch.object(KimiDesktopHost, "run"):
            installer.install_kimi_desktop()
        text = self.desktop.read_text(encoding="utf-8")
        raw = next(line.split("=", 1)[1].strip() for line in text.splitlines()
                   if line.startswith("command = ") and "powershell.exe" in line)
        command = json.JSONDecoder().raw_decode(raw)[0]
        decoded = base64.b64decode(command.split()[-1]).decode("utf-16le")
        desktop_home = self.desktop.parent / "home"
        self.assertIn("EVEROS_KIMI_DESKTOP_HOME", decoded)
        self.assertIn(str(desktop_home).replace("'", "''"), decoded)

        session = "conv-stop-only"
        wire = desktop_home / "sessions/wd_fixture" / session / "agents/main/wire.jsonl"
        wire.parent.mkdir(parents=True)
        rows = [
            {"type": "context.append_message", "time": 1, "message": {
                "role": "user", "origin": {"kind": "user"},
                "content": [{"type": "text", "text": "<meta awareness=\"low\" />Stop fixture user"}]}},
            {"type": "context.append_loop_event", "time": 2, "event": {
                "type": "content.part", "stepUuid": "step-1",
                "part": {"type": "text", "text": "Stop fixture assistant"}}},
        ]
        wire.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

        def respond(request):
            return 200, {"data": {"status": "accumulated" if request["path"].endswith("/add") else "no_extraction"}}

        with FakeEverOS(respond) as api:
            environment = dict(os.environ, EVEROS_API_BASE=api.base,
                               EVEROS_USER_ID="windows-fixture", EVEROS_API_KEY="fixture-key",
                               EVEROS_API_VERSION="v2", EVEROS_READ_V1="0", EVEROS_WRITE="1", PYTHONUTF8="1")
            payload = json.dumps({"session_id": session, "hook_event_name": "Stop"})
            result = subprocess.run(command.split(), env=environment, input=payload, text=True,
                                    encoding="utf-8", capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = self.home / ".everos/state/sessions" / (session + ".json")
            deadline = time.monotonic() + 10
            while not ledger.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ledger.exists(), "worker did not find the transcript using the bound desktop home")
            self.assertEqual(json.loads(ledger.read_text(encoding="utf-8"))["messages"], 2)
            self.assertTrue(any(row["path"].endswith("/add") for row in api.requests))

    def test_kimi_rule_sync_preserves_codex_target(self):
        WindowsHost(REPO).sync_rules()
        KimiDesktopHost(REPO).sync_rules()
        state = self.home / ".everos/state/rules"
        current = json.loads((state / "current.json").read_text(encoding="utf-8"))
        report = json.loads((state / "targets.json").read_text(encoding="utf-8"))
        self.assertEqual(report["revision"], current["revision"])
        self.assertEqual(report["scope"], ["codex", "kimi_desktop"])
        self.assertEqual({row["host"] for row in report["targets"]}, {"codex", "kimi_desktop"})

    @unittest.skipUnless(os.name == "nt", "native PowerShell entrypoint")
    def test_powershell_kimi_preview_does_not_write_or_require_codex(self):
        before = {str(path.relative_to(self.home)): path.read_bytes()
                  for path in self.home.rglob("*") if path.is_file()}
        result = subprocess.run([
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(REPO / "install.ps1"), "-Python", sys.executable,
            "-DryRun", "-KimiDesktop",
        ], env=dict(os.environ, PATH=""), capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runtime\\windows-", result.stdout)
        self.assertIn("hook.py", result.stdout)
        after = {str(path.relative_to(self.home)): path.read_bytes()
                 for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
