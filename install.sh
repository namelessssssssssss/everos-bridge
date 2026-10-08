#!/usr/bin/env bash
# EverOS Bridge 一键接线：在本机检测已安装的 agent，逐个接上 EverOS（改动前先备份原配置）。
#   ./install.sh            接线
#   ./install.sh --dry-run  只打印将做什么
# 兼容 macOS 自带 bash 3.2：不用关联数组，不展开可能为空的数组。
set -eu

REPO="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${EVEROS_HOME:-$HOME/.everos}"
MCP="$REPO/plugin/everos/mcp_server.py"
HOOK="$REPO/plugin/everos/hook.py"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
STAMP="$(date +%Y%m%d%H%M%S)"

say() { printf '%s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then say "  [dry-run] $*"; else "$@"; fi; }
has() { command -v "$1" >/dev/null 2>&1; }

command -v python3 >/dev/null 2>&1 || { say "需要 python3"; exit 1; }

# 1. 配置：环境变量优先；都没有就交互写入 $EVEROS_HOME/config.env（0600，永不入库）
say "== 配置（$HOME_DIR）"
if [ -f "$HOME_DIR/config.env" ] || [ -n "${EVEROS_API_KEY:-}" ]; then
  say "  已有配置，跳过"
elif [ "$DRY" = 1 ]; then
  say "  [dry-run] 将询问 user_id 和 API key 并写入 $HOME_DIR/config.env"
else
  mkdir -p "$HOME_DIR"
  printf 'EverOS user_id: '; read -r uid
  printf 'EverOS API key（输入不回显）: '; stty -echo; read -r key; stty echo; printf '\n'
  printf 'EverOS 邮箱前缀别名（botmux 用，可留空）: '; read -r aliases
  printf '官方是否还没把你的 v1 旧数据迁到 v2？还没迁完就输入 y，读取时会合并旧库 [y/N]: '; read -r v1
  case "$v1" in y|Y) readv1=1 ;; *) readv1=0 ;; esac
  umask 077
  printf 'EVEROS_API_BASE=https://api.evermind.ai\nEVEROS_USER_ID=%s\nEVEROS_API_KEY=%s\nEVEROS_OWNER_ALIASES=%s\nEVEROS_API_VERSION=v2\nEVEROS_READ_V1=%s\n' \
    "$uid" "$key" "$aliases" "$readv1" > "$HOME_DIR/config.env"
  say "  已写入 $HOME_DIR/config.env"
fi

# JSON 配置合并：mcpServers.everos（Claude Desktop / Cursor / Gemini 通用格式）
merge_mcp_json() {
  f="$1"
  [ -f "$f" ] || return 0
  run cp -p "$f" "$f.bak-everos-$STAMP"
  if [ "$DRY" = 1 ]; then say "  [dry-run] 在 $f 写入 mcpServers.everos"; return 0; fi
  python3 - "$f" "$MCP" <<'PY'
import json, sys
path, mcp = sys.argv[1], sys.argv[2]
raw = open(path).read().strip() or "{}"
data = json.loads(raw)
data.setdefault("mcpServers", {})["everos"] = {"command": "python3", "args": [mcp]}
open(path, "w").write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
PY
  say "  已写入 $f"
}

# 2. Claude Code：插件（hook + MCP + skill），并清掉旧的用户级直挂 hook，避免重复写入
if has claude; then
  say "== Claude Code"
  run claude plugin marketplace add "$REPO" || true
  run claude plugin install everos@everos-bridge --scope user || true
  S="$HOME/.claude/settings.json"
  if [ -f "$S" ] && grep -q 'everos_hook.py' "$S"; then
    run cp -p "$S" "$S.bak-everos-$STAMP"
    if [ "$DRY" = 0 ]; then
      python3 - "$S" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for ev, groups in list(d.get("hooks", {}).items()):
    keep = [g for g in groups if not any("everos_hook.py" in h.get("command", "") for h in g.get("hooks", []))]
    d["hooks"][ev] = keep
open(p, "w").write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
PY
      say "  已移除 settings.json 里旧的 everos_hook.py 直挂 hook（备份 .bak-everos-$STAMP）"
    fi
  fi
fi

# 3. Codex：插件（hook）+ MCP
if has codex; then
  say "== Codex"
  run codex plugin marketplace add "$REPO" || true
  run codex plugin add everos@everos-bridge || true   # Codex 是拷贝安装，仓库更新后重跑本脚本即可
  # MCP 用绝对路径注册在 config.toml（插件里的 MCP 声明只给 Claude Code，避免 Codex 读到未展开的变量）
  run codex mcp remove everos >/dev/null 2>&1 || true
  run codex mcp add everos -- python3 "$MCP" || true
  grep -q '^hooks *= *true' "$HOME/.codex/config.toml" 2>/dev/null || \
    say "  注意：Codex 的 hook 需要在 ~/.codex/config.toml 的 [features] 下设 hooks = true"
fi

# 4. TraeCode CLI（coco / traecli）：MCP
for bin in coco traecli; do
  if has "$bin"; then
    say "== $bin"
    if ! "$bin" mcp get everos >/dev/null 2>&1; then run "$bin" mcp add everos -- python3 "$MCP" || true; fi
    break
  fi
done

# 5. 用 mcpServers JSON 的客户端：Claude Desktop（含 Cowork）、Cursor、Gemini CLI
say "== 其他 MCP 客户端"
merge_mcp_json "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
merge_mcp_json "$HOME/.config/Claude/claude_desktop_config.json"
merge_mcp_json "$HOME/.cursor/mcp.json"
merge_mcp_json "$HOME/.gemini/settings.json"

# 6. Kimi Code：MCP（~/.kimi-code/mcp.json）+ config.toml 里的 [[hooks]]（自动读写）
if has kimi; then
  say "== Kimi Code"
  K="${KIMI_CODE_HOME:-$HOME/.kimi-code}"
  [ -f "$K/mcp.json" ] || { [ "$DRY" = 1 ] || { mkdir -p "$K"; printf '{}\n' > "$K/mcp.json"; }; }
  merge_mcp_json "$K/mcp.json"
  if [ -f "$K/config.toml" ] && ! grep -q 'everos/hook.py' "$K/config.toml"; then
    run cp -p "$K/config.toml" "$K/config.toml.bak-everos-$STAMP"
    if [ "$DRY" = 0 ]; then
      for spec in UserPromptSubmit:20 Stop:10 PreCompact:10 SessionEnd:5; do
        printf '\n[[hooks]]\nevent = "%s"\ncommand = "python3 %s"\ntimeout = %s\n' "${spec%%:*}" "$HOOK" "${spec##*:}" >> "$K/config.toml"
      done
      say "  已在 $K/config.toml 追加 4 条 [[hooks]]"
    else
      say "  [dry-run] 在 $K/config.toml 追加 UserPromptSubmit / Stop / PreCompact / SessionEnd 的 [[hooks]]"
    fi
  fi
  [ "$DRY" = 1 ] || kimi doctor 2>&1 | tail -2 | sed 's/^/  /'
fi

say "== 其他 MCP 客户端可手动添加"
say "  python3 $MCP"

# 7. 自检
say "== 自检"
python3 "$HOOK" --doctor || true
say "完成。Claude Code、Codex 在新会话生效；Claude Desktop 需要重启。"
