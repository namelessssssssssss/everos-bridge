"""Fault-injection regressions for read recovery; no real API or credentials."""
from __future__ import annotations

import http.client
import hashlib
import io
import json
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

from test_core_regressions import IsolatedCore, core


def http_error(status, payload=None, stream=None):
    if stream is None:
        stream = io.BytesIO(json.dumps(payload or {"message": "temporary failure"}).encode())
    return urllib.error.HTTPError("https://memory.invalid/search", status, "test error",
                                  {"x-request-id": "synthetic-request"}, stream)


def episodes(identifier="recovered"):
    return {"data": {"episodes": [{"id": identifier, "summary": "synthetic answer", "score": .9}]}}


class TruncatedErrorBody(io.BytesIO):
    def read(self, size=-1):
        raise http.client.IncompleteRead(b"partial", 10)


class ReadResilienceTests(IsolatedCore):
    def test_malformed_primary_data_recovers_without_consuming_total_deadline(self):
        for malformed in ([{"unexpected": "array"}], {"episodes": [None]},
                          {"episodes": "not a list"}, {"episodes": ["not an object"]}):
            with self.subTest(data=malformed):
                self.api.reset_mock()
                self.api.side_effect = lambda path, body, timeout: (
                    {"data": malformed} if body["method"] == "agentic" else episodes())
                notices = []
                started = time.monotonic()
                items = core.search_items("synthetic question", kind="episodes", method="agentic",
                                          budget=1, notices=notices)
                self.assertEqual([item["id"] for item in items], ["recovered"])
                self.assertEqual([call.args[1]["method"] for call in self.api.call_args_list],
                                 ["agentic", "hybrid"])
                self.assertLess(time.monotonic() - started, .8)
                self.assertFalse(any("总时限" in notice for notice in notices))

    def test_incomplete_http_error_body_keeps_status_and_retries_safe_read(self):
        failure = core.read_failure(http_error(503, stream=TruncatedErrorBody()), {"method": "hybrid"})
        self.assertEqual(failure["status"], 503)
        self.assertTrue(failure["retryable"])
        self.api.side_effect = [http_error(503, stream=TruncatedErrorBody()), episodes()]
        results = core._read_jobs({"v2u": ("/api/v2/memory/search", {
            "query": "synthetic", "method": "hybrid", "user_id": "test_owner"})}, 1)
        self.assertEqual(results["v2u"]["episodes"][0]["id"], "recovered")
        self.assertEqual(self.api.call_count, 2)

    def test_truncated_200_response_retries_through_real_api_for_safe_read(self):
        self.net.side_effect = [TruncatedErrorBody(), io.BytesIO(json.dumps(episodes()).encode())]
        with patch.object(core, "api", self.real_api):
            items = core.search_items("synthetic", kind="episodes", method="hybrid", budget=1, strict=True)
        self.assertEqual([item["id"] for item in items], ["recovered"])
        self.assertEqual(self.net.call_count, 2)
        for call in self.net.call_args_list:
            request = call.args[0]
            self.assertTrue(request.full_url.endswith("/api/v2/memory/search"))
            self.assertEqual(json.loads(request.data)["method"], "hybrid")
        self.assertIn('"exception":"IncompleteRead"', core.LOG.read_text())

    def test_truncated_200_response_does_not_retry_memory_write(self):
        self.net.side_effect = lambda *args, **kwargs: TruncatedErrorBody()
        with patch.object(core, "api", self.real_api), self.assertRaises(http.client.IncompleteRead):
            core.post_messages("synthetic-write", [{"role": "user", "content": "synthetic content"}], ver="v2")
        self.assertEqual(self.net.call_count, 1)
        self.assertTrue(self.net.call_args.args[0].full_url.endswith("/api/v2/memory/add"))

    def test_422_is_not_retried_and_notice_identifies_http_error(self):
        def fake(path, body, timeout):
            if body["method"] == "agentic":
                raise http_error(422, {"detail": [{"type": "value_error", "loc": ["body", "query"],
                                                   "msg": "Invalid query"}]})
            return episodes()

        self.api.side_effect = fake
        notices = []
        items = core.search_items("synthetic", kind="episodes", method="agentic", budget=1, notices=notices)
        self.assertEqual([item["id"] for item in items], ["recovered"])
        self.assertEqual([call.args[1]["method"] for call in self.api.call_args_list], ["agentic", "hybrid"])
        self.assertTrue(any("HTTP 422" in notice and "hybrid" in notice for notice in notices))
        self.assertFalse(any("失败或超时" in notice for notice in notices))

    def test_503_retries_once_and_can_recover_without_changing_method(self):
        self.api.side_effect = [http_error(503), episodes()]
        items = core.search_items("synthetic", kind="episodes", method="hybrid", budget=1)
        self.assertEqual([item["id"] for item in items], ["recovered"])
        self.assertEqual(self.api.call_count, 2)
        self.assertTrue(all(call.args[1]["method"] == "hybrid" for call in self.api.call_args_list))

    def test_repeated_transient_failure_stops_after_one_retry(self):
        def unavailable(*args, **kwargs):
            raise http_error(503)

        self.api.side_effect = unavailable
        with self.assertRaises(core.APIResponseError):
            core.search_items("synthetic", kind="episodes", method="hybrid", budget=1, strict=True)
        self.assertEqual(self.api.call_count, 2)

    def test_failed_track_falls_back_before_slow_sibling_finishes(self):
        fallback_started = threading.Event()
        slow_finished = threading.Event()
        observed = []

        def fake(path, body, timeout):
            if body.get("agent_id"):
                fallback_started.wait(.4)
                slow_finished.set()
                return {"data": {"agent_cases": [{"id": "practice", "task_intent": "synthetic practice"}]}}
            if body["method"] == "agentic":
                raise http_error(422)
            observed.append(not slow_finished.is_set())
            fallback_started.set()
            return episodes()

        self.api.side_effect = fake
        items = core.search_items("synthetic", method="agentic", budget=1)
        self.assertEqual(observed, [True])
        self.assertEqual({item["id"] for item in items}, {"practice", "recovered"})
        self.assertEqual(sum(bool(call.args[1].get("agent_id")) for call in self.api.call_args_list), 1)

    def test_deadline_is_logged_and_request_text_identity_and_key_are_absent(self):
        release = threading.Event()
        late_logged = threading.Event()
        original_log = core.log

        def log(message):
            original_log(message)
            if '"status":"late_result"' in message:
                late_logged.set()

        def slow(path, body, timeout):
            release.wait(1)
            return episodes()

        self.api.side_effect = slow
        self.stack.enter_context(patch.object(core, "log", side_effect=log))
        try:
            results = core._read_jobs({"v2u": ("/api/v2/memory/search", {
                "query": "private_question_needle", "user_id": "test_owner", "method": "agentic"})}, .04)
            self.assertNotIn("v2u", results)
            self.assertEqual(results.failures["v2u"]["kind"], "deadline")
            log = core.LOG.read_text()
            self.assertIn('"status":"deadline"', log)
            self.assertIn('"method":"agentic"', log)
            for private in ("private_question_needle", "test_owner", "test_key"):
                self.assertNotIn(private, log)
        finally:
            release.set()
            self.assertTrue(late_logged.wait(1))
        # Late transport success must not be counted as accepted query success.
        events = [json.loads(line[line.index("{"):]) for line in core.LOG.read_text().splitlines()]
        self.assertEqual({event["status"] for event in events}, {"deadline", "late_result"})
        self.assertEqual(len({event["operation"] for event in events}), 1)

    def test_validation_error_projection_omits_echoed_inputs_and_credentials(self):
        body = {"query": "private_question_needle", "user_id": "test_owner", "method": "hybrid"}
        payload = {"detail": [{"type": "value_error", "loc": ["body", "query"],
                               "msg": "private_question_needle test_owner test_key Bearer fakecredential",
                               "input": {"secret_input": "must_not_be_logged"}}]}
        self.api.side_effect = http_error(422, payload)
        results = core._read_jobs({"v2u": ("/api/v2/memory/search", body)}, 1)
        self.assertEqual(results.failures["v2u"]["status"], 422)
        log = core.LOG.read_text()
        for private in ("private_question_needle", "test_owner", "test_key", "fakecredential",
                        "must_not_be_logged", "secret_input"):
            self.assertNotIn(private, log)
        self.assertIn("value_error", log)

    def test_shared_operation_correlates_both_tracks_and_fallback(self):
        def fake(path, body, timeout):
            if body.get("agent_id"):
                return {"data": {"agent_cases": [{"id": "practice", "task_intent": "synthetic practice"}]}}
            if body["method"] == "agentic":
                raise http_error(422)
            return episodes()

        self.api.side_effect = fake
        core.search_items("synthetic", method="agentic", budget=1)
        events = [json.loads(line[line.index("{"):]) for line in core.LOG.read_text().splitlines()]
        self.assertEqual(len(events), 3)
        self.assertEqual(len({event["operation"] for event in events}), 1)
        self.assertEqual({(event["track"], event["method"]) for event in events},
                         {("v2u", "agentic"), ("v2a", "agentic"), ("v2u", "hybrid")})

    def test_request_id_header_cannot_bypass_known_secret_redaction(self):
        error = http_error(422)
        error.headers["x-request-id"] = "test_key"
        self.api.side_effect = error
        core._read_jobs({"v2u": ("/api/v2/memory/search", {
            "query": "synthetic", "method": "hybrid", "user_id": "test_owner"})}, 1)
        self.assertNotIn("test_key", core.LOG.read_text())

    def test_error_free_text_is_omitted_for_repr_json_escapes_and_overlapping_secrets(self):
        query = "test_owner\nprivate_line_needle\\quoted 'payload'"
        body = {"query": query, "user_id": "test_owner", "method": "hybrid"}
        for echoed in (query, repr(query), json.dumps(query), query.replace("test_owner", "[redacted]")):
            for payload in (
                    {"message": echoed}, {"detail": echoed}, {"error": echoed},
                    {"detail": [{"type": "value_error", "loc": ["body", "query"], "msg": echoed, "input": query}]},
                    {"error": {"code": "InvalidParameter", "param": "query", "message": echoed}}):
                with self.subTest(echo=echoed, payload=payload):
                    failure = core.read_failure(http_error(422, payload), body)
                    stored = json.dumps(failure)
                    for private in ("private_line_needle", "test_owner", "quoted", "payload"):
                        self.assertNotIn(private, stored)
                    self.assertNotIn("message", failure)
                    if isinstance(failure.get("detail"), list):
                        self.assertEqual(failure["detail"], [{"type": "value_error", "loc": ["body", "query"]}])
                    else:
                        self.assertNotIn("detail", failure)

    def test_unknown_diagnostic_symbols_cannot_carry_private_identifier_text(self):
        private = "identifier_shaped_private_payload"
        payload = {"code": private, "type": private, "param": private, "loc": ["body", private],
                   "detail": [{"type": private, "loc": ["body", private], "msg": private}]}
        failure = core.read_failure(http_error(422, payload), {"query": "synthetic"})
        for key in ("code", "type", "param", "loc", "detail"):
            self.assertNotIn(key, failure)
        self.assertNotIn(private, json.dumps(failure))

    def test_known_error_codes_and_structured_validation_fields_remain_useful(self):
        for code in ("InvalidParameter", "PROVIDER_NOT_CONFIGURED", "INVALID_INPUT", "validation_error", 422):
            with self.subTest(code=code):
                payload = {"error": {"code": code, "param": "private_param_needle", "message": "private_message_needle",
                                      "detail": [{"type": "value_error", "loc": ["body", "filters", 123456789],
                                                  "msg": "private_message_needle"}]}}
                failure = core.read_failure(http_error(422, payload), {"query": "synthetic"})
                self.assertEqual(failure["code"], code)
                self.assertNotIn("param", failure)
                self.assertEqual(failure["detail"], [{"type": "value_error", "loc": ["body", "filters", "[index]"]}])
                self.assertNotIn("needle", json.dumps(failure))
                self.assertNotIn("123456789", json.dumps(failure))

    def test_request_id_keeps_only_a_digest_even_for_arbitrary_echoed_text(self):
        echoed = "private_header_needle\n" + repr("test_owner\nprivate query")
        error = http_error(422)
        error.headers["x-request-id"] = echoed
        failure = core.read_failure(error, {"query": "synthetic"})
        self.assertNotIn("request_id", failure)
        self.assertEqual(failure["request_id_sha256"], hashlib.sha256(echoed.encode()).hexdigest())
        for private in ("private_header_needle", "test_owner", "private query"):
            self.assertNotIn(private, json.dumps(failure))

    def test_rotation_denied_still_appends_then_recovers_on_next_write(self):
        core.LOG.parent.mkdir(parents=True, exist_ok=True)
        core.LOG.write_text("old record\n" + "x" * 1_000_001)
        with patch.object(Path, "replace", side_effect=PermissionError("reader denies replacement")):
            core.log("kept during denied rotation")
        self.assertIn("kept during denied rotation", core.LOG.read_text())
        core.log("rotation recovered")
        self.assertIn("rotation recovered", core.LOG.read_text())
        self.assertIn("kept during denied rotation", core.LOG.with_suffix(".log.1").read_text())

    def test_open_log_reader_does_not_lose_current_or_following_event(self):
        core.LOG.parent.mkdir(parents=True, exist_ok=True)
        core.LOG.write_text("old record\n" + "x" * 1_000_001)
        with core.LOG.open("r", encoding="utf-8") as reader:
            core.log("event while reader holds log")
            self.assertIn("event while reader holds log", core.LOG.read_text())
            self.assertEqual(reader.readline(), "old record\n")
        core.log("event after reader closes")
        retained = core.LOG.read_text()
        backup = core.LOG.with_suffix(".log.1")
        if backup.exists():
            retained += backup.read_text()
        self.assertIn("event while reader holds log", retained)
        self.assertIn("event after reader closes", retained)

    def test_long_query_never_sends_a_truncation_suffix_beyond_transport_limit(self):
        self.api.return_value = {"data": {}}
        query = "历史查询" * 200
        core.search_items(query, method="agentic", budget=1)
        self.assertGreaterEqual(self.api.call_count, 2)
        for call in self.api.call_args_list:
            transmitted = call.args[1]["query"]
            self.assertEqual(transmitted, query[:500])
            self.assertLessEqual(len(transmitted), 500)


if __name__ == "__main__":
    import unittest
    unittest.main()
