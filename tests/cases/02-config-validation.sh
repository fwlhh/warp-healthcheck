#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

# Case 1: empty TG_TOKEN
(
  export TG_TOKEN=""
  export TG_CHAT_ID="123"
  export NODE_NAME="test-node"
  export COMPOSE_DIR="/tmp"
  export COMPOSE_FILE="does-not-exist.yml"
  # shellcheck disable=SC1090
  source "${SCRIPT}"
  if require_config 2>/dev/null; then
    printf 'expected failure for empty TG_TOKEN\n' >&2
    exit 1
  fi
)

# Case 2: Invalid NODE_NAME
(
  tmp_compose="$(mktemp -d)"
  trap 'rm -rf "${tmp_compose}"' EXIT
  printf 'version: "3"\n' > "${tmp_compose}/docker-compose.yml"

  export TG_TOKEN="test-token"
  export TG_CHAT_ID="123"
  export NODE_NAME="bad name with spaces"
  export COMPOSE_DIR="${tmp_compose}"
  export COMPOSE_FILE="docker-compose.yml"

  # shellcheck disable=SC1090
  source "${SCRIPT}"
  if require_config 2>/dev/null; then
    printf 'expected failure for bad NODE_NAME\n' >&2
    exit 1
  fi
)

exit 0