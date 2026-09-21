#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

tmp_bin="$(mktemp -d)"
tmp_state="$(mktemp -d)"
tmp_compose="$(mktemp -d)"
mock_log="$(mktemp)"
trap 'rm -rf "${tmp_bin}" "${tmp_state}" "${tmp_compose}" "${mock_log}"' EXIT

# Docker mock: logs calls.
cat > "${tmp_bin}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${MOCK_DOCKER_LOG}"
exit 0
EOF
chmod +x "${tmp_bin}/docker"

export PATH="${tmp_bin}:${PATH}"
export MOCK_DOCKER_LOG="${mock_log}"

printf 'version: "3"\n' > "${tmp_compose}/docker-compose.yml"

export TG_TOKEN="test-token"
export TG_CHAT_ID="123"
export NODE_NAME="test-node"
export COMPOSE_DIR="${tmp_compose}"
export COMPOSE_FILE="docker-compose.yml"
export STATE_DIR="${tmp_state}"
export STATE_FILE="${tmp_state}/last-restart"
export OFFSET_FILE="${tmp_state}/offset"

# shellcheck disable=SC1090
source "${SCRIPT}"

init_state

# The first restart should go through.
restart_warp "test-1" || true
if [[ ! -s "${MOCK_DOCKER_LOG}" ]]; then
  printf 'expected docker to be called on first restart\n' >&2
  exit 1
fi

# A second immediate restart should hit the cooldown.
: > "${MOCK_DOCKER_LOG}"
rc=0
restart_warp "test-2" || rc=$?
if (( rc != EXIT_COOLDOWN )); then
  printf 'expected EXIT_COOLDOWN, got %d\n' "${rc}" >&2
  exit 1
fi
if [[ -s "${MOCK_DOCKER_LOG}" ]]; then
  printf 'docker should not be called during cooldown\n' >&2
  exit 1
fi

exit 0