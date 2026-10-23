"""Run synthetic Codex behavior comparisons; all trajectories stay outside the repository.

Uses the configured/default Codex model and an existing auth file without copying its contents.
The fixture MCP cannot reach EverOS or write memories. No subtask implementation is delegated.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from fixture_server import CASES

ROOT = Path(__file__).resolve().parents[2]
POLICY = ('初始记忆仅覆盖对应问题。制定方案、新线索、异常、历史决策依赖或追问前有历史信息缺口时主动补查；'
          '摘要不足要展开，不把记忆当成当前状态或当前授权；证据已覆盖时复用。每条消息首次检索使用agentic。')
ARMS = ('auto', 'policy', 'workflow', 'proactive')


def run_trial(root, baseline, case, arm, codex, auth, timeout):
    run = root / (case + '-' + arm)
    run.mkdir()
    codex_home = run / 'codex-home'
    codex_home.mkdir()
    (codex_home / 'auth.json').symlink_to(auth)
    state = run / 'memory'
    state.mkdir()
    source = root / 'candidate/plugin/everos' if arm in ('workflow', 'proactive') else baseline / 'plugin/everos'
    audit = run / 'calls.jsonl'
    config = '\n'.join([
        '[mcp_servers.everos]', 'command = ' + json.dumps(sys.executable),
        'args = ' + json.dumps([str(ROOT / 'scripts/eval/fixture_server.py'), '--source', str(source),
                               '--case', case, '--audit', str(audit)]),
        'tool_timeout_sec = 30', 'default_tools_approval_mode = "writes"',
        '[mcp_servers.everos.tools.memory_context]', 'approval_mode = "approve"',
        '[mcp_servers.everos.env]', 'EVEROS_HOME = ' + json.dumps(str(state)),
        'EVEROS_USER_ID = "fixture-owner"', 'EVEROS_API_KEY = "fixture-key"',
        'EVEROS_READ_V1 = "0"', 'EVEROS_WRITE = "0"',
    ])
    (codex_home / 'config.toml').write_text(config)
    prompt = CASES[case]['prompt']
    if arm != 'auto':
        prompt += '\n\n协作规则：' + POLICY
    if arm != 'proactive' and case in ('preference', 'stale'):
        prompt += '\n\n【初始记忆摘要】' + CASES[case]['summary'][:260]
    if arm == 'workflow':
        prompt += '\n\n任务阶段提示：读到新的错误码后，现有摘要可能没有覆盖该问题，按新线索判断是否需补查。'
    prompt += '\n\n这是只读任务。不要写记忆或修改文件，不访问网络。现场信息通过inspect_runtime获取。'
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    # Never inherit a real EverOS backend or user-space config into a fixture process.
    for key in list(env):
        if key.startswith('EVEROS_'):
            del env[key]
    command = [codex, 'exec', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
               '--disable', 'hooks', '-s', 'read-only', '-C', str(run), '--json',
               '-o', str(run / 'answer.txt'), '-']
    start = time.monotonic()
    try:
        result = subprocess.run(command, input=prompt, text=True, env=env, capture_output=True, timeout=timeout)
        (run / 'events.jsonl').write_text(result.stdout)
        (run / 'stderr.txt').write_text(result.stderr)
        status = 'completed' if result.returncode == 0 else 'runtime_error'
        if 'requires approval' in result.stdout or 'MCP startup failed' in result.stderr:
            status = 'infrastructure_failure'
    except subprocess.TimeoutExpired as exc:
        (run / 'events.jsonl').write_text((exc.stdout or b'').decode() if isinstance(exc.stdout, bytes) else exc.stdout or '')
        status = 'timeout'
    shutil.rmtree(codex_home)
    calls = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
    answer = (run / 'answer.txt').read_text() if (run / 'answer.txt').exists() else ''
    return {'case': case, 'arm': arm, 'status': status, 'seconds': round(time.monotonic()-start, 2),
            'answer': answer, 'calls': calls, 'criteria': CASES[case]['criteria'],
            'answer_sha256': hashlib.sha256(answer.encode()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=list(CASES))
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS))
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--baseline', default='origin/main')
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if ROOT == output or ROOT in output.parents:
        parser.error('Save actual trajectories outside the source repository')
    output.mkdir(parents=True, exist_ok=True)
    auth = Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'auth.json'
    if not auth.is_file():
        parser.error('Existing Codex auth.json is required; authenticate normally before evaluation')
    codex = shutil.which('codex')
    if not codex:
        parser.error('codex CLI is required')
    baseline = output / 'baseline'
    baseline.mkdir(exist_ok=True)
    archive = subprocess.check_output(['git', 'archive', args.baseline, 'plugin/everos'], cwd=ROOT)
    subprocess.run(['tar', '-x', '-C', str(baseline)], input=archive, check=True)
    candidate = output / 'candidate/plugin/everos'
    shutil.copytree(ROOT / 'plugin/everos', candidate, ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
    fingerprint = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(candidate.glob('*.py'))}
    metadata = {'candidate_files_sha256': fingerprint, 'baseline': subprocess.check_output(['git', 'rev-parse', args.baseline], cwd=ROOT, text=True).strip(),
                'candidate': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'codex_version': subprocess.check_output([codex, '--version'], text=True).strip(),
                'model': 'configured CLI default, no per-arm override', 'synthetic_only': True}
    (output / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    rows = []
    for case in args.cases:
        for arm in args.arms:
            row = run_trial(output, baseline, case, arm, codex, auth.resolve(), args.timeout)
            rows.append(row)
            (output / 'results.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(json.dumps({key: row[key] for key in ('case', 'arm', 'status', 'seconds')}) , flush=True)
    return 0 if all(row['status'] == 'completed' for row in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
