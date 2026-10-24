"""CLI acceptance tests using isolated configuration and a loopback-only API.

No test uses real credentials, user configuration, or a remote EverOS endpoint.
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "bin/everos"
SOURCE = ROOT / "plugin/everos"

# subprocess tests execute the actual launcher. This guard prevents accidental
# remote requests even if a configuration-precedence regression chooses a real
# API base; it also detects attempted requests on help/validation-only paths.
NETWORK_GUARD = '''
import os
import urllib.parse
import urllib.request

_real_urlopen = urllib.request.urlopen

def _local_only_urlopen(request, *args, **kwargs):
    with open(os.environ["CLI_TEST_NETWORK_LOG"], "a") as output:
        output.write("attempt\\n")
    address = request.full_url if hasattr(request, "full_url") else request
    parsed = urllib.parse.urlsplit(address)
    allowed = urllib.parse.urlsplit(os.environ.get("CLI_TEST_ALLOWED_BASE", ""))
    if not (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
            and parsed.port == allowed.port and allowed.hostname == "127.0.0.1"):
        raise RuntimeError("Remote networking is forbidden in CLI tests")
    return _real_urlopen(request, *args, **kwargs)

urllib.request.urlopen = _local_only_urlopen
'''


class FakeEverOS:
    """Small HTTP fixture; responder returns (HTTP status, JSON response)."""

    def __init__(self, responder):
        self.requests = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                request = {"path": self.path, "body": body,
                           "authorization": self.headers.get("Authorization")}
                with fixture.lock:
                    fixture.requests.append(request)
                status, result = responder(request)
                encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def matching_results(request):
    if request["body"].get("agent_id"):
        data = {"agent_cases": [{"id": "case-one", "score": 0.8,
                                 "task_intent": "Organize garden notes",
                                 "key_insight": "Keep a dated journal"}]}
    else:
        data = {"episodes": [{"id": "episode-one", "score": 0.9,
                              "timestamp": "2026-10-12T00:00:00Z",
                              "summary": "Garden journal started in October"}]}
    return 200, {"code": 0, "data": data}


class CliProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "unrelated working directory"
        self.cwd.mkdir()
        self.home = self.root / "everos home"
        self.home.mkdir()
        self.guard = self.root / "python guard"
        self.guard.mkdir()
        (self.guard / "sitecustomize.py").write_text(NETWORK_GUARD, encoding="utf-8")
        self.network_log = self.root / "network-attempts"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("EVEROS_") and key != "CLAUDE_CODE_REMOTE"
                    and key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        self.env.update(EVEROS_HOME=str(self.home), PYTHONPATH=str(self.guard),
                        PYTHONDONTWRITEBYTECODE="1", CLI_TEST_NETWORK_LOG=str(self.network_log),
                        CLI_TEST_ALLOWED_BASE="", NO_PROXY="127.0.0.1,localhost",
                        PATH=str(Path(sys.executable).parent) + os.pathsep + "/usr/bin:/bin")

    def run_cli(self, *args, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        self.network_log.unlink(missing_ok=True)
        return subprocess.run([sys.executable, "-X", "utf8", str(ENTRY), *args], cwd=self.cwd, env=env,
                              text=True, capture_output=True, timeout=10)

    def configured_env(self, api):
        return {"EVEROS_API_BASE": api.base, "CLI_TEST_ALLOWED_BASE": api.base,
                "EVEROS_USER_ID": "cli-owner", "EVEROS_API_KEY": "fixture-key",
                "EVEROS_API_VERSION": "v2", "EVEROS_READ_V1": "0"}

    def assert_no_network(self):
        self.assertFalse(self.network_log.exists(), "CLI attempted a network request")

    def test_launcher_help_from_another_cwd_needs_no_configuration_or_network(self):
        for args in (("--help",), ("search", "--help")):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("search", result.stdout)
                self.assert_no_network()
        self.assertFalse((self.home / "state").exists())

    def test_bad_parameters_fail_before_configuration_or_network(self):
        invalid = [(), ("unknown",), ("search",), ("search", "   "),
                   ("search", "x" * 501),
                   ("search", "garden notes", "--kind", "profiles"),
                   ("search", "garden notes", "--method", "vector")]
        invalid.extend(("search", "garden notes", "--top-k", value)
                       for value in ("-2", "0", "101", "abc"))
        invalid.extend(("search", "garden notes", "--timeout", value)
                       for value in ("0", "-1", "abc", "", "nan", "inf", "-inf"))
        for args in invalid:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assert_no_network()

    def test_missing_configuration_is_a_runtime_error_without_network(self):
        result = self.run_cli("search", "garden notes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("EVEROS", result.stderr)
        self.assert_no_network()

    def test_default_search_queries_both_tracks_and_prints_results(self):
        with FakeEverOS(matching_results) as api:
            result = self.run_cli("search", "  garden notes  ", extra_env=self.configured_env(api))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Garden journal started in October", result.stdout)
        self.assertIn("Keep a dated journal", result.stdout)
        self.assertEqual(len(api.requests), 2)
        self.assertEqual({r["path"] for r in api.requests}, {"/api/v2/memory/search"})
        for request in api.requests:
            self.assertEqual(request["body"]["query"], "garden notes")
            self.assertEqual(request["body"]["method"], "hybrid")
            self.assertEqual(request["body"]["top_k"], 5)
            self.assertEqual(request["authorization"], "Bearer fixture-key")
        self.assertFalse((self.home / "state/recall").exists())
        self.assertFalse((self.home / "state/sessions").exists())

    def test_episode_kind_preserves_each_official_top_k_boundary(self):
        for count in (-1, 1, 100):
            with self.subTest(top_k=count), FakeEverOS(matching_results) as api:
                result = self.run_cli("search", "garden notes", "--kind", "episodes",
                                      "--top-k", str(count), extra_env=self.configured_env(api))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(api.requests), 1)
            body = api.requests[0]["body"]
            self.assertEqual(body["user_id"], "cli-owner")
            self.assertNotIn("agent_id", body)
            self.assertEqual(body["top_k"], count)

    def test_query_accepts_exactly_500_characters_without_truncation(self):
        question = "种" * 500
        with FakeEverOS(matching_results) as api:
            result = self.run_cli("search", question, "--kind", "episodes",
                                  extra_env=self.configured_env(api))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(api.requests[0]["body"]["query"], question)

    def test_agentic_practices_use_agent_track_and_keep_low_scored_results(self):
        def low_scored_practice(request):
            return 200, {"code": 0, "data": {"agent_cases": [{
                "id": "deep-case", "score": 0.01, "task_intent": "Grow herbs",
                "key_insight": "Use the sunny windowsill"}]}}

        with FakeEverOS(low_scored_practice) as api:
            result = self.run_cli("search", "garden notes", "--kind", "practices",
                                  "--method", "agentic", "--top-k", "-1",
                                  extra_env=self.configured_env(api))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Use the sunny windowsill", result.stdout)
        self.assertEqual(len(api.requests), 1)
        body = api.requests[0]["body"]
        self.assertEqual(body["agent_id"], "cli-owner_agent")
        self.assertNotIn("user_id", body)
        self.assertEqual(body["method"], "agentic")
        self.assertEqual(body["top_k"], -1)
        self.assertNotIn("min_score", body)

    def test_successful_empty_search_is_distinct_from_failure(self):
        with FakeEverOS(lambda request: (200, {"code": 0, "data": {}})) as api:
            result = self.run_cli("search", "garden notes", extra_env=self.configured_env(api))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("没有相关记忆", result.stdout)

    def test_http_and_business_failures_are_nonzero_not_empty_results(self):
        for status, response in ((503, {"code": 503, "message": "fixture unavailable"}),
                                 (200, {"code": 500, "message": "fixture rejected"})):
            with self.subTest(status=status), FakeEverOS(lambda request: (status, response)) as api:
                result = self.run_cli("search", "garden notes", extra_env=self.configured_env(api))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue(result.stderr.strip())
            self.assertNotIn("没有相关记忆", result.stdout)
            self.assertNotIn("fixture-key", result.stderr)

    def test_one_failed_track_keeps_usable_results(self):
        def partial_result(request):
            if request["body"].get("agent_id"):
                return 503, {"code": 503, "message": "fixture practice track unavailable"}
            return matching_results(request)

        with FakeEverOS(partial_result) as api:
            result = self.run_cli("search", "garden notes", extra_env=self.configured_env(api))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Garden journal started in October", result.stdout)
        self.assertIn("以下仅展示成功轨道", result.stdout)

    def test_config_file_can_supply_all_credentials_and_api_settings(self):
        with FakeEverOS(matching_results) as api:
            (self.home / "config.env").write_text(
                "EVEROS_API_BASE={}\nEVEROS_USER_ID=file-owner\n"
                "EVEROS_API_KEY=file-fixture-key\nEVEROS_API_VERSION=v2\n"
                "EVEROS_READ_V1=0\n".format(api.base), encoding="utf-8")
            result = self.run_cli("search", "garden notes", "--kind", "episodes",
                                  extra_env={"CLI_TEST_ALLOWED_BASE": api.base})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(api.requests[0]["body"]["user_id"], "file-owner")
        self.assertEqual(api.requests[0]["authorization"], "Bearer file-fixture-key")

    def test_environment_overrides_file_configuration(self):
        (self.home / "config.env").write_text(
            "EVEROS_API_BASE=https://invalid.example\nEVEROS_USER_ID=file-owner\n"
            "EVEROS_API_KEY=file-fixture-key\nEVEROS_API_VERSION=v1\n"
            "EVEROS_READ_V1=1\n", encoding="utf-8")
        with FakeEverOS(matching_results) as api:
            result = self.run_cli("search", "garden notes", "--kind", "episodes",
                                  extra_env=self.configured_env(api))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(api.requests[0]["path"], "/api/v2/memory/search")
        self.assertEqual(api.requests[0]["body"]["user_id"], "cli-owner")
        self.assertEqual(api.requests[0]["authorization"], "Bearer fixture-key")


class CliUnitTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SOURCE))
        self.addCleanup(lambda: sys.path.remove(str(SOURCE)))
        self.cli = importlib.import_module("cli")
        self.core = importlib.import_module("core")

    def invoke(self, args):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            try:
                result = self.cli.main(args)
            except SystemExit as exit_result:
                result = exit_result.code
        return result, output.getvalue(), errors.getvalue()

    def test_cli_selects_strict_search_and_preserves_agentic_parameters(self):
        signature = inspect.signature(self.core.search_text)
        with mock.patch.object(self.core, "ready", return_value=True), \
                mock.patch.object(self.core, "search_text", return_value="fixture result") as search:
            code, output, errors = self.invoke([
                "search", "garden notes", "--kind", "practices", "--method", "agentic", "--top-k", "-1"])
        self.assertEqual(code, 0, errors)
        self.assertIn("fixture result", output)
        search.assert_called_once()
        forwarded = signature.bind(*search.call_args.args, **search.call_args.kwargs).arguments
        self.assertEqual(forwarded["query"], "garden notes")
        self.assertEqual(forwarded["kind"], "practices")
        self.assertEqual(forwarded["method"], "agentic")
        self.assertEqual(forwarded["top_k"], -1)
        self.assertIs(forwarded["strict"], True)
        self.assertNotIn("budget", forwarded)

    def test_cli_forwards_positive_finite_timeout_as_per_call_budget(self):
        signature = inspect.signature(self.core.search_text)
        for method in ("hybrid", "agentic"):
            for value in (".25", "180", "300.5"):
                with self.subTest(method=method, timeout=value), \
                        mock.patch.object(self.core, "ready", return_value=True), \
                        mock.patch.object(self.core, "search_text", return_value="fixture result") as search:
                    code, output, errors = self.invoke([
                        "search", "garden notes", "--method", method, "--timeout", value])
                self.assertEqual(code, 0, errors)
                forwarded = signature.bind(*search.call_args.args, **search.call_args.kwargs).arguments
                self.assertEqual(forwarded["budget"], float(value))
                self.assertEqual(forwarded["method"], method)
                self.assertIs(forwarded["strict"], True)

    def test_invalid_cli_timeout_is_rejected_before_configuration_and_dispatch(self):
        for value in ("0", "-1", "abc", "nan", "inf", "-inf", ""):
            with self.subTest(timeout=value), \
                    mock.patch.object(self.core, "ready") as ready, \
                    mock.patch.object(self.core, "search_text") as search:
                code, output, errors = self.invoke([
                    "search", "garden notes", "--method", "agentic", "--timeout", value])
                self.assertEqual(code, 2)
                self.assertTrue(errors)
                ready.assert_not_called()
                search.assert_not_called()

    def test_keyboard_interrupt_returns_130_without_traceback(self):
        with mock.patch.object(self.core, "ready", return_value=True), \
                mock.patch.object(self.core, "search_text", side_effect=KeyboardInterrupt):
            code, output, errors = self.invoke(["search", "garden notes"])
        self.assertEqual(code, 130)
        self.assertNotIn("Traceback", errors)


if __name__ == "__main__":
    unittest.main()
