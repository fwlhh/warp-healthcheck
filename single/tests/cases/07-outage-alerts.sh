#!/usr/bin/env bash
# shellcheck disable=SC2317,SC2016
# SC2317: warp_alive & co. are stand-ins called by the sourced script.
# SC2016: the bash -c programs are meant to be single-quoted.
#
# A dead WARP proxy used to produce one log line and nothing else, and a broken
# country probe produced the same Telegram message every CHECK_INTERVAL for as
# long as it lasted. Now: one message per outage, one when it ends, and an
# alert that Telegram did not accept is retried.
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin" "${work}/compose" "${work}/state"
printf 'version: "3"\n' > "${work}/compose/docker-compose.yml"

# Mock curl: every call is one line in calls.log. With MOCK_TG_FAIL=1 it fails
# like an unreachable Telegram, otherwise it answers like the Bot API.
cat > "${work}/bin/curl" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${work}/calls.log"
if [[ "\${MOCK_TG_FAIL:-0}" == 1 ]]; then
  exit 22
fi
printf '{"ok":true,"result":{}}'
EOF
chmod +x "${work}/bin/curl"
: > "${work}/calls.log"

export PATH="${work}/bin:${PATH}"
export TG_TOKEN="test-token" TG_CHAT_ID="123" NODE_NAME="test-node"
export COMPOSE_DIR="${work}/compose" COMPOSE_FILE="docker-compose.yml"
export STATE_DIR="${work}/state" CHECK_INTERVAL=0

# shellcheck disable=SC1090
source "${SCRIPT}"

# Stand-ins for the probes; the cases flip these.
WARP_UP=1
COUNTRY=DE
warp_alive() { [[ "${WARP_UP}" == 1 ]]; }
detect_google_country() { printf '%s' "${COUNTRY}"; }
restart_warp() { :; }

messages() { wc -l < "${work}/calls.log" | tr -d ' '; }

expect_messages() {
  local label="$1" want="$2" got
  got="$(messages)"
  if [[ "${got}" != "${want}" ]]; then
    printf '%s: expected %s Telegram message(s) so far, got %s\n' "${label}" "${want}" "${got}" >&2
    cat "${work}/calls.log" >&2
    exit 1
  fi
}

check() { last_check_ts=0; scheduled_check; }

# Healthy: silence.
check
expect_messages 'healthy' 0

# --- WARP goes down: one message, however long it stays down ----------------
WARP_UP=0
check
expect_messages 'down: first check' 1
check; check; check
expect_messages 'down: repeated checks' 1

# --- WARP comes back: one recovery message ----------------------------------
WARP_UP=1
check
expect_messages 'recovery' 2
check; check
expect_messages 'recovery: repeated checks' 2

# --- Country probe breaks: once, not every interval -------------------------
COUNTRY=''
check; check; check
expect_messages 'detect failed' 3
COUNTRY=DE
check
expect_messages 'detect recovered' 4

# --- Telegram unreachable while WARP is down: not lost, retried -------------
WARP_UP=0
MOCK_TG_FAIL=1 check
if [[ -e "${ALERT_DIR}/warp_down" ]]; then
  printf 'marked as alerted although Telegram did not accept the message\n' >&2
  exit 1
fi
check                       # Telegram is back
expect_messages 'retry' 6   # one failed attempt (still logged by the mock) + the delivery
check
expect_messages 'retry: no repeat' 6

# --- The marker survives a restart of the bot: no second alert -------------
# A new process with the same STATE_DIR, WARP still down.
bash -c '
  source "$1" 2>/dev/null
  warp_alive() { return 1; }
  detect_google_country() { printf DE; }
  last_check_ts=0
  scheduled_check
' _ "${SCRIPT}" 2>/dev/null
expect_messages 'after bot restart' 6

# ... and the recovery after a restart is still reported.
bash -c '
  source "$1" 2>/dev/null
  warp_alive() { return 0; }
  detect_google_country() { printf DE; }
  last_check_ts=0
  scheduled_check
' _ "${SCRIPT}" 2>/dev/null
expect_messages 'recovery after bot restart' 7

exit 0
