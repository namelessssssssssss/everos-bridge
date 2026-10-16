#!/usr/bin/env bash
# Health check for the EverOS Doubao Work integration.
# Resolves the bridge root from this symlink/file; works from any clone location.
set -u
SELF="$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")"
# Typical location after install: <repo>/plugin/doubao/check.sh  (synced into the user skill)
ROOT="$(cd "$(dirname "$SELF")/../.." 2>/dev/null && pwd)"
[ -f "$ROOT/plugin/everos/hook.py" ] || ROOT="${EVEROS_BRIDGE:-$HOME/.everos-bridge}"
fail=0
ok()  { printf 'OK    %s\n' "$1"; }
bad() { printf 'MISS  %s  -> %s\n' "$1" "$2"; fail=1; }

for f in plugin/everos/hook.py plugin/everos/mcp_server.py plugin/doubao/doubao.py plugin/doubao/mcp_call.py \
         bin/everos-doubao-ingest bin/everos-mcp; do
  [ -f "$ROOT/$f" ] && ok "仓库文件 $f" || bad "仓库文件 $f" "在 $ROOT 重跑 git pull 或重新 clone"
done
[ -f "$HOME/.everos/config.env" ] && ok "~/.everos/config.env" \
  || bad "~/.everos/config.env" "缺少 EVEROS_USER_ID / EVEROS_API_KEY；见 docs/doubao-integration.md"
for c in everos-doubao-ingest everos-mcp; do
  command -v "$c" >/dev/null 2>&1 && ok "命令 $c" || bad "命令 $c" "重跑 install.sh（会在 ~/.local/bin 建符号链接）"
done
if [ "$fail" = 0 ]; then
  if timeout 25 python3 "$ROOT/plugin/everos/hook.py" --doctor >/tmp/everos-doctor.txt 2>&1; then
    ok "EverOS API doctor"
  else
    bad "EverOS API doctor" "见 /tmp/everos-doctor.txt（网络、key 或服务问题）"
  fi
fi
exit "$fail"
