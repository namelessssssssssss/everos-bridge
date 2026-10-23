#!/usr/bin/env bash
# Claude Code 云端环境的 setup script：把 everos-bridge 装到 /opt/everos-bridge。
# 私有仓库需要在环境变量里给一个只读 GH_TOKEN（fine-grained，只授权本仓库）。
# 失败也 exit 0：记忆接线不值得挡住会话启动。
REPO="${EVEROS_BRIDGE_REPO:-namelessssssssssss/everos-bridge}"
URL="https://github.com/$REPO.git"
[ -n "${GH_TOKEN:-}" ] && URL="https://x-access-token:${GH_TOKEN}@github.com/$REPO.git"
rm -rf /opt/everos-bridge
git clone --depth 1 "$URL" /opt/everos-bridge >/dev/null 2>&1 || echo "everos-bridge: clone failed, memory disabled"
if [ -f /opt/everos-bridge/install.sh ]; then
    # The cloud image may not expose the CLI binary during setup yet.
    python3 /opt/everos-bridge/scripts/install_config.py --rules-only --rules-host claude || echo "everos-bridge: native rules sync incomplete"
fi
exit 0
