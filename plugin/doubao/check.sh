#!/usr/bin/env bash
# Verify the runtime actually selected by the installed CLI.
set -eu
CLI="$(command -v everos-mcp || true)"
[ -n "$CLI" ] || { printf 'MISS everos-mcp\n'; exit 1; }
REAL="$(readlink -f "$CLI")"
ROOT="$(dirname "$(dirname "$REAL")")"
[ -f "$ROOT/plugin/doubao/doubao.py" ] || { printf 'MISS current CLI is not the repository runtime\n'; exit 1; }
for f in plugin/everos/hook.py plugin/everos/mcp_server.py plugin/doubao/doubao.py plugin/doubao/mcp_call.py bin/everos-doubao-ingest bin/everos-mcp; do
  [ -f "$ROOT/$f" ] || { printf 'MISS %s\n' "$f"; exit 1; }
done
INGEST="$(command -v everos-doubao-ingest || true)"
[ -n "$INGEST" ] && [ "$(readlink -f "$INGEST")" = "$ROOT/bin/everos-doubao-ingest" ] || { printf 'MISS mixed CLI runtime\n'; exit 1; }
printf 'OK runtime files and CLI targets\n'
TOOLS="$($CLI list)"
for name in memory_rules memory_profile memory_search memory_recent memory_open memory_context memory_remember memory_record_practice memory_flush memory_fix_profile; do
  present=0
  while IFS="$(printf '\t')" read -r tool description; do
    [ "$tool" != "$name" ] || present=1
  done <<EOF
$TOOLS
EOF
  [ "$present" -eq 1 ] || { printf 'MISS %s\n' "$name"; exit 1; }
done
printf 'OK required memory tools\n'
python3 - "$ROOT" <<'PY'
import stat, sys
sys.path.insert(0, sys.argv[1] + '/plugin/everos')
import core
p = core.HOME / 'config.env'
if p.is_file() and stat.S_IMODE(p.stat().st_mode) != 0o600:
    raise SystemExit('MISS config permission is not 0600')
report, healthy = core.doctor_result()
for line in report.splitlines():
    if not line.startswith('user_id'):
        print(line)
raise SystemExit(0 if healthy else 1)
PY
