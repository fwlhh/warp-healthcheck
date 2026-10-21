#!/usr/bin/env bash
# Regression test: when Telegram is unreachable the main loop used to spin at
# hundreds of curl calls per second because tg_api swallows errors and
# handle_updates returned 0 on a failed poll.
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin" "${work}/compose" "${work}/state"
printf 'version: "3"\n' > "${work}/compose/docker-compose.yml"

# Every curl call fails instantly (DNS failure / token rejected) and is counted.
cat > "${work}/bin/curl" <<EOF
#!/usr/bin/env bash
echo x >> "${work}/curl.count"
exit 6
EOF
# main() only needs these to exist; the failing-poll path never reaches jq.
printf '#!/usr/bin/env bash\nexit 0\n' > "${work}/bin/jq"
printf '#!/usr/bin/env bash\nexit 0\n' > "${work}/bin/docker"
chmod +x "${work}/bin/"*

export TG_TOKEN="test-token" TG_CHAT_ID="123" NODE_NAME="test-node"
export COMPOSE_DIR="${work}/compose" COMPOSE_FILE="docker-compose.yml"
export STATE_DIR="${work}/state"

# --- Case 1: backoff progression is 5, 10, 20, 40, 60, 60 ------------------
(
  PATH="${work}/bin:${PATH}"
  # shellcheck disable=SC1090
  source "${SCRIPT}"
  cur=0 seq=''
  for _ in 1 2 3 4 5 6; do
    cur="$(next_backoff "${cur}")"
    seq+="${cur} "
  done
  if [[ "${seq}" != '5 10 20 40 60 60 ' ]]; then
    printf 'unexpected backoff sequence: %s\n' "${seq}" >&2
    exit 1
  fi
)

# --- Case 2: a failed poll is reported to the caller ------------------------
(
  PATH="${work}/bin:${PATH}"
  # shellcheck disable=SC1090
  source "${SCRIPT}"
  mkdir -p "${STATE_DIR}"
  printf '0\n' > "${STATE_DIR}/offset"
  if handle_updates; then
    printf 'handle_updates must fail when the poll fails\n' >&2
    exit 1
  fi
)

# --- Case 3: the main loop does not spin while Telegram is down -------------
(
  PATH="${work}/bin:${PATH}"
  export POLL_BACKOFF_MIN=1 POLL_BACKOFF_MAX=1
  : > "${work}/curl.count"
  timeout 4 bash "${SCRIPT}" >/dev/null 2>&1 || true
  calls="$(wc -l < "${work}/curl.count")"
  # With a 1s backoff: ~4 polls plus a few scheduled-check probes. The
  # unfixed loop made several hundred calls per second.
  if (( calls > 40 )); then
    printf 'main loop is spinning: %s curl calls in 4s\n' "${calls}" >&2
    exit 1
  fi
)

exit 0
