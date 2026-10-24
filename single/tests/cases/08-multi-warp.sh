#!/usr/bin/env bash
# shellcheck disable=SC2317,SC2329,SC2034,SC2016,SC2154
# SC2317/SC2329: the probe functions below are stand-ins the sourced script calls.
# SC2034/SC2154: variables shared with the sourced script.
# SC2016: the bash -c programs are meant to be single-quoted.
#
# Several WARPs on one node: WARP2_*, WARP3_*, ... are discovered from the
# environment (no limit); every WARP has its own proxy, cooldown and alerts;
# every message says which WARP it is about.
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin" "${work}/compose" "${work}/state"
printf 'version: "3"\n' > "${work}/compose/docker-compose.yml"

# Mock curl: one line per call in calls.log; answers like the Bot API.
cat > "${work}/bin/curl" <<EOF
#!/usr/bin/env bash
# One log line per call, even for a multi-line message.
printf '%s ' "\$@" | tr '\n' ' ' >> "${work}/calls.log"
echo >> "${work}/calls.log"
printf '{"ok":true,"result":{}}'
EOF
# Mock docker: records the compose command line.
cat > "${work}/bin/docker" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "${work}/docker.log"
exit 0
EOF
chmod +x "${work}/bin/"*
: > "${work}/calls.log"
: > "${work}/docker.log"

export PATH="${work}/bin:${PATH}"
export TG_TOKEN="test-token" TG_CHAT_ID="123" NODE_NAME="test-node"
export COMPOSE_DIR="${work}/compose" COMPOSE_FILE="docker-compose.yml"
export STATE_DIR="${work}/state" CHECK_INTERVAL=0
export RESTART_COOLDOWN=300 WARP_BOOT_TIMEOUT=1

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

expect_eq() {
  [[ "$2" == "$3" ]] || fail "$1: expected '$3', got '$2'"
}

# --- Configuration: run in fresh processes ----------------------------------
names_with() { # VAR=value ... -> the WARP names, or "DIED: <message>"
  local out rc=0
  out=$(env "$@" bash -c 'source "$1"; printf "%s" "${WARP_NAMES[*]}"' _ "${SCRIPT}" 2>&1) || rc=$?
  if (( rc != 0 )); then
    printf 'DIED: %s' "${out}"
  else
    printf '%s' "${out}"
  fi
}

expect_eq 'one WARP by default' "$(names_with)" 'warp'
expect_eq 'a second WARP' \
  "$(names_with WARP2_PROXY=127.0.0.1:1081 WARP2_SERVICE=warp2)" 'warp warp2'
expect_eq 'an explicit name' \
  "$(names_with WARP2_NAME=fra WARP2_PROXY=127.0.0.1:1081 WARP2_SERVICE=w2)" 'warp fra'
expect_eq 'the first WARP can be named' \
  "$(names_with WARP_NAME=ams WARP2_PROXY=127.0.0.1:1081 WARP2_SERVICE=w2)" 'ams w2'

many=()
for n in $(seq 2 12); do
  many+=("WARP${n}_PROXY=127.0.0.1:$((1079 + n))" "WARP${n}_SERVICE=w${n}")
done
expect_eq 'no limit, numeric order' \
  "$(names_with "${many[@]}")" 'warp w2 w3 w4 w5 w6 w7 w8 w9 w10 w11 w12'

proxy_of_second() {
  env "$@" bash -c 'source "$1"; printf "%s" "${WARP_PROXIES[1]}"' _ "${SCRIPT}"
}
expect_eq 'docker ports form' \
  "$(proxy_of_second WARP2_PROXY=127.0.0.1:1081:1080 WARP2_SERVICE=w2)" '127.0.0.1:1081'
expect_eq 'plain form' \
  "$(proxy_of_second WARP2_PROXY=127.0.0.1:1081 WARP2_SERVICE=w2)" '127.0.0.1:1081'

for bad in \
  'WARP2_SERVICE=w2' \
  'WARP2_PROXY=127.0.0.1:1081' \
  'WARP2_PROXY=127.0.0.1:1081 WARP2_SERVICE=warp' \
  'WARP2_PROXY=127.0.0.1:1080 WARP2_SERVICE=w2' \
  'WARP2_PROXY=127.0.0.1:1081 WARP2_SERVICE=w2 WARP2_NAME=warp' \
  'WARP2_PROXY=127.0.0.1:1081 WARP2_SERVICE=w2 WARP2_NAME=bad_name!'; do
  # shellcheck disable=SC2086
  got="$(names_with ${bad})"
  [[ "${got}" == DIED:* ]] || fail "accepted a bad configuration: ${bad} -> ${got}"
done

# --- Load two WARPs into this shell -----------------------------------------
export WARP2_NAME=warp2 WARP2_PROXY=127.0.0.1:1081:1080 WARP2_SERVICE=warp2
# shellcheck disable=SC1090
source "${SCRIPT}"

expect_eq 'two WARPs loaded' "${WARP_NAMES[*]}" 'warp warp2'
select_warp 1
expect_eq 'tag of a multi-WARP node' "${CUR_WARP_TAG}" ' / warp2'

# Probes ask through the current WARP's proxy.
warp_alive || true
grep -q -- '--socks5-hostname 127.0.0.1:1081 ' "${work}/calls.log" \
  || fail 'warp_alive did not use the second proxy'
: > "${work}/calls.log"
select_warp 0
warp_alive || true
grep -q -- '--socks5-hostname 127.0.0.1:1080 ' "${work}/calls.log" \
  || fail 'warp_alive did not use the first proxy'
: > "${work}/calls.log"

# Stand-ins for the probes, per WARP.
declare -A UP=([warp]=1 [warp2]=1)
declare -A COUNTRY=([warp]=NL [warp2]=NL)
IP_BEFORE=1.1.1.1
IP_AFTER=2.2.2.2
# "Restarted" is a marker file: the docker stand-in runs in a command
# substitution, so a plain variable it sets would be lost.
warp_alive() { [[ "${UP[${CUR_NAME}]}" == 1 ]]; }
detect_google_country() { printf '%s' "${COUNTRY[${CUR_NAME}]}"; }
warp_exit_country() { printf 'NL'; }
warp_exit_ip() { if [[ -e "${work}/restarted" ]]; then printf '%s' "${IP_AFTER}"; else printf '%s' "${IP_BEFORE}"; fi; }

# The API method is in curl's config file, not in its arguments; every message
# the bot sends carries chat_id=.
messages() { grep -c 'chat_id=' "${work}/calls.log" || true; }
docker_calls() { grep -c . "${work}/docker.log" || true; }
last_message() { grep 'chat_id=' "${work}/calls.log" | tail -n1; }
check() { last_check_ts=0; scheduled_check; }

# --- Scheduled checks: each WARP on its own ----------------------------------
check
expect_eq 'healthy: silence' "$(messages)" 0

UP[warp2]=0
check
expect_eq 'only the second WARP is reported' "$(messages)" 1
last_message | grep -q 'test-node / warp2' || fail 'alert does not name the WARP'
[[ -e "${ALERT_DIR}/warp_down.warp2" ]] || fail 'no alert marker for the second WARP'
[[ ! -e "${ALERT_DIR}/warp_down" ]] || fail 'the first WARP was marked down too'
check; check
expect_eq 'no repeat while it stays down' "$(messages)" 1

UP[warp2]=1
check
expect_eq 'recovery' "$(messages)" 2
last_message | grep -q 'test-node / warp2' || fail 'recovery does not name the WARP'

# --- BAD_COUNTRIES: only the WARP in a refused country is restarted ----------
: > "${work}/calls.log"
COUNTRY[warp2]=RU
check
expect_eq 'one restart' "$(docker_calls)" 1
grep -q 'restart warp2' "${work}/docker.log" || fail 'restarted the wrong service'
grep -q 'restart warp$' "${work}/docker.log" && fail 'restarted the healthy WARP too'
[[ -s "${STATE_DIR}/last-restart.warp2" ]] || fail 'no cooldown stamp for the second WARP'
last_message | grep -q 'test-node / warp2' || fail 'report does not name the WARP'

# Each WARP has its own cooldown: restarting the first one is not blocked.
COUNTRY[warp]=RU
: > "${work}/docker.log"
check
grep -q 'restart warp$' "${work}/docker.log" || fail 'first WARP was held back by the second one'
grep -q 'restart warp2' "${work}/docker.log" && fail 'second WARP restarted inside its cooldown'

# --- /restart [warp] -------------------------------------------------------------
COUNTRY[warp]=NL COUNTRY[warp2]=NL
rm -f "${STATE_DIR}/last-restart" "${STATE_DIR}/last-restart.warp2"
: > "${work}/docker.log" ; : > "${work}/calls.log"
dispatch_command '/restart WARP2'
expect_eq 'named WARP only (any case)' "$(cat "${work}/docker.log")" \
  "compose -f ${work}/compose/docker-compose.yml restart warp2"
dispatch_command '/restart warp2'
last_message | grep -q 'cooldown' || fail 'a second restart inside the cooldown was not refused'
: > "${work}/docker.log"
dispatch_command '/restart@my_bot'
expect_eq 'a bare /restart restarts what is not cooling down' \
  "$(cat "${work}/docker.log")" "compose -f ${work}/compose/docker-compose.yml restart warp"

: > "${work}/docker.log"
dispatch_command '/restart nope'
expect_eq 'unknown WARP restarts nothing' "$(docker_calls)" 0
last_message | grep -q 'Unknown WARP' || fail 'no message for an unknown WARP'
last_message | grep -q 'warp warp2' || fail 'the known WARPs were not listed'

# Messages that are not about a WARP carry no WARP name.
dispatch_command '/help'
last_message | grep -q 'test-node / ' && fail 'a help message was tagged with a WARP'

# --- The report shows the exit address change -------------------------------------
rm -f "${STATE_DIR}/last-restart" "${STATE_DIR}/last-restart.warp2" "${work}/restarted"
IP_BEFORE=1.1.1.1 IP_AFTER=2.2.2.2
restart_with_ip_change() { select_warp 0; restart_warp 'test' >/dev/null 2>&1 || true; }
# warp_exit_ip answers differently once the docker stand-in has run:
docker() { : > "${work}/restarted"; echo "$*" >> "${work}/docker.log"; }
restart_with_ip_change
last_message | grep -q '1.1.1.1</code> → <code>2.2.2.2' || fail 'address change not shown'
rm -f "${STATE_DIR}/last-restart" "${work}/restarted"
IP_AFTER=1.1.1.1
restart_with_ip_change
last_message | grep -q 'unchanged' || fail 'an unchanged address was not called out'
unset -f docker

# --- BAD_COUNTRIES is configurable --------------------------------------------------
bad_with() { # BAD_COUNTRIES value, country -> yes/no
  env BAD_COUNTRIES="$1" bash -c 'source "$1"; is_bad_country "$2" && echo yes || echo no' _ "${SCRIPT}" "$2"
}
expect_eq 'RU is bad by default' "$(env -u BAD_COUNTRIES bash -c 'source "$1"; is_bad_country ru && echo yes || echo no' _ "${SCRIPT}")" yes
expect_eq 'BY is not by default' "$(env -u BAD_COUNTRIES bash -c 'source "$1"; is_bad_country BY && echo yes || echo no' _ "${SCRIPT}")" no
expect_eq 'BY once configured' "$(bad_with 'RU BY' by)" yes
expect_eq 'comma separated' "$(bad_with 'RU,BY,IR' ir)" yes
expect_eq 'others stay fine' "$(bad_with 'RU,BY' NL)" no
expect_eq 'empty list: nothing is bad' "$(bad_with '' RU)" no

# --- Flags ---------------------------------------------------------------------------
expect_eq 'flag NL' "$(LC_ALL=C flag_of nl)" '🇳🇱'
expect_eq 'flag RU' "$(LC_ALL=C flag_of RU)" '🇷🇺'
expect_eq 'no flag for junk' "$(flag_of 1x)" '❔'

exit 0
