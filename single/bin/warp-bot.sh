#!/usr/bin/env bash
#
# warp-bot.sh — Telegram bot and healthcheck for a WARP container (single mode).
#
# Behaviour:
#   * Long-polls Telegram for commands.
#   * Only the configured chat id is answered; every other message is
#     silently ignored.
#   * Every CHECK_INTERVAL seconds, asks Google (through the WARP SOCKS5
#     proxy) which country it thinks we are in.
#   * If that country is RU, restarts the WARP docker-compose service.
#   * After any restart (auto or manual) probes Google again and reports
#     the new country and WARP exit IP to Telegram.
#
# Requires: curl, jq, docker.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

readonly TG_TOKEN="${TG_TOKEN:-PASTE_BOT_TOKEN_HERE}"
readonly TG_CHAT_ID="${TG_CHAT_ID:-0}"
readonly NODE_NAME="${NODE_NAME:-unknown-node}"

readonly WARP_PROXY="${WARP_PROXY:-127.0.0.1:1080}"
readonly COMPOSE_DIR="${COMPOSE_DIR:-/opt/warp}"
readonly COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
readonly COMPOSE_SERVICE="${COMPOSE_SERVICE:-warp}"
readonly WARP_NAME="${WARP_NAME:-}"
# Countries in which Google refuses the service: Google seeing one of them as
# the exit's country restarts that WARP (space or comma separated). Set but
# empty means "none": monitor only. (${VAR-x}, not ${VAR:-x}, keeps it empty.)
readonly BAD_COUNTRIES="${BAD_COUNTRIES-RU}"

readonly CHECK_INTERVAL="${CHECK_INTERVAL:-300}"
readonly RESTART_COOLDOWN="${RESTART_COOLDOWN:-60}"
readonly POLL_TIMEOUT="${POLL_TIMEOUT:-25}"
readonly POLL_BACKOFF_MIN="${POLL_BACKOFF_MIN:-5}"
readonly POLL_BACKOFF_MAX="${POLL_BACKOFF_MAX:-60}"
readonly CURL_TIMEOUT="${CURL_TIMEOUT:-10}"
readonly WARP_BOOT_TIMEOUT="${WARP_BOOT_TIMEOUT:-45}"

readonly STATE_DIR="${STATE_DIR:-/var/lib/warp-bot}"
readonly TG_API_BASE="${TG_API_BASE:-https://api.telegram.org}"
readonly ALERT_DIR="${STATE_DIR}/alerts"
readonly STATE_FILE="${STATE_DIR}/last-restart"
readonly OFFSET_FILE="${STATE_DIR}/offset"

readonly USER_AGENT='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'
readonly GOOGLE_CONSENT_COOKIE='SOCS=CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjUwNzMwLjA1X3AwGgJlbiACGgYIgPC_xAY'

readonly EXIT_OK=0
readonly EXIT_FAIL=1
readonly EXIT_COOLDOWN=2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# " / name" of the WARP being handled, or nothing when the node has just one
# WARP with the default name. Set by select_warp.
CUR_WARP_TAG=''

log() {
  printf '[%s] [%s%s] %s\n' "$(date -Is)" "${NODE_NAME}" "${CUR_WARP_TAG}" "$*" >&2
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

# ---------------------------------------------------------------------------
# WARP instances
#
# WARP #1 is the long-standing WARP_PROXY / COMPOSE_SERVICE pair. Every further
# WARP is a group of variables with its number, discovered rather than counted
# (there is no limit):
#
#   WARP2_NAME=warp2            optional; defaults to the service name
#   WARP2_PROXY=127.0.0.1:1081  "127.0.0.1:1081:1080" (docker's ports form) works too
#   WARP2_SERVICE=warp2         the docker compose service to restart
#   WARP2_COMPOSE_DIR / WARP2_COMPOSE_FILE   optional
#
# The functions below work on the "current" WARP (CUR_*), chosen with
# select_warp, so probes and restarts are written once for any number of WARPs.
# ---------------------------------------------------------------------------

WARP_NAMES=()
WARP_PROXIES=()
WARP_SERVICES=()
WARP_DIRS=()
WARP_FILES=()
CUR_NAME='' CUR_PROXY='' CUR_SERVICE='' CUR_DIR='' CUR_FILE=''
CUR_STATE_FILE='' CUR_KEY_SUFFIX=''

# 127.0.0.1:1081:1080 (docker's host:published:container) -> 127.0.0.1:1081
normalize_proxy() {
  local value="$1" parts=()
  if [[ "${value}" != \[* ]]; then
    IFS=: read -ra parts <<<"${value}"
    if (( ${#parts[@]} == 3 )); then
      value="${parts[0]}:${parts[1]}"
    fi
  fi
  printf '%s' "${value}"
}

# Fills WARP_* from the environment. Dies on an inconsistent configuration.
load_warps() {
  local numbers=(1) var n i j
  while IFS= read -r var; do
    if [[ -n "${!var:-}" ]]; then
      n="${var#WARP}"
      numbers+=("${n%%_*}")
    fi
  done < <(compgen -A variable | grep -E '^WARP[0-9]+_(PROXY|SERVICE|NAME|COMPOSE_DIR|COMPOSE_FILE)$' || true)
  mapfile -t numbers < <(printf '%s\n' "${numbers[@]}" | sort -un)

  WARP_NAMES=() WARP_PROXIES=() WARP_SERVICES=() WARP_DIRS=() WARP_FILES=()
  local proxy service name dir file p_var s_var n_var d_var f_var
  for n in "${numbers[@]}"; do
    p_var="WARP${n}_PROXY" s_var="WARP${n}_SERVICE" n_var="WARP${n}_NAME"
    d_var="WARP${n}_COMPOSE_DIR" f_var="WARP${n}_COMPOSE_FILE"
    proxy="${!p_var:-}" service="${!s_var:-}" name="${!n_var:-}"
    dir="${!d_var:-}" file="${!f_var:-}"
    if (( n == 1 )); then
      proxy="${proxy:-${WARP_PROXY}}"
      service="${service:-${COMPOSE_SERVICE}}"
      name="${name:-${WARP_NAME}}"
    fi
    [[ -n "${proxy}" ]] || die "WARP${n}_PROXY is not set (WARP${n}_* variables exist)"
    [[ -n "${service}" ]] || die "WARP${n}_SERVICE is not set: nothing to restart for WARP #${n}"
    name="${name:-${service}}"
    [[ "${name}" =~ ^[A-Za-z0-9._-]{1,32}$ ]] || die "WARP #${n}: name '${name}' must match [A-Za-z0-9._-]{1,32}"
    WARP_NAMES+=("${name}")
    WARP_PROXIES+=("$(normalize_proxy "${proxy}")")
    WARP_SERVICES+=("${service}")
    WARP_DIRS+=("${dir:-${COMPOSE_DIR}}")
    WARP_FILES+=("${file:-${COMPOSE_FILE}}")
  done

  for (( i = 0; i < ${#WARP_NAMES[@]}; i++ )); do
    for (( j = i + 1; j < ${#WARP_NAMES[@]}; j++ )); do
      [[ "${WARP_NAMES[i]}" != "${WARP_NAMES[j]}" ]] || die "two WARPs share the name '${WARP_NAMES[i]}'"
      [[ "${WARP_PROXIES[i]}" != "${WARP_PROXIES[j]}" ]] || die "two WARPs share the proxy '${WARP_PROXIES[i]}'"
      [[ "${WARP_SERVICES[i]}" != "${WARP_SERVICES[j]}" ]] || die "two WARPs share the service '${WARP_SERVICES[i]}'"
    done
  done
}

# Makes WARP number $1 (0-based) the current one.
select_warp() {
  local i="$1"
  CUR_NAME="${WARP_NAMES[i]}"
  CUR_PROXY="${WARP_PROXIES[i]}"
  CUR_SERVICE="${WARP_SERVICES[i]}"
  CUR_DIR="${WARP_DIRS[i]}"
  CUR_FILE="${WARP_FILES[i]}"
  if (( i == 0 )); then
    # WARP #1 keeps the file and alert names used before there were several.
    CUR_STATE_FILE="${STATE_FILE}"
    CUR_KEY_SUFFIX=''
  else
    CUR_STATE_FILE="${STATE_FILE}.${CUR_NAME}"
    CUR_KEY_SUFFIX=".${CUR_NAME}"
  fi
  if (( ${#WARP_NAMES[@]} > 1 )) || [[ "${CUR_NAME}" != 'warp' ]]; then
    CUR_WARP_TAG=" / ${CUR_NAME}"
  else
    CUR_WARP_TAG=''
  fi
}

# Prints the 0-based index of the WARP called $1 (any case), or fails.
find_warp() {
  local wanted="${1,,}" i
  for (( i = 0; i < ${#WARP_NAMES[@]}; i++ )); do
    if [[ "${WARP_NAMES[i],,}" == "${wanted}" ]]; then
      printf '%s' "${i}"
      return 0
    fi
  done
  return 1
}

# True if $1 is one of BAD_COUNTRIES.
is_bad_country() {
  local c="${1^^}" b
  # Unquoted on purpose: split the list at spaces (commas became spaces).
  # shellcheck disable=SC2086
  for b in ${BAD_COUNTRIES//,/ }; do
    if [[ "${b^^}" == "${c}" ]]; then
      return 0
    fi
  done
  return 1
}

# Flag emoji for a two-letter country code (regional indicators, written as
# UTF-8 bytes so it does not depend on the locale); the code itself otherwise.
flag_of() {
  local c="${1^^}"
  if [[ "${c}" =~ ^[A-Z]{2}$ ]]; then
    # U+1F1E6 + (letter - 'A') is F0 9F 87 (A6 + offset). The hex digits go into
    # the format string, because printf resolves \x escapes before it formats.
    local a b
    a=$(printf '%02X' $(( 0xA6 + $(printf '%d' "'${c:0:1}") - 65 )))
    b=$(printf '%02X' $(( 0xA6 + $(printf '%d' "'${c:1:1}") - 65 )))
    # shellcheck disable=SC2059
    printf "\\xF0\\x9F\\x87\\x${a}\\xF0\\x9F\\x87\\x${b}"
  else
    printf '❔'
  fi
}

load_warps
select_warp 0

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

# The bot token is part of the URL. Passing it as a curl argument would expose
# it to every local user in `ps` and /proc/<pid>/cmdline, so the URL goes
# through a curl config file (process substitution) instead; only a
# /dev/fd/N path appears in the argument list.
tg_api() {
  local method="$1" token
  shift
  token="${TG_TOKEN//\\/\\\\}"
  token="${token//\"/\\\"}"
  curl -fsS --max-time 30 \
    -K <(printf 'url = "%s/bot%s/%s"\n' "${TG_API_BASE}" "${token}" "${method}") \
    "$@" 2>/dev/null || true
}

tg_send() {
  local text="$1"
  tg_api sendMessage \
    -d "chat_id=${TG_CHAT_ID}" \
    --data-urlencode "text=[<code>${NODE_NAME}${CUR_WARP_TAG}</code>] ${text}" \
    -d 'parse_mode=HTML' \
    -d 'disable_web_page_preview=true' >/dev/null
}

# Like tg_send, but succeeds only if Telegram accepted the message. Use it in
# `if`: it returns non-zero on failure, which `set -e` would otherwise treat
# as fatal.
tg_send_ok() {
  local reply
  reply=$(tg_api sendMessage \
    -d "chat_id=${TG_CHAT_ID}" \
    --data-urlencode "text=[<code>${NODE_NAME}${CUR_WARP_TAG}</code>] $1" \
    -d 'parse_mode=HTML' \
    -d 'disable_web_page_preview=true')
  [[ "${reply}" == *'"ok":true'* ]]
}

# alert_raise KEY MESSAGE: tell the operator once per outage. The marker is
# written only after Telegram accepted the message, so an alert that could not
# be sent is retried on the next check instead of being lost; being on disk,
# it also survives a restart of the bot.
alert_raise() {
  local key="$1${CUR_KEY_SUFFIX}" message="$2"
  if [[ -e "${ALERT_DIR}/${key}" ]]; then
    return 0
  fi
  if tg_send_ok "${message}"; then
    mkdir -p "${ALERT_DIR}"
    : >"${ALERT_DIR}/${key}"
  fi
  return 0
}

# alert_clear KEY MESSAGE: report the recovery, but only if an alert for KEY
# was raised before.
alert_clear() {
  local key="$1${CUR_KEY_SUFFIX}" message="$2"
  if [[ ! -e "${ALERT_DIR}/${key}" ]]; then
    return 0
  fi
  if tg_send_ok "${message}"; then
    rm -f "${ALERT_DIR}/${key}"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# WARP helpers
# ---------------------------------------------------------------------------

# Returns 0 if the WARP SOCKS5 proxy answers, non-zero otherwise.
warp_alive() {
  curl -fs --max-time 8 \
    --socks5-hostname "${CUR_PROXY}" \
    'https://cloudflare.com/cdn-cgi/trace' >/dev/null 2>&1
}

# Prints the WARP exit IP as reported by Cloudflare, or nothing.
warp_exit_ip() {
  local trace
  trace=$(curl -s --max-time 8 \
    --socks5-hostname "${CUR_PROXY}" \
    'https://cloudflare.com/cdn-cgi/trace' 2>/dev/null || true)
  grep -oP '^ip=\K.*' <<<"${trace}" | head -n1 || true
}

# Prints the WARP exit country as reported by Cloudflare, or nothing.
warp_exit_country() {
  local trace
  trace=$(curl -s --max-time 8 \
    --socks5-hostname "${CUR_PROXY}" \
    'https://cloudflare.com/cdn-cgi/trace' 2>/dev/null || true)
  grep -oP '^loc=\K[A-Z]{2}' <<<"${trace}" | head -n1 || true
}

# Prints the ISO country code Google reports through WARP, or nothing.
# Primary source: YouTube (countryCode in inline JS).
# Fallbacks: Google Search (gl param), accounts.google.com.
detect_google_country() {
  local opts=(
    -fsSL
    --max-time "${CURL_TIMEOUT}"
    -A "${USER_AGENT}"
    -H "Cookie: ${GOOGLE_CONSENT_COOKIE}"
    -H 'Accept-Language: en-US,en;q=0.9'
    --socks5-hostname "${CUR_PROXY}"
  )
  local resp country=''

  resp=$(curl "${opts[@]}" 'https://www.youtube.com' 2>/dev/null || true)
  country=$(grep -oP '"countryCode":"\K[A-Z]{2}' <<<"${resp}" | head -n1 || true)
  if [[ -n "${country}" ]]; then
    printf '%s' "${country}"
    return
  fi

  resp=$(curl "${opts[@]}" 'https://www.google.com/search?q=test' 2>/dev/null || true)
  country=$(grep -oP '"gl":"\K[A-Z]{2}' <<<"${resp}" | head -n1 || true)
  if [[ -n "${country}" ]]; then
    printf '%s' "${country}"
    return
  fi

  resp=$(curl "${opts[@]}" 'https://accounts.google.com/' 2>/dev/null || true)
  country=$(grep -oP '"countryCode":"\K[A-Z]{2}' <<<"${resp}" | head -n1 || true)
  printf '%s' "${country}"
}

# Blocks until warp_alive() returns 0 or WARP_BOOT_TIMEOUT seconds elapse.
# Returns 0 if WARP came up, non-zero otherwise.
wait_for_warp() {
  local waited=0
  while (( waited < WARP_BOOT_TIMEOUT )); do
    if warp_alive; then
      return 0
    fi
    sleep 3
    waited=$(( waited + 3 ))
  done
  return 1
}

# Composes a short post-restart report: new Google country + WARP exit IP.
# Prints a Telegram-formatted string (no leading newline).
post_restart_report() {
  local previous_ip="${1:-}" country ip cloudflare_country

  if ! wait_for_warp; then
    printf '⚠️ WARP did not come up within %ss' "${WARP_BOOT_TIMEOUT}"
    return
  fi

  ip=$(warp_exit_ip)
  cloudflare_country=$(warp_exit_country)
  country=$(detect_google_country)

  local out=''
  out+="Google now: $(flag_of "${country}") <b>${country:-?}</b>"
  if [[ -n "${cloudflare_country}" ]]; then
    out+=$'\n'"Cloudflare loc: $(flag_of "${cloudflare_country}") <code>${cloudflare_country}</code>"
  fi
  if [[ -n "${ip}" && -n "${previous_ip}" && "${ip}" != "${previous_ip}" ]]; then
    out+=$'\n'"Exit IP: <code>${previous_ip}</code> → <code>${ip}</code>"
  elif [[ -n "${ip}" ]]; then
    out+=$'\n'"Exit IP: <code>${ip}</code>"
    if [[ "${ip}" == "${previous_ip}" ]]; then
      out+=' (unchanged ⚠️)'
    fi
  fi
  if is_bad_country "${country}"; then
    out+=$'\n'"🔴 still ${country} — try /restart again after cooldown"
  fi
  printf '%s' "${out}"
}

# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

read_last_restart() {
  local value=0
  # A WARP that has never been restarted has no stamp file yet (only the first
  # WARP's is created at start).
  if [[ -r "${CUR_STATE_FILE}" ]]; then
    value=$(<"${CUR_STATE_FILE}") || value=0
  fi
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    value=0
  fi
  printf '%s' "${value}"
}

# Restarts the WARP service and reports the new geo state.
#   $1  human-readable reason shown in the Telegram notification
# Returns:
#   EXIT_OK        on success
#   EXIT_FAIL      on docker failure
#   EXIT_COOLDOWN  if called again within RESTART_COOLDOWN seconds
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

  local cmd=(docker compose -f "${CUR_DIR}/${CUR_FILE}" restart "${CUR_SERVICE}")

  log "restarting warp: reason='${reason}'"

  # The exit address before the restart, so the report can say "old -> new".
  local previous_ip
  previous_ip=$(warp_exit_ip)

  local output
  if output=$("${cmd[@]}" 2>&1); then
    printf '%s\n' "${now}" >"${CUR_STATE_FILE}"

    local report
    report=$(post_restart_report "${previous_ip}")

    tg_send "✅ WARP restarted
Reason: <b>${reason}</b>
Service: <code>${CUR_SERVICE}</code>

${report}"
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

# One scheduled check of the current WARP: probe it, alert on outages, restart
# it if Google sees one of BAD_COUNTRIES.
check_current_warp() {
  local country rc

  if ! warp_alive; then
    log 'WARP proxy unreachable'
    alert_raise warp_down '🔴 WARP proxy is unreachable'
    return 0
  fi
  alert_clear warp_down '🟢 WARP proxy is reachable again'

  country=$(detect_google_country)
  if [[ -z "${country}" ]]; then
    log 'failed to detect Google country'
    alert_raise detect_failed '⚠️ Could not detect Google country through WARP'
    return 0
  fi
  alert_clear detect_failed '🟢 Google country detection works again'

  log "check: Google=${country}"

  if is_bad_country "${country}"; then
    tg_send "$(flag_of "${country}") Google is detected as <b>${country}</b> via WARP → restarting"
    rc=0
    restart_warp "auto: Google=${country}" || rc=$?
    if (( rc != EXIT_OK )); then
      log "auto restart returned ${rc}"
    fi
  fi
}

scheduled_check() {
  local now i
  now=$(date +%s)

  if (( now - last_check_ts < CHECK_INTERVAL )); then
    return 0
  fi
  last_check_ts=${now}

  for (( i = 0; i < ${#WARP_NAMES[@]}; i++ )); do
    select_warp "${i}"
    check_current_warp
  done
  CUR_WARP_TAG=''
}

# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------

cmd_help() {
  tg_send "🤖 <b>WARP bot</b>

/status  — current Google country and exit IP, per WARP
/check   — run a check right now
/restart [warp] — restart WARP manually (every WARP, or the named one) and report the new state
/help    — this message"
}

status_current_warp() {
  if ! warp_alive; then
    tg_send '⚠️ WARP SOCKS5 is unreachable'
    return 0
  fi

  local country ip cloudflare_country
  country=$(detect_google_country)
  ip=$(warp_exit_ip)
  cloudflare_country=$(warp_exit_country)

  local out="🌍 Google via WARP: $(flag_of "${country}") <b>${country:-?}</b>"
  if [[ -n "${cloudflare_country}" ]]; then
    out+=$'\n'"Cloudflare loc: $(flag_of "${cloudflare_country}") <code>${cloudflare_country}</code>"
  fi
  if [[ -n "${ip}" ]]; then
    out+=$'\n'"Exit IP: <code>${ip}</code>"
  fi
  tg_send "${out}"
}

cmd_status() {
  local i
  for (( i = 0; i < ${#WARP_NAMES[@]}; i++ )); do
    select_warp "${i}"
    status_current_warp
  done
}

check_current_warp_now() {
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

  if ! is_bad_country "${country}"; then
    status_current_warp
    return 0
  fi

  tg_send "$(flag_of "${country}") Google is detected as <b>${country}</b> via WARP → restarting"
  rc=0
  restart_warp "manual /check: Google=${country}" || rc=$?
  if (( rc == EXIT_COOLDOWN )); then
    tg_send '⏳ Restart skipped: cooldown active.'
  fi
}

cmd_check() {
  local i
  for (( i = 0; i < ${#WARP_NAMES[@]}; i++ )); do
    select_warp "${i}"
    check_current_warp_now
  done
}

# /restart restarts every WARP; /restart <name> one of them.
cmd_restart() {
  local wanted="${1:-}" i rc
  local indexes=()
  if [[ -n "${wanted}" ]]; then
    if ! i=$(find_warp "${wanted}"); then
      CUR_WARP_TAG=''
      tg_send "❓ Unknown WARP <code>${wanted//[<>&]/}</code>. Known: <code>${WARP_NAMES[*]}</code>"
      return 0
    fi
    indexes=("${i}")
  else
    for (( i = 0; i < ${#WARP_NAMES[@]}; i++ )); do
      indexes+=("${i}")
    done
  fi

  for i in "${indexes[@]}"; do
    select_warp "${i}"
    rc=0
    restart_warp 'manual /restart' || rc=$?
    if (( rc == EXIT_COOLDOWN )); then
      tg_send '⏳ Restart skipped: cooldown active.'
    fi
  done
}

dispatch_command() {
  local cmd arg
  read -r cmd arg _ <<<"$1"
  cmd="${cmd%%@*}"
  CUR_WARP_TAG=''
  case "${cmd}" in
    /start | /help) cmd_help ;;
    /status)        cmd_status ;;
    /check)         cmd_check ;;
    /restart)       cmd_restart "${arg:-}" ;;
    *)              : ;;
  esac
  CUR_WARP_TAG=''
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

  # An empty or non-ok reply means the poll itself failed (network down,
  # DNS error, revoked token, 409 from a second poller). A healthy long poll
  # blocks for POLL_TIMEOUT, so a failure returns instantly: the caller must
  # back off or the main loop spins.
  if [[ -z "${updates}" ]]; then
    return 1
  fi
  if ! jq -e '.ok == true' >/dev/null 2>&1 <<<"${updates}"; then
    return 1
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
  return 0
}

# Prints the next poll-failure delay: POLL_BACKOFF_MIN, then doubling,
# capped at POLL_BACKOFF_MAX. $1 is the current delay (0 = no failures yet).
next_backoff() {
  local current="$1" next
  if (( current <= 0 )); then
    next="${POLL_BACKOFF_MIN}"
  else
    next=$(( current * 2 ))
  fi
  if (( next > POLL_BACKOFF_MAX )); then
    next="${POLL_BACKOFF_MAX}"
  fi
  printf '%s' "${next}"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

main() {
  require_config
  require_commands
  init_state

  log "bot started (node=${NODE_NAME}, chat_id=${TG_CHAT_ID}, interval=${CHECK_INTERVAL}s)"

  local backoff=0
  while true; do
    if handle_updates; then
      backoff=0
    else
      backoff=$(next_backoff "${backoff}")
      log "telegram poll failed; retrying in ${backoff}s"
      sleep "${backoff}"
    fi
    scheduled_check
  done
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi