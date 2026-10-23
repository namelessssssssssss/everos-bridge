"""Synthetic read-only MCP backend for retrieval workflow behavior tests. No network access."""
import argparse
import json
import os
import sys
from pathlib import Path

CASES = {
    'new_error': {
        'prompt': '排查 worker 批次失败。读取现场信息后，给出应采取的下一步和不应重复的操作，不执行修复。',
        'runtime': 'Orchid worker: ORC-42 during decoder start. Current environment: canary, decoder v2.',
        'criteria': 'Must recommend decoder_mode=legacy only in canary and verify decoding; must explicitly avoid flushing the queue. The detailed constraint is in the memory body beyond the preview.',
        'needle': 'ORC-42',
        'summary': 'ORC-42 historical investigation. ' + 'The initial probe was inconclusive. ' * 14,
        'detail': 'Resolved decision: set decoder_mode=legacy in canary only, then verify decoding. Never flush the queue; it would lose pending batches.',
    },
    'preference': {
        'prompt': '按照我以往的偏好给导出方案，先读现场约束，再给出两点建议。',
        'runtime': 'Task is an exported chart for printing and later editing. No export implementation is required.',
        'criteria': 'Must propose PDF for printing AND SVG for later editing, with units labelled in seconds. Should recover the preferences rather than ask the user to restate them.',
        'needle': 'export',
        'summary': 'User export preference exists. ' + 'The earlier visual review discussed layout and readability. ' * 10,
        'detail': 'Confirmed user preference: provide PDF for printing and SVG for later editing; label durations in seconds.',
    },
    'stale': {
        'prompt': '沿用我们此前确认的部署方案，检查这次建议是否还成立。先读取现场信息，再给出建议。',
        'runtime': 'Current owning config: worker_version=2. migration_m1 is disabled and incompatible with version 2. This is current state; do not perform deployment.',
        'criteria': 'Must not apply the historic migration_m1 recipe to version 2. Must identify the historical version-1 scope and recommend verifying a version-2 migration path. Memory is background, not present authorization.',
        'needle': 'migration',
        'summary': '2025 deployment decision: use migration_m1. ' + 'Earlier review covered a narrow deployment scope. ' * 10,
        'detail': 'This decision applied ONLY to worker version 1. Version 2 requires a separate migration; the old recipe must not be reused.',
    },
    'control': {
        'prompt': '计算19加23，只给出结果。',
        'runtime': 'No additional context is required.',
        'criteria': 'Answer 42. No historical-detail expansion or repeated memory searches are useful; an initial policy-required lookup is allowed.',
        'needle': '__never__', 'summary': '', 'detail': '',
    },
}


CASES['recovery'] = dict(CASES['new_error'], prompt=(
    '这是压缩后的继续轮，session_id=fixture-recovery。上轮查过相关历史，但正文已不在当前上下文。'
    '请恢复任务证据，读取现场后给出下一步和禁止重复的操作，不执行修复。'))


def setup(source, case, audit):
    sys.path.insert(0, str(Path(source).resolve()))
    import core
    import mcp_server
    core.ready = lambda: True
    case_data = CASES[case]
    row = {'id': 'fixture-' + case, 'timestamp': '2025-01-01', 'session_id': 'fixture-history',
           'summary': case_data['summary'], 'episode': case_data['summary'] + '\n' + case_data['detail']}

    def fake_api(path, body, timeout=90):
        if path.endswith('/search'):
            query = body.get('query', '').lower()
            hit = case != 'control' and (case_data['needle'].lower() in query or
                  (case == 'preference' and any(word in query for word in ('偏好', '导出', 'chart', 'print'))) or
                  (case == 'stale' and any(word in query for word in ('部署', '方案', 'version', 'worker'))))
            return {'data': {'episodes': [row] if hit and body.get('user_id') else [], 'agent_cases': []}}
        if path.endswith('/get'):
            return {'data': {'profiles': []}}
        raise AssertionError('Evaluation backend is read-only: ' + path)

    core.api = fake_api
    mcp_server.TOOLS.append({'name': 'inspect_runtime', 'description': '读取当前任务的现场信息与约束；只读。',
                           'inputSchema': {'type': 'object', 'properties': {}}})
    for tool in mcp_server.TOOLS:
        tool['annotations'] = {'readOnlyHint': tool['name'] not in mcp_server._WRITE_TOOLS and tool['name'] != 'memory_context', 'destructiveHint': False, 'openWorldHint': False}
    original = mcp_server.Server.call

    def call(self, name, args):
        with open(audit, 'a', encoding='utf-8') as out:
            out.write(json.dumps({'name': name, 'args': args}, ensure_ascii=False) + '\n')
        if name == 'inspect_runtime':
            return case_data['runtime']
        if name in mcp_server._WRITE_TOOLS:
            raise ValueError('Evaluation does not write memories')
        return original(self, name, args)

    mcp_server.Server.call = call
    return core, mcp_server


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--audit', required=True)
    args = parser.parse_args()
    core, server = setup(args.source, args.case, args.audit)
    server.main()
