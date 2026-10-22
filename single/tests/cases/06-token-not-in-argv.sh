#!/usr/bin/env bash
# shellcheck disable=SC2030,SC2031
# Intentional: each case runs in its own ( ... ) subshell so that the
# environment it sets cannot leak into the next case.
#
# The bot token is part of the Telegram URL. It must not be a curl argument:
# every local user can read arguments from `ps` and /proc/<pid>/cmdline.
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir -p "${work}/bin" "${work}/compose" "${work}/state"
printf 'version: "3"\n' > "${work}/compose/docker-compose.yml"

# Mock curl: records the argument list and the content of the -K config file.
cat > "${work}/bin/curl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" >> "${work}/args.log"
while (( \$# )); do
  if [[ "\$1" == "-K" ]]; then
    cat "\$2" >> "${work}/config.log"
    shift
  fi
  shift
done
printf '{"ok":true}'
EOF
chmod +x "${work}/bin/curl"

run_case() {
  local token="$1"
  : > "${work}/args.log"
  : > "${work}/config.log"
  (
    export PATH="${work}/bin:${PATH}"
    export TG_TOKEN="${token}" TG_CHAT_ID="123" NODE_NAME="test-node"
    export COMPOSE_DIR="${work}/compose" COMPOSE_FILE="docker-compose.yml"
    export STATE_DIR="${work}/state"
    # shellcheck disable=SC1090
    source "${SCRIPT}"
    tg_send "hello"
  )
}

# --- Case 1: an ordinary token is only in the config, never in argv --------
run_case '123456:SECRET-abc_DEF'
if grep -q 'SECRET' "${work}/args.log"; then
  printf 'bot token leaked into the curl argument list:\n' >&2
  cat "${work}/args.log" >&2
  exit 1
fi
if ! grep -qxF 'url = "https://api.telegram.org/bot123456:SECRET-abc_DEF/sendMessage"' "${work}/config.log"; then
  printf 'unexpected curl config:\n' >&2
  cat "${work}/config.log" >&2
  exit 1
fi

# --- Case 2: a token with a backslash and a quote is escaped for curl's -----
# config syntax (a bare quote would end the string early).
run_case '12:a\b"c'
want='url = "https://api.telegram.org/bot12:a\\b\"c/sendMessage"'
if ! grep -qxF "${want}" "${work}/config.log"; then
  printf 'token not escaped for the curl config; got:\n' >&2
  cat "${work}/config.log" >&2
  exit 1
fi

exit 0
