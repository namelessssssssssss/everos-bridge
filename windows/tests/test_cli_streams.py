"""Real PowerShell pipeline and isolated console cancellation regressions."""
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
from install import WindowsInstaller


CLI = r'''
import os, sys, time
from pathlib import Path
root = Path(os.environ['EVEROS_TEST_ROOT'])
if sys.argv[1:] == ['wait']:
    try:
        (root / 'ready').write_text(str(os.getpid()), encoding='ascii')
        time.sleep(60)
    except KeyboardInterrupt:
        (root / 'cancelled').write_text('KeyboardInterrupt', encoding='ascii')
        sys.exit(130)
elif sys.argv[1:] == ['bytes']:
    sys.stdout.buffer.write(b'\x00\xff out \xce\xb1\r\n')
    sys.stderr.buffer.write(b'\x01\xfe err \xce\xb1\n')
    sys.exit(9)
elif sys.argv[1:] == ['streams']:
    for _ in range(40):
        print('out \u03b1 ' + 'x' * 8192, flush=True)
        print('err \u03b1 ' + 'y' * 8192, file=sys.stderr, flush=True)
    sys.exit(7)
else:
    print('help \u03b1')
    print('second line')
'''

# A hidden, separate console keeps CTRL_C_EVENT away from the test runner and
# the user's console. Only this helper and its fixture descendants share it.
CANCEL_PROBE = r'''
import ctypes, os, subprocess, sys, time
from pathlib import Path
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.restype = ctypes.c_void_p
kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
kernel.CloseHandle.argtypes = [ctypes.c_void_p]
kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
kernel.SetConsoleCtrlHandler(None, False)
root, entry = Path(sys.argv[1]), Path(sys.argv[2])
if entry.suffix == '.cmd':
    shell = str(Path(os.environ['SystemRoot']) / 'System32/cmd.exe')
    command = '"{}" /d /v:off /s /c ""{}" wait"'.format(shell, entry)
else:
    command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(entry), 'wait']
child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
handle = None
try:
    deadline = time.monotonic() + 10
    while not (root / 'ready').exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert (root / 'ready').exists(), 'CLI never became ready'
    # Retain this process handle before cancelling; never reopen a recycled PID.
    handle = kernel.OpenProcess(0x100001, False, int((root / 'ready').read_text(encoding='ascii')))
    assert handle, 'Cannot observe fixture process'
    # Set only after spawning, so descendants do not inherit ignore-Ctrl+C.
    assert kernel.SetConsoleCtrlHandler(None, True)
    assert kernel.GenerateConsoleCtrlEvent(0, 0)
    deadline = time.monotonic() + 5
    while not (root / 'cancelled').exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert (root / 'cancelled').exists(), 'Python child did not receive Ctrl+C'
    assert kernel.WaitForSingleObject(handle, 5000) == 0, 'Cancelled Python child did not exit'
    child.wait(timeout=5)
    print((root / 'cancelled').read_text(encoding='ascii'))
finally:
    if handle:
        if kernel.WaitForSingleObject(handle, 0) == 258:
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 5000)
        kernel.CloseHandle(handle)
    if child.poll() is None:
        child.kill()
    child.wait()
'''


@unittest.skipUnless(os.name == "nt", "PowerShell streams and Windows console events")
class CliStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="everos-stream-路径 α ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        cli = self.root / "source/plugin/everos/cli.py"
        cli.parent.mkdir(parents=True)
        cli.write_text(CLI, encoding="ascii")
        self.environment = dict(os.environ, EVEROS_TEST_ROOT=str(self.root), PYTHONUTF8="1")
        with patch.dict(os.environ, {"EVEROS_INSTALL_HOME": str(self.root / "home")}):
            WindowsInstaller(self.root / "source").install_cli()
        self.launcher = self.root / "home/.local/bin/everos.ps1"

    def powershell(self, code):
        code = "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; " + code
        encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
        return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                              env=self.environment, capture_output=True, text=True, encoding="utf-8", timeout=25)

    def test_direct_ps1_supports_variable_capture_and_file_redirection(self):
        self.environment["EVEROS_TEST_LAUNCHER"] = str(self.launcher)
        result = self.powershell(r'''
$captured = @(& $env:EVEROS_TEST_LAUNCHER --help)
& $env:EVEROS_TEST_LAUNCHER --help > "$env:EVEROS_TEST_ROOT/help.txt"
$redirected = @(Get-Content -LiteralPath "$env:EVEROS_TEST_ROOT/help.txt" | ForEach-Object { [string]$_ })
ConvertTo-Json -Compress @{ captured = $captured; redirected = $redirected }
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = ["help α", "second line"]
        self.assertEqual(json.loads(result.stdout), {"captured": expected, "redirected": expected})

    def test_direct_ps1_drains_both_streams_and_preserves_error_records_and_exit(self):
        self.environment["EVEROS_TEST_LAUNCHER"] = str(self.launcher)
        result = self.powershell(r'''
$items = @(& $env:EVEROS_TEST_LAUNCHER streams 2>&1)
$code = $LASTEXITCODE
$errors = @($items | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })
$output = @($items | Where-Object { $_ -is [string] })
ConvertTo-Json -Compress @{ code = $code; errors = $errors.Count; output = $output.Count; firstError = $errors[0].Exception.Message; firstOutput = $output[0] }
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"code": 7, "errors": 40, "output": 40,
                                                   "firstError": "err α " + "y" * 8192,
                                                   "firstOutput": "out α " + "x" * 8192})

    def test_cmd_preserves_native_bytes_on_both_streams_and_exit(self):
        shell = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        command = '"{}" /d /v:off /s /c ""{}" bytes"'.format(shell, self.launcher.with_suffix(".cmd"))
        result = subprocess.run(command, env=self.environment, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stdout, b'\x00\xff out \xce\xb1\r\n')
        self.assertEqual(result.stderr, b'\x01\xfe err \xce\xb1\n')

    def test_ctrl_c_reaches_python_through_both_launchers(self):
        probe = self.root / "cancel_probe.py"
        probe.write_text(CANCEL_PROBE, encoding="ascii")
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        for suffix in (".ps1", ".cmd"):
            with self.subTest(launcher=suffix):
                for name in ("ready", "cancelled"):
                    (self.root / name).unlink(missing_ok=True)
                result = subprocess.run([sys.executable, "-X", "utf8", str(probe), str(self.root), str(self.launcher.with_suffix(suffix))],
                                        env=self.environment, creationflags=subprocess.CREATE_NEW_CONSOLE,
                                        startupinfo=startup, capture_output=True, text=True, encoding="utf-8", timeout=25)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "KeyboardInterrupt")
