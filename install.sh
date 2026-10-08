#!/usr/bin/env bash
# macOS bash 3.2 compatible entry point. All config changes use the same helper.
set -eu
REPO="$(cd "$(dirname "$0")" && pwd)"
command -v python3 >/dev/null 2>&1 || { printf '%s\n' '需要 python3 >= 3.9'; exit 1; }
exec python3 "$REPO/scripts/install_config.py" "$@"
