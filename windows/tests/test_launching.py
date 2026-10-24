"""Regression cases for PR #16's Windows paths, batch shims and encoding."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows"))
from host import WindowsHost
from install import WindowsInstaller


class AbsolutePathTests(unittest.TestCase):
    def test_relative_paths_are_bound_before_cwd_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            origin = Path(temporary) / "origin"
            other = Path(temporary) / "other"
            origin.mkdir(); other.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(origin)
                with patch.dict(os.environ, {"EVEROS_HOME": "./data", "EVEROS_INSTALL_HOME": "./home", "CODEX_HOME": "./codex"}):
                    installer = WindowsInstaller(ROOT)
                    runtime = installer.snapshot()
                os.chdir(other)
                self.assertEqual(installer.everos, origin / "data")
                self.assertEqual(installer.home, origin / "home")
                self.assertEqual(installer.environment["CODEX_HOME"], str(origin / "codex"))
                hooks = json.loads((runtime / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))
                command = hooks["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
                decoded = base64.b64decode(command.split()[-1]).decode("utf-16le")
                self.assertIn(str(origin / "data"), decoded)
                self.assertTrue(runtime.is_absolute())
            finally:
                os.chdir(previous)

    @unittest.skipUnless(os.name == "nt", "Windows junction validation")
    def test_absolute_conversion_does_not_hide_a_junction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"; target.mkdir()
            link = root / "junction"
            shell = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
            subprocess.run('"{}" /d /c mklink /J "{}" "{}"'.format(shell, link, target),
                           check=True, stdout=subprocess.DEVNULL)
            try:
                with patch.dict(os.environ, {"EVEROS_HOME": str(link / "data")}):
                    installer = WindowsInstaller(ROOT)
                    self.assertIn(link, installer.everos.parents)
                    with self.assertRaisesRegex(ValueError, "reparse point"):
                        installer.private_home()
                self.assertFalse((target / "data").exists())
            finally:
                link.rmdir()  # Remove only the fixture junction, never its target.


@unittest.skipUnless(os.name == "nt", "native Windows command parsing")
class WindowsLaunchingTests(unittest.TestCase):
    def test_shim_only_codex_preserves_arguments_and_does_not_expand_shell_data(self):
        with tempfile.TemporaryDirectory(prefix="everos-shim-路径 α ") as temporary:
            root = Path(temporary)
            binary = root / "命令 shim & test"; binary.mkdir()
            script = root / "echo.py"
            script.write_text('import json,sys; print(json.dumps(sys.argv[1:]))\n', encoding="ascii")
            shim = binary / "codex.cmd"
            shim.write_text('@"%EVEROS_TEST_PYTHON%" -X utf8 "%EVEROS_TEST_SCRIPT%" %*\n', encoding="ascii")
            host = WindowsHost(ROOT)
            host.environment["PATH"] = str(binary)
            host.environment["EVEROS_TEST_PYTHON"] = sys.executable
            host.environment["EVEROS_TEST_SCRIPT"] = str(script)
            arguments = ["space value", "a&b", "percent%PATH%", "bang!caret^", "(paren)", "中文α", "x>fixture"]
            output = host.run(["codex", *arguments], capture=True)
            self.assertEqual(json.loads(output), arguments)
            self.assertEqual(Path(host.codex_path()).suffix.lower(), ".cmd")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                host.run(["codex", 'x" & invalid'], capture=True)

    def test_unicode_launchers_execute_with_legacy_cmd_codepage(self):
        with tempfile.TemporaryDirectory(prefix="everos-cli-") as temporary:
            root = Path(temporary)
            source = root / "运行 路径 α"
            cli = source / "plugin/everos/cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text('import json,sys\nif sys.argv[1:] == ["fail"]:\n    sys.stderr.buffer.write("error \\u03b1\\n".encode("utf-8")); sys.exit(7)\nprint(json.dumps(sys.argv[1:]))\n', encoding="ascii")
            home = root / "用户 路径 α"
            with patch.dict(os.environ, {"EVEROS_INSTALL_HOME": str(home)}):
                WindowsInstaller(source).install_cli()
            batch = home / ".local/bin/everos.cmd"
            powershell_script = batch.with_suffix(".ps1")
            self.assertTrue(batch.read_bytes().isascii())
            self.assertTrue(powershell_script.read_bytes().startswith(b'\xef\xbb\xbf'))
            arguments = ["search", "中文α and spaces", "--context", "a&b ()"]
            shell = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
            command = '"{}" /d /v:off /s /c "chcp 437 >nul & call "{}" {}"'.format(
                shell, batch, subprocess.list2cmdline(arguments))
            result = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), arguments)
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                     str(powershell_script.with_name("everos-native.ps1")), "fail"], text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(result.stderr.strip(), "error α")
            arguments = ["search", 'value "quoted" text', "", "trail" + chr(92)]
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                     str(powershell_script), *arguments], text=True, encoding="utf-8", capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), arguments)
