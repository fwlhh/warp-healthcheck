#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

tmp_compose="$(mktemp -d)"
trap 'rm -rf "${tmp_compose}"' EXIT
printf 'version: "3"\n' > "${tmp_compose}/docker-compose.yml"

export TG_TOKEN="test-token"
export TG_CHAT_ID="123"
export NODE_NAME="test-node"
export COMPOSE_DIR="${tmp_compose}"
export COMPOSE_FILE="docker-compose.yml"

# shellcheck disable=SC1090
source "${SCRIPT}"

for fn in detect_google_country restart_warp warp_alive require_config require_commands init_state; do
  if ! declare -F "${fn}" >/dev/null; then
    printf 'missing function: %s\n' "${fn}" >&2
    exit 1
  fi
done

exit 0