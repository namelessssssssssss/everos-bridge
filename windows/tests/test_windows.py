"""Native Windows setup and process-lock checks, with no real credentials/API."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "windows"))
import windows_lock as filelock
from install import WindowsInstaller, hook_command


class ProcessLockTests(unittest.TestCase):
    def test_exclusion_and_release_across_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shared.lock"
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); import windows_lock as filelock; "
                "f=open(sys.argv[2], 'a+b'); filelock.flock(f, filelock.LOCK_EX); "
                "print('locked', flush=True); sys.stdin.readline()"
            )
            process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", code,
                                        str(REPO / "windows"), str(path)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "locked")
                with path.open("a+b") as lock:
                    with self.assertRaises(BlockingIOError):
                        filelock.flock(lock, filelock.LOCK_EX | filelock.LOCK_NB)
                    process.stdin.close()
                    self.assertEqual(process.wait(timeout=10), 0)
                    filelock.flock(lock, filelock.LOCK_EX | filelock.LOCK_NB)
                    filelock.flock(lock, filelock.LOCK_UN)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()


class WindowsSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="everos Windows ")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home 空间 ' & $"
        self.patch = patch.dict(os.environ, {
            "EVEROS_INSTALL_HOME": str(self.home), "EVEROS_HOME": str(self.home / ".everos"),
            "CODEX_HOME": str(self.home / ".codex"), "EVEROS_AGENTIC_TIMEOUT": "240",
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_snapshot_is_immutable_and_hooks_share_home_budget_and_interpreter(self):
        installer = WindowsInstaller(REPO)
        runtime = installer.snapshot()
        self.assertEqual(runtime, installer.snapshot())
        hooks = json.loads((runtime / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
        handler = hooks["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(handler["timeout"], 250)
        decoded = base64.b64decode(handler["commandWindows"].split()[-1]).decode("utf-16le")
        self.assertIn(str(installer.python).replace("'", "''"), decoded)
        self.assertIn(str(installer.everos).replace("'", "''"), decoded)
        self.assertIn("-X utf8", decoded)
        manifest = json.loads((runtime / "plugin/.codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertIn("-windows.", manifest["version"])
        self.assertFalse((runtime / "config.env").exists())
        (runtime / "plugin/everos/core.py").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "snapshot differs"):
            installer.snapshot()

    def test_dry_run_makes_no_files_or_cli_calls(self):
        installer = WindowsInstaller(REPO, dry=True)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess in preview")):
            installer.snapshot()
            installer.install_cli()
            installer.protocol_link()
        self.assertFalse(self.home.exists())

    def test_kimi_only_install_needs_no_host_cli_and_is_idempotent(self):
        kimi = self.home / ".kimi-code"
        environment = {"KIMI_CODE_HOME": str(kimi), "EVEROS_USER_ID": "fixture-user",
                       "EVEROS_API_KEY": "fixture-key"}
        with patch.dict(os.environ, environment):
            installer = WindowsInstaller(REPO, only="kimi")
            with patch("shutil.which", side_effect=AssertionError("no host CLI lookup")), \
                    patch.object(installer, "private_home", lambda: installer.everos.mkdir(parents=True, exist_ok=True)), \
                    patch.object(installer, "run", return_value="") as run:
                installer.install()
                mcp_path = kimi / "mcp.json"
                config_path = kimi / "config.toml"
                mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
                everos = mcp["mcpServers"]["everos"]
                self.assertTrue(Path(everos["command"]).is_absolute())
                self.assertEqual(everos["args"][:2], ["-X", "utf8"])
                self.assertEqual(everos["env"]["KIMI_CODE_HOME"], str(kimi))
                self.assertEqual(everos["toolTimeoutMs"], 250000)
                config = config_path.read_text(encoding="utf-8")
                self.assertEqual(config.count("[[hooks]]"), 4)
                self.assertIn('event = "SessionEnd"\n', config)
                stamps = {path: path.stat().st_mtime_ns for path in
                          (mcp_path, config_path, kimi / "AGENTS.md")}
                installer.install()
                self.assertEqual(stamps, {path: path.stat().st_mtime_ns for path in stamps})
                self.assertEqual(run.call_count, 2)
        self.assertFalse((self.home / ".codex").exists())

    def test_kimi_inline_hooks_are_rejected_before_any_write(self):
        kimi = self.home / ".kimi-code"
        kimi.mkdir(parents=True)
        original_mcp = '{"mcpServers":{"other":{"command":"keep"}}}\n'
        original_config = 'hooks = [{event = "Stop", command = "echo keep", timeout = 7}]\n'
        (kimi / "mcp.json").write_text(original_mcp, encoding="utf-8")
        (kimi / "config.toml").write_text(original_config, encoding="utf-8")
        with patch.dict(os.environ, {"KIMI_CODE_HOME": str(kimi)}):
            installer = WindowsInstaller(REPO, only="kimi")
            with patch.object(installer, "private_home", side_effect=AssertionError("must not write")):
                with self.assertRaisesRegex(ValueError, "inline hooks array"):
                    installer.install()
        self.assertEqual((kimi / "mcp.json").read_text(encoding="utf-8"), original_mcp)
        self.assertEqual((kimi / "config.toml").read_text(encoding="utf-8"), original_config)
        self.assertFalse((self.home / ".everos").exists())

    def test_kimi_only_full_dry_run_has_no_writes_or_subprocess(self):
        with patch.dict(os.environ, {"KIMI_CODE_HOME": str(self.home / ".kimi-code")}):
            installer = WindowsInstaller(REPO, dry=True, only="kimi")
            with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess in preview")):
                installer.install()
        self.assertFalse(self.home.exists())

    def test_kimi_wire_fixture_keeps_only_user_origin_and_visible_text(self):
        runtime = WindowsInstaller(REPO).snapshot()
        wire = self.home / "wire.jsonl"
        self.home.mkdir(parents=True, exist_ok=True)
        entries = [
            {"type": "context.append_message", "time": 1000, "message": {"role": "user",
             "origin": {"kind": "user"}, "content": [{"type": "text", "text": "visible user"},
             {"type": "image", "text": "not text"}]}},
            {"type": "context.append_message", "time": 2000, "message": {"role": "user",
             "origin": {"kind": "injection"}, "content": [{"type": "text", "text": "exclude injection"}]}},
            {"type": "context.append_message", "time": 3000, "message": {"role": "system",
             "origin": {"kind": "user"}, "content": [{"type": "text", "text": "exclude system"}]}},
            {"type": "context.append_loop_event", "time": 4000, "event": {"type": "content.part",
             "stepUuid": "one", "part": {"type": "reasoning", "text": "exclude reasoning"}}},
            {"type": "context.append_loop_event", "time": 5000, "event": {"type": "content.part",
             "stepUuid": "one", "part": {"type": "text", "text": "visible assistant"}}},
        ]
        wire.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        source = runtime / "plugin/everos"
        previous_path = sys.path[:]
        previous_bytecode = sys.dont_write_bytecode
        try:
            sys.path.insert(0, str(source))
            sys.dont_write_bytecode = True
            spec = importlib.util.spec_from_file_location("windows_kimi_fixture_core", source / "core.py")
            core = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(core)
        finally:
            sys.path[:] = previous_path
            sys.dont_write_bytecode = previous_bytecode
        with patch.dict(os.environ, {"EVEROS_USER_ID": "fixture-user"}):
            messages = core.messages_kimi([json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()])
        self.assertEqual([(message["role"], message["content"]) for message in messages],
                         [("user", "visible user"), ("assistant", "visible assistant")])
        self.assertNotIn("exclude", json.dumps(messages))

    def test_cli_refresh_preserves_unrelated_commands(self):
        installer = WindowsInstaller(REPO)
        installer.install_cli()
        installer.install_cli()
        launcher = self.home / ".local/bin/everos.cmd"
        self.assertTrue(launcher.read_bytes().isascii())
        self.assertTrue(launcher.with_suffix(".ps1").read_bytes().startswith(b'\xef\xbb\xbf'))
        launcher.write_text("another program\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            installer.install_cli()
        self.assertEqual(launcher.read_text(encoding="utf-8"), "another program\n")

    @unittest.skipUnless(os.name == "nt", "native PowerShell command")
    def test_encoded_hook_preserves_stdin_unicode_and_quoted_paths(self):
        self.home.mkdir(parents=True)
        hook = self.home / "synthetic hook.py"
        hook.write_text("import json, os, sys; print(json.dumps([os.environ['EVEROS_HOME'], sys.stdin.read()], ensure_ascii=False))", encoding="utf-8")
        command = hook_command(sys.executable, hook, self.home)
        result = subprocess.run(command.split(), input="中文 JSON 输入", text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [str(self.home), "中文 JSON 输入"])

    @unittest.skipUnless(os.name == "nt", "native PowerShell command")
    def test_kimi_encoded_hook_sets_all_host_environment(self):
        self.home.mkdir(parents=True)
        hook = self.home / "synthetic environment.py"
        hook.write_text("import json, os; print(json.dumps({key: os.environ[key] for key in "
                        "['EVEROS_HOST', 'KIMI_CODE_HOME', 'EVEROS_HOME', 'PYTHONUTF8']}, ensure_ascii=False))",
                        encoding="utf-8")
        kimi = self.home / ".kimi-code"
        command = hook_command(sys.executable, hook, self.home / ".everos", host="kimi", kimi_home=kimi)
        result = subprocess.run(command.split(), text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"EVEROS_HOST": "kimi", "KIMI_CODE_HOME": str(kimi),
                         "EVEROS_HOME": str(self.home / ".everos"), "PYTHONUTF8": "1"})

    @unittest.skipUnless(os.name == "nt", "native ACL and PowerShell wrapper")
    def test_powershell_preview_and_private_acl(self):
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO / "install.ps1"),
                                 "-Python", sys.executable, "-DryRun", "-CliOnly"],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.home.exists())
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO / "install.ps1"),
                                 "-Python", sys.executable, "-DryRun", "-RulesOnly", "-Only", "kimi"],
                                env=dict(os.environ, KIMI_CODE_HOME=str(self.home / ".kimi-code")),
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.home.exists())
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(REPO / "install.ps1"),
                                 "-Python", sys.executable, "-DryRun", "-KimiDesktop", "-Only", "kimi"],
                                env=dict(os.environ, KIMI_CODE_HOME=str(self.home / ".kimi-code")),
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutually exclusive", result.stderr)
        self.assertFalse(self.home.exists())
        installer = WindowsInstaller(REPO)
        installer.everos.mkdir(parents=True)
        (installer.everos / "config.env").write_text("fixture-only", encoding="utf-8")
        subprocess.run(["icacls.exe", str(installer.everos), "/grant", "*S-1-1-0:(OI)(CI)R"],
                       check=True, stdout=subprocess.DEVNULL)
        installer.private_home()
        result = subprocess.run(["icacls.exe", str(installer.everos)], capture_output=True, text=True, encoding="oem")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("(I)", result.stdout)
        self.assertNotIn("Everyone", result.stdout)

    @unittest.skipUnless(os.name == "nt", "native background worker")
    def test_stop_worker_survives_hook_exit_and_writes_to_loopback_only(self):
        sys.path.insert(0, str(REPO / "tests"))
        from test_cli import FakeEverOS
        self.home.mkdir(parents=True)
        runtime = WindowsInstaller(REPO).snapshot()
        transcript = self.home / "synthetic.jsonl"
        transcript.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Synthetic Windows worker check"}]}}) + "\n", encoding="utf-8")
        def respond(request):
            return 200, {"data": {"status": "accumulated" if request["path"].endswith("/add") else "no_extraction"}}
        with FakeEverOS(respond) as api:
            environment = dict(os.environ, USERPROFILE=str(self.home), EVEROS_API_BASE=api.base,
                               EVEROS_USER_ID="windows-fixture", EVEROS_API_KEY="fixture-key",
                               EVEROS_API_VERSION="v2", EVEROS_READ_V1="0", EVEROS_WRITE="1", PYTHONUTF8="1")
            payload = json.dumps({"session_id": "windows-worker", "hook_event_name": "Stop", "transcript_path": str(transcript)})
            command = hook_command(sys.executable, runtime / "plugin/everos/hook.py", self.home / ".everos")
            result = subprocess.run(command.split(), env=environment, input=payload, text=True,
                                    encoding="utf-8", capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            ledger = self.home / ".everos/state/sessions/windows-worker.json"
            deadline = time.monotonic() + 10
            while not ledger.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ledger.exists(), "worker did not persist after the hook exited")
            self.assertEqual(json.loads(ledger.read_text(encoding="utf-8"))["messages"], 1)
            self.assertTrue(any(row["path"].endswith("/add") for row in api.requests))


if __name__ == "__main__":
    unittest.main()
