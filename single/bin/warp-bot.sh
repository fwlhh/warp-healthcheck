#!/usr/bin/env bash
#
# warp-bot.sh — Telegram bot and healthcheck for a WARP container.
#
# Behaviour:
#   * Long-polls Telegram for commands.
#   * Only the configured chat id is answered; every other message is
#     silently ignored.
#   * Every CHECK_INTERVAL seconds, asks Google (through the WARP SOCKS5
#     proxy) which country it thinks we are in.
#   * If that country is RU, restarts the WARP docker-compose service.
#   * Every outgoing message is prefixed with NODE_NAME so multiple
#     nodes are distinguishable in a shared chat.
#
# Requires: curl, jq, docker.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

readonly TG_TOKEN="${TG_TOKEN:-PASTE_BOT_TOKEN_HERE}"
readonly TG_CHAT_ID="${TG_CHAT_ID:-0}"

# Human-readable name of this node. Shown in every Telegram message.
# Allowed: letters, digits, dot, underscore, dash.
readonly NODE_NAME="${NODE_NAME:-unknown-node}"

readonly WARP_PROXY="${WARP_PROXY:-127.0.0.1:1080}"
readonly COMPOSE_DIR="${COMPOSE_DIR:-/opt/warp}"
readonly COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
readonly COMPOSE_SERVICE="${COMPOSE_SERVICE:-warp}"

readonly CHECK_INTERVAL="${CHECK_INTERVAL:-300}"
readonly RESTART_COOLDOWN="${RESTART_COOLDOWN:-300}"
readonly POLL_TIMEOUT="${POLL_TIMEOUT:-25}"
readonly CURL_TIMEOUT="${CURL_TIMEOUT:-10}"

readonly STATE_DIR="${STATE_DIR:-/var/lib/warp-bot}"
readonly STATE_FILE="${STATE_DIR}/last-restart"
readonly OFFSET_FILE="${STATE_DIR}/offset"

readonly USER_AGENT='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'

readonly EXIT_OK=0
readonly EXIT_FAIL=1
readonly EXIT_COOLDOWN=2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log() {
  printf '[%s] [%s] %s\n' "$(date -Is)" "${NODE_NAME}" "$*" >&2
}

die() {
  log "FATAL: $*"
  exit "${EXIT_FAIL}"
}

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

require_config() {
  if [[ -z "${TG_TOKEN}" || "${TG_TOKEN}" == 'PASTE_BOT_TOKEN_HERE' ]]; then
    die 'TG_TOKEN is not configured'
  fi
  if [[ ! "${TG_CHAT_ID}" =~ ^[0-9]+$ || "${TG_CHAT_ID}" == '0' ]]; then
    die 'TG_CHAT_ID is not configured'
  fi
  if [[ ! "${NODE_NAME}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    die "NODE_NAME must match [a-zA-Z0-9._-]+, got: '${NODE_NAME}'"
  fi
  if [[ ! -f "${COMPOSE_DIR}/${COMPOSE_FILE}" ]]; then
    die "compose file not found: ${COMPOSE_DIR}/${COMPOSE_FILE}"
  fi
}

require_commands() {
  local cmd
  for cmd in curl jq docker; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      die "required command not found: ${cmd}"
    fi
  done
}

init_state() {
  mkdir -p "${STATE_DIR}"
  if [[ ! -f "${STATE_FILE}" ]]; then
    printf '0\n' >"${STATE_FILE}"
  fi
  if [[ ! -f "${OFFSET_FILE}" ]]; then
    printf '0\n' >"${OFFSET_FILE}"
  fi
}

# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

tg_api() {
  local method="$1"
  shift
  curl -fsS --max-time 30 \
    "https://api.telegram.org/bot${TG_TOKEN}/${method}" \
    "$@" 2>/dev/null || true
}

tg_send() {
  local text="$1"
  tg_api sendMessage \
    -d "chat_id=${TG_CHAT_ID}" \
    --data-urlencode "text=[<code>${NODE_NAME}</code>] ${text}" \
    -d 'parse_mode=HTML' \
    -d 'disable_web_page_preview=true' >/dev/null
}

# ---------------------------------------------------------------------------
# WARP helpers
# ---------------------------------------------------------------------------

warp_alive() {
  curl -fs --max-time 8 \
    --socks5-hostname "${WARP_PROXY}" \
    'https://cloudflare.com/cdn-cgi/trace' >/dev/null 2>&1
}

detect_google_country() {
  local opts=(
    -fsL
    --max-time "${CURL_TIMEOUT}"
    -A "${USER_AGENT}"
    --socks5-hostname "${WARP_PROXY}"
  )
  local resp country=''

  resp=$(curl "${opts[@]}" 'https://www.google.com' 2>/dev/null || true)
  country=$(sed -n 's/.*"[a-z]\{2\}_\([A-Z]\{2\}\)".*/\1/p' <<<"${resp}" | head -n1)

  if [[ -z "${country}" ]]; then
    resp=$(curl "${opts[@]}" 'https://play.google.com/' 2>/dev/null || true)
    country=$(grep -oP 'countryCode":"\K[A-Z]{2}' <<<"${resp}" | head -n1 || true)
  fi

  printf '%s' "${country}"
}

# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

read_last_restart() {
  local value
  value=$(<"${STATE_FILE}") || value=0
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    value=0
  fi
  printf '%s' "${value}"
}

restart_warp() {
  local reason="$1"
  local now last elapsed
  now=$(date +%s)
  last=$(read_last_restart)
  elapsed=$(( now - last ))

  if (( elapsed < RESTART_COOLDOWN )); then
    log "restart skipped: cooldown ${elapsed}s < ${RESTART_COOLDOWN}s"
    return "${EXIT_COOLDOWN}"
  fi

  local cmd=(docker compose -f "${COMPOSE_DIR}/${COMPOSE_FILE}" restart)
  if [[ -n "${COMPOSE_SERVICE}" ]]; then
    cmd+=("${COMPOSE_SERVICE}")
  fi

  log "restarting warp: reason='${reason}'"

  local output
  if output=$("${cmd[@]}" 2>&1); then
    printf '%s\n' "${now}" >"${STATE_FILE}"
    tg_send "✅ WARP restarted
Reason: <b>${reason}</b>
Service: <code>${COMPOSE_SERVICE:-all}</code>"
    log 'restart ok'
    return "${EXIT_OK}"
  fi

  local safe_output
  safe_output=$(printf '%s' "${output}" | head -c 1500 | sed 's/[<>&]//g')

  tg_send "❌ WARP restart failed
Reason: <b>${reason}</b>
<pre>${safe_output}</pre>"
  log "restart failed: ${output}"
  return "${EXIT_FAIL}"
}

# ---------------------------------------------------------------------------
# Scheduled healthcheck
# ---------------------------------------------------------------------------

last_check_ts=0

scheduled_check() {
  local now country rc
  now=$(date +%s)

  if (( now - last_check_ts < CHECK_INTERVAL )); then
    return 0
  fi
  last_check_ts=${now}

  if ! warp_alive; then
    log 'WARP proxy unreachable'
    return 0
  fi

  country=$(detect_google_country)
  if [[ -z "${country}" ]]; then
    log 'failed to detect Google country'
    tg_send '⚠️ Could not detect Google country through WARP'
    return 0
  fi

  log "check: Google=${country}"

  if [[ "${country}" == 'RU' ]]; then
    tg_send '🇷🇺 Google is detected as <b>RU</b> via WARP → restarting'
    rc=0
    restart_warp 'auto: Google=RU' || rc=$?
    if (( rc != EXIT_OK )); then
      log "auto restart returned ${rc}"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------

cmd_help() {
  tg_send "🤖 <b>WARP bot</b>

/status  — current Google country via WARP
/check   — run a check right now
/restart — restart WARP manually
/help    — this message"
}

cmd_status() {
  if ! warp_alive; then
    tg_send '⚠️ WARP SOCKS5 is unreachable'
    return 0
  fi

  local country
  country=$(detect_google_country)
  if [[ -z "${country}" ]]; then
    tg_send '⚠️ Could not detect Google country'
    return 0
  fi

  tg_send "🌍 Google via WARP: <b>${country}</b>"
}

cmd_check() {
  if ! warp_alive; then
    tg_send '⚠️ WARP SOCKS5 is unreachable'
    return 0
  fi

  local country rc
  country=$(detect_google_country)
  if [[ -z "${country}" ]]; then
    tg_send '⚠️ Could not detect Google country'
    return 0
  fi

  tg_send "🌍 Google via WARP: <b>${country}</b>"

  if [[ "${country}" == 'RU' ]]; then
    rc=0
    restart_warp 'manual /check: Google=RU' || rc=$?
    if (( rc == EXIT_COOLDOWN )); then
      tg_send '⏳ Restart skipped: cooldown active.'
    fi
  fi
}

cmd_restart() {
  local rc=0
  restart_warp 'manual /restart' || rc=$?
  if (( rc == EXIT_COOLDOWN )); then
    tg_send '⏳ Restart skipped: cooldown active.'
  fi
}

dispatch_command() {
  case "$1" in
    /start | /help) cmd_help ;;
    /status)        cmd_status ;;
    /check)         cmd_check ;;
    /restart)       cmd_restart ;;
    *)              : ;;
  esac
}

# ---------------------------------------------------------------------------
# Telegram update loop
# ---------------------------------------------------------------------------

handle_updates() {
  local offset
  offset=$(<"${OFFSET_FILE}")

  local updates
  updates=$(tg_api getUpdates \
    -d "timeout=${POLL_TIMEOUT}" \
    -d "offset=${offset}" \
    -d 'allowed_updates=["message"]')

  if [[ -z "${updates}" ]]; then
    return 0
  fi
  if ! jq -e '.ok == true' >/dev/null 2>&1 <<<"${updates}"; then
    return 0
  fi

  local count
  count=$(jq '.result | length' <<<"${updates}")
  if (( count == 0 )); then
    return 0
  fi

  local row uid text upd_id
  while IFS= read -r row; do
    uid=$(jq -r '.message.from.id // empty' <<<"${row}")
    text=$(jq -r '.message.text   // empty' <<<"${row}")
    upd_id=$(jq -r '.update_id' <<<"${row}")

    printf '%s\n' "$(( upd_id + 1 ))" >"${OFFSET_FILE}"

    if [[ "${uid}" == "${TG_CHAT_ID}" && -n "${text}" ]]; then
      dispatch_command "${text}" || log "handler error: ${text}"
    fi
  done < <(jq -c '.result[]' <<<"${updates}")
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

main() {
  require_config
  require_commands
  init_state

  log "bot started (node=${NODE_NAME}, chat_id=${TG_CHAT_ID}, interval=${CHECK_INTERVAL}s)"

  while true; do
    handle_updates
    scheduled_check
  done
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi