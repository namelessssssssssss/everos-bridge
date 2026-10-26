"""Prove the Unix path is unchanged and run its semantic tests on a Windows copy."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "windows"))
from install import WindowsInstaller
from platform_runtime import adapt


class IsolationTests(unittest.TestCase):
    def test_all_existing_unix_entrypoints_and_tests_match_the_baseline(self):
        baseline = json.loads((ROOT / "windows/linux-baseline.json").read_text(encoding="utf-8"))
        for name, expected in baseline["sha256"].items():
            with self.subTest(path=name):
                data = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected)

    def test_changed_upstream_adapter_anchor_is_rejected_without_source_writes(self):
        contents = {p.relative_to(ROOT): p.read_bytes() for p in (ROOT / "plugin").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        path = Path("plugin/everos/core.py")
        contents[path] = contents[path].replace(b"import fcntl", b"# changed upstream platform dependency")
        with self.assertRaisesRegex(ValueError, "anchor changed"):
            adapt(contents, b"fixture lock module")

    def test_snapshot_adapts_the_shared_diagnostic_log_lock(self):
        contents = {p.relative_to(ROOT): p.read_bytes() for p in (ROOT / "plugin").rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        generated = adapt(contents, b"fixture lock module")
        core = generated[Path("plugin/everos/core.py")].decode("utf-8")
        self.assertIn('open(LOG.with_suffix(".lock"), "a+b")', core)
        self.assertNotIn('open(LOG.with_suffix(".lock"), "a")', core)

    @unittest.skipUnless(os.name == "nt", "exercise generated Windows runtime")
    def test_unmodified_core_and_mcp_regressions_against_the_windows_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                "EVEROS_INSTALL_HOME": temporary, "EVEROS_HOME": str(Path(temporary) / ".everos")}):
            runtime = WindowsInstaller(ROOT).snapshot()
            tests = runtime / "tests"
            tests.mkdir()
            for name in ("test_core_regressions.py", "test_mcp.py", "test_read_resilience.py", "test_mcp_concurrency.py"):
                shutil.copyfile(ROOT / "tests" / name, tests / name)
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", str(tests), "-v"],
                                    cwd=runtime, env=dict(os.environ, PYTHONUTF8="1"), capture_output=True,
                                    text=True, encoding="utf-8", timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print("Unmodified core/MCP parity: " + next(line for line in result.stderr.splitlines() if line.startswith("Ran ")))
