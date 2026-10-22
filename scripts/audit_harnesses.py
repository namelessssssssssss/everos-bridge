"""Read configured EverOS entries and exercise their actual stdio programs with local synthetic data.

This checks configured adapters, not an already-running app's cached tool catalog or model behavior.
No credentials or arbitrary environment fields are printed. --exercise never contacts real EverOS.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_config import Installer

EXPECTED = {'memory_profile', 'memory_search', 'memory_open', 'memory_context', 'memory_recent',
            'memory_remember', 'memory_record_practice', 'memory_fix_profile'}


def json_entry(path, kind='mcpServers'):
    data = json.loads(Installer.strip_jsonc(path.read_text()))
    entry = (data.get(kind) or {}).get('everos') or {}
    command = entry.get('command')
    return command if isinstance(command, list) else [command] + entry.get('args', [])


def toml_entry(path):
    text = path.read_text()
    table = re.search(r'(?m)^\[\s*mcp_servers\.(?:everos|"everos")\s*\]\s*\n([\s\S]*?)(?=^\[|\Z)', text)
    if not table:
        return []
    values = {}
    for key in ('command', 'args'):
        match = re.search(r'(?m)^\s*' + key + r'\s*=\s*(.+)$', table[1])
        if match:
            values[key] = ast.literal_eval(match[1])
    return [values['command']] + values.get('args', []) if values.get('command') else []


def root_binding(path, kind):
    if kind == 'toml':
        text = path.read_text()
        section = re.search(r'(?m)^\[\s*mcp_servers\.(?:everos|"everos")\.env\s*\]\s*\n([\s\S]*?)(?=^\[|\Z)', text)
        match = re.search(r'(?m)^\s*EVEROS_HOME\s*=\s*(.+)$', section[1]) if section else None
        return ast.literal_eval(match[1]) if match else None
    data = json.loads(Installer.strip_jsonc(path.read_text()))
    entry = (data.get(kind) or {}).get('everos') or {}
    return (entry.get('env') or entry.get('environment') or {}).get('EVEROS_HOME')


def entries(home):
    paths = [
        ('codex', home/'.codex/config.toml', 'toml'),
        ('trae', home/'.trae/traecli.toml', 'toml'),
        ('kimi', home/'.kimi-code/mcp.json', 'mcpServers'),
        ('kimi_desktop', home/'Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/mcp.json', 'mcpServers'),
        ('mimocode', home/'.config/mimocode/mimocode.jsonc', 'mcp'),
        ('claude_desktop', home/'Library/Application Support/Claude/claude_desktop_config.json', 'mcpServers'),
        ('claude_desktop_linux', home/'.config/Claude/claude_desktop_config.json', 'mcpServers'),
        ('cursor', home/'.cursor/mcp.json', 'mcpServers'),
        ('gemini', home/'.gemini/settings.json', 'mcpServers'),
    ]
    result = []
    for host, path, kind in paths:
        try:
            command = toml_entry(path) if kind == 'toml' else json_entry(path, kind)
            result.append({'host': host, 'config': str(path), 'command': command, 'state_root': root_binding(path,kind)})
        except FileNotFoundError:
            result.append({'host': host, 'status': 'not_configured', 'config': str(path)})
        except (ValueError, KeyError, SyntaxError):
            result.append({'host': host, 'status': 'unsupported_config_shape', 'config': str(path)})
    installed = home/'.claude/plugins/installed_plugins.json'
    if installed.exists():
        data = json.loads(installed.read_text())
        for row in (data.get('plugins') or {}).get('everos@everos-bridge') or []:
            root = Path(row['installPath'])
            config = root/'claude-mcp.json'
            command = [str(x).replace('${CLAUDE_PLUGIN_ROOT}', str(root)) for x in json_entry(config)]
            result.append({'host': 'claude', 'config': str(config), 'command': command})
    return result


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if not self.path.endswith(('/search', '/get')):
            self.send_response(403); self.end_headers(); return
        row = {'id': 'fixture', 'summary': 'Harness expansion fixture. ' + 'Preview only. ' * 30,
               'episode': 'HARNESS_EXPANSION_OK', 'timestamp': '2026-01-01'}
        data = {'episodes': [row] if body.get('user_id') and self.path.endswith('/search') else [],
                'agent_cases': [], 'profiles': []}
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps({'data': data}).encode())


class Client:
    def __init__(self, command, env, cwd):
        self.proc = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.mid = 0

    def call(self, method, params=None):
        self.mid += 1
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.mid, 'method': method,
                                         'params': params or {}})+'\n')
        self.proc.stdin.flush()
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            if not self.selector.select(max(0, deadline-time.monotonic())):
                break
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError('stdio_server_exited')
            data = json.loads(line)
            if data.get('id') == self.mid:
                if 'error' in data:
                    raise RuntimeError('rpc_error')
                return data['result']
        raise RuntimeError('stdio_timeout')

    def tool(self, name, arguments):
        result = self.call('tools/call', {'name': name, 'arguments': arguments})
        if result.get('isError'):
            raise RuntimeError('tool_error_' + name)
        return '\n'.join(block.get('text', '') for block in result.get('content', []) if block.get('type')=='text')

    def close(self):
        self.selector.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill(); self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


def probe(entry):
    command = entry.get('command') or []
    if len(command) < 2 or not str(command[1]).endswith('/everos/mcp_server.py'):
        return dict(entry, status=entry.get('status', 'unsupported_entrypoint'))
    with tempfile.TemporaryDirectory(prefix='everos-adapter-audit-') as tmp:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = dict(os.environ, EVEROS_HOME=tmp, EVEROS_USER_ID='audit-owner', EVEROS_API_KEY='fixture-key',
                   EVEROS_AGENT_ID='audit-agent', EVEROS_API_VERSION='v2', EVEROS_READ_V1='0', EVEROS_WRITE='0',
                   EVEROS_API_BASE=f'http://127.0.0.1:{server.server_port}')
        client = None
        try:
            client = Client(command, env, tmp)
            info = client.call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                             'clientInfo': {'name': entry['host'], 'version': 'adapter-audit'}})
            names = {x['name'] for x in client.call('tools/list')['tools']}
            if not EXPECTED <= names:
                return dict(entry, status='missing_tools', version=info['serverInfo']['version'], missing=sorted(EXPECTED-names))
            text = client.tool('memory_search', {'query': 'harness fixture', 'method': 'agentic'})
            ref = re.search(r'id=(em_[a-f0-9]{40})', text).group(1)
            opened = json.loads(client.tool('memory_open', {'memory_id': ref, 'limit': 12000}))
            context = json.loads(client.tool('memory_context', {'session_id': 'audit', 'stage': 'recover'}))
            valid = 'HARNESS_EXPANSION_OK' in opened['text'] and context['generation']==1
            return dict(entry, status='adapter_passed' if valid else 'adapter_failed',
                        version=info['serverInfo']['version'], tool_count=len(names),
                        native_client_verified=False)
        except Exception as error:
            return dict(entry, status='adapter_failed', error=type(error).__name__+':'+str(error)[:80])
        finally:
            if client:
                client.close()
            server.shutdown(); server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--exercise', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    rows = entries(args.home)
    if args.exercise:
        rows = [probe(row) for row in rows]
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text+'\n')
    print(text)
    return 1 if any(row.get('status') in ('adapter_failed','missing_tools') for row in rows) else 0


if __name__ == '__main__':
    sys.exit(main())
