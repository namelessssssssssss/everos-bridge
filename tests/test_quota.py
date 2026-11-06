"""Hard-quota regressions; synthetic payloads, temporary state, no live API."""
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
from unittest.mock import patch

from test_core_regressions import IsolatedCore, core, response
import quota


def refusal(resource="mcu", balance=0, **extra):
    payload = dict(error="quota_exceeded", quota_type=resource, balance=balance, **extra)
    return urllib.error.HTTPError("https://memory.invalid/add", 429, "Too Many Requests", {},
                                  io.BytesIO(json.dumps(payload).encode()))


class QuotaTests(IsolatedCore):
    def gate(self, resource="mcu", **config):
        return quota.Gate(core.STATE, dict(core.cfg(), **config), resource)

    def real(self, path="/api/v2/memory/add"):
        return self.real_api(path, {})

    def test_hard_quota_preserves_safe_reason_and_pauses_all_write_paths(self):
        self.net.side_effect = refusal(message="private_question_needle test_key test_owner")
        with self.assertRaises(quota.QuotaError) as caught:
            self.real()
        self.assertIn("MCU", str(caught.exception))
        self.assertIn("balance=0", str(caught.exception))
        for path in quota.WRITE_PATHS:
            with self.assertRaises(quota.QuotaError) as repeated:
                self.real(path)
            self.assertTrue(repeated.exception.cached)
        self.assertEqual(self.net.call_count, 1)
        for p in (core.STATE / "quotas").iterdir():
            self.assertNotIn("test_key", p.name)
            for private in ("private_question_needle", "test_owner", "test_key"):
                self.assertNotIn(private, p.read_text())
            if os.name != "nt":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(core.LOG.read_text().count('"event":"quota_paused"'), 1)

    def test_mcu_exhaustion_does_not_block_reads(self):
        self.net.side_effect = [refusal(), io.BytesIO(b'{"data":{"episodes":[]}}')]
        with self.assertRaises(quota.QuotaError): self.real()
        self.assertEqual(self.real('/api/v2/memory/get'), {"data":{"episodes":[]}})
        self.assertEqual(self.net.call_count, 2)

    def test_retrieval_exhaustion_does_not_block_writes_or_retry_reads(self):
        self.net.side_effect = [refusal("retrieval"), io.BytesIO(b'{"data":{"status":"extracted"}}')]
        with patch.object(core, "api", self.real_api):
            found = core._read_jobs({"v2u": ('/api/v2/memory/search', {'method':'hybrid'})}, 1)
        self.assertFalse(found)
        self.assertFalse(found.failures['v2u']['retryable'])
        self.assertEqual(found.failures['v2u']['quota_type'], 'retrieval')
        self.assertEqual(self.real(), response('extracted'))
        self.assertEqual(self.net.call_count, 2)

    def test_hard_quota_does_not_trigger_agentic_hybrid_fallback(self):
        self.net.side_effect = refusal("retrieval")
        with patch.object(core, "api", self.real_api):
            notices = []
            results, methods = core._agentic_jobs({'v2u':('/api/v2/memory/search', {
                'method':'agentic','query':'synthetic'})}, time.monotonic()+1, .1, notices,
                primary_budget=.9, total_budget=1)
        self.assertEqual(self.net.call_count, 1)
        self.assertFalse(results)
        self.assertTrue(any('quota_exceeded' in notice for notice in notices))
        self.assertFalse(any('hybrid' in notice for notice in notices))
        self.assertEqual(results.failures['v2u']['code'], 'quota_exceeded')

    def test_plain_rate_limit_remains_a_retryable_http_error_with_body(self):
        payload = b'{"error":"rate_limit_exceeded","message":"temporary"}'
        self.net.side_effect = urllib.error.HTTPError('https://memory.invalid',429,'rate',{},io.BytesIO(payload))
        with self.assertRaises(urllib.error.HTTPError) as caught: self.real()
        failure = core.read_failure(caught.exception, {})
        self.assertEqual(failure['status'],429)
        self.assertTrue(failure['retryable'])
        self.assertFalse(self.gate().status()['paused'])

    def test_unknown_quota_type_cannot_inject_text_into_state(self):
        self.net.side_effect = refusal('private_question_needle', message='test_key')
        with self.assertRaises(urllib.error.HTTPError): self.real()
        self.assertFalse(self.gate().status()['paused'])

    def test_same_credential_shares_circuit_but_other_key_or_backend_is_isolated(self):
        self.gate().block(0)
        with self.assertRaises(quota.QuotaError): self.gate().before(1)
        self.assertIsNone(self.gate(EVEROS_API_KEY='other_synthetic_key').before(1))
        self.assertIsNone(self.gate(EVEROS_API_BASE='https://other.invalid').before(1))
        self.assertFalse(self.gate('retrieval').status()['paused'])

    def test_expired_circuit_allows_only_one_probe_and_success_clears_it(self):
        gate = self.gate(); gate.block(0); gate.retry()
        token = gate.before(1)
        with self.assertRaises(quota.QuotaError): self.gate().before(1)
        gate.finish(token, True)
        self.assertFalse(gate.status()['paused'])
        self.assertIsNone(gate.before(1))

    def test_failed_probe_keeps_pause_and_late_success_cannot_clear_new_refusal(self):
        gate = self.gate(); gate.block(0); gate.retry(); token = gate.before(1)
        gate.block(0)
        gate.finish(token, True)
        self.assertTrue(gate.status()['paused'])
        gate.retry(); next_token = gate.before(1); gate.finish(next_token, False)
        with self.assertRaises(quota.QuotaError): gate.before(1)

    def test_retry_does_not_revoke_in_flight_probe(self):
        gate = self.gate(); gate.block(0); gate.retry(); token = gate.before(1)
        gate.retry()
        with self.assertRaises(quota.QuotaError): gate.before(1)
        gate.finish(token, True)

    def test_probe_lease_is_shared_with_another_process(self):
        gate = self.gate(); gate.block(0); gate.retry()
        script = """import sys;from pathlib import Path;import quota
g=quota.Gate(Path(sys.argv[1]), {'EVEROS_API_BASE':'https://api.evermind.ai','EVEROS_API_KEY':'test_key'}, 'mcu')
try: print(g.before(1))
except quota.QuotaError: print('paused')
"""
        env = dict(os.environ, PYTHONPATH=str(core.Path(core.__file__).parent), PYTHONDONTWRITEBYTECODE='1')
        first = subprocess.run([sys.executable,'-c',script,str(core.STATE)], env=env, capture_output=True, text=True, check=True)
        second = subprocess.run([sys.executable,'-c',script,str(core.STATE)], env=env, capture_output=True, text=True, check=True)
        self.assertNotEqual(first.stdout.strip(),'paused')
        self.assertEqual(second.stdout.strip(),'paused')

    def test_failed_first_write_retains_source_identity_without_advancing_offset(self):
        payload = self.transcript(); self.api.side_effect = quota.QuotaError('mcu',0)
        with self.assertRaises(quota.QuotaError): core.write_turns(payload)
        ledger = self.ledger()
        self.assertEqual(ledger['transcript_path'],payload['transcript_path'])
        self.assertEqual(ledger['claude_session_id'],payload['session_id'])
        self.assertEqual(ledger.get('offset',0),0)
        self.assertNotIn('last_write_status',ledger)

    def test_cli_quota_status_and_retry_do_not_call_network(self):
        import cli
        gate = self.gate(); gate.block(0)
        output = io.StringIO()
        with patch('sys.stdout', output):
            self.assertEqual(cli.main(['quota','status']),0)
        self.assertTrue(json.loads(output.getvalue())[0]['paused'])
        with patch('sys.stdout', io.StringIO()):
            self.assertEqual(cli.main(['quota','retry','--resource','mcu']),0)
        self.assertIsNotNone(gate.before(1))
        self.net.assert_not_called()

    def test_cached_mcp_refusal_still_returns_error_without_repeated_log(self):
        import mcp_server
        self.api.side_effect = quota.QuotaError('mcu',0,cached=True)
        server = mcp_server.Server()
        result = server.handle({'method':'tools/call','params':{
            'name':'memory_remember','arguments':{'topic':'synthetic','content':'synthetic'}}})
        self.assertTrue(result['isError'])
        self.assertIn('quota_exceeded',result['content'][0]['text'])
        self.assertFalse(core.LOG.exists())

    def test_nonfinite_or_extreme_balance_is_not_persisted(self):
        for balance in (float('inf'),float('nan'),10**400,'test_key'):
            self.assertIsNone(quota.number(balance))


    def test_strict_search_surfaces_quota_reason_instead_of_generic_retry(self):
        self.net.side_effect = refusal('retrieval')
        with patch.object(core, 'api', self.real_api):
            with self.assertRaises(quota.QuotaError) as first:
                core.search_items('synthetic',kind='episodes',method='agentic',budget=1,strict=True)
            self.assertFalse(first.exception.cached)
            with self.assertRaises(quota.QuotaError) as second:
                core.search_items('synthetic',kind='episodes',method='agentic',budget=1,strict=True)
            self.assertTrue(second.exception.cached)
        self.assertEqual(self.net.call_count,1)

    def test_app_suggestions_do_not_consume_prompt_retrieval_or_become_user_history(self):
        import hook
        from test_core_regressions import codex_message
        prompts = [
            'This is an app-generated request for a suggested next user message. synthetic context',
            '# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for a synthetic project',
        ]
        with patch.object(core, 'search_items') as search:
            for prompt in prompts:
                hook.on_prompt({'session_id':'synthetic-helper','prompt':prompt})
                self.assertEqual(core.messages_codex([codex_message('user',prompt)]),[])
        search.assert_not_called()
        self.assertFalse(core.RECALL_DIR.exists())
        # A real user's short instruction is still a prompt, including test-like text.
        self.assertEqual(len(core.messages_codex([codex_message('user','Reply with exactly READY.')])),1)
