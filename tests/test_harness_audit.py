"""Probe configured stdio entrypoints, separately from native-client verification."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('harness_audit', ROOT/'scripts/audit_harnesses.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class HarnessAuditTests(unittest.TestCase):
    def test_probe_uses_real_server_and_local_fixture_without_claiming_native_client(self):
        row = audit.probe({'host':'fixture-client', 'command':[sys.executable, str(ROOT/'plugin/everos/mcp_server.py')]})
        self.assertEqual(row['status'], 'adapter_passed', row)
        self.assertEqual(row['tool_count'], 9)
        self.assertFalse(row['native_client_verified'])

    def test_inventory_does_not_publish_config_secrets_or_treat_missing_config_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            folder = home/'.kimi-code'
            folder.mkdir()
            (folder/'mcp.json').write_text(json.dumps({'mcpServers':{'everos':{
                'command':'python3','args':['/fixture/everos/mcp_server.py'],
                'env':{'PRIVATE_VALUE':'not-to-be-reported'}}}}))
            rows = audit.entries(home)
            self.assertNotIn('not-to-be-reported', json.dumps(rows))
            cursor = next(row for row in rows if row['host']=='cursor')
            self.assertEqual(cursor['status'], 'not_configured')

    @unittest.skipIf(os.name == 'nt', 'Unix cloud Bash hook template')
    def test_cloud_hook_template_and_mcp_share_expandable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = audit.ThreadingHTTPServer(('127.0.0.1', 0), audit.Fixture)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            env = dict(os.environ, EVEROS_HOME=tmp, EVEROS_USER_ID='cloud-fixture',
                       EVEROS_API_KEY='fixture-key', EVEROS_API_VERSION='v2', EVEROS_READ_V1='0',
                       EVEROS_WRITE='0', EVEROS_INJECT='1', CLAUDE_CODE_REMOTE='true',
                       EVEROS_API_BASE=f'http://127.0.0.1:{server.server_port}')
            client = None
            try:
                template = json.loads((ROOT/'cloud/settings.snippet.json').read_text())
                command = template['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
                command = command.replace('/opt/everos-bridge', shlex.quote(str(ROOT)))
                payload = json.dumps({'session_id':'cloud-fixture','hook_event_name':'UserPromptSubmit',
                                      'prompt':'cloud harness fixture'})
                result = subprocess.run(['bash','-c',command], env=env, input=payload, text=True,
                                        capture_output=True, timeout=5)
                context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
                ref = re.search(r'em_[0-9a-f]{40}', context).group(0)
                client = audit.Client([sys.executable,str(ROOT/'plugin/everos/mcp_server.py')],env,tmp)
                client.call('initialize',{'protocolVersion':'2025-06-18'})
                opened = json.loads(client.tool('memory_open',{'memory_id':ref}))
                self.assertIn('HARNESS_EXPANSION_OK', opened['text'])
                disabled = subprocess.run(['bash','-c',command], env=dict(env,CLAUDE_CODE_REMOTE='false'),
                                          input=payload,text=True,capture_output=True,timeout=5)
                self.assertEqual(disabled.stdout,'')
            finally:
                if client:
                    client.close()
                server.shutdown();server.server_close()
