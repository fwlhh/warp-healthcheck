#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

tmp_bin="$(mktemp -d)"
tmp_compose="$(mktemp -d)"
trap 'rm -rf "${tmp_bin}" "${tmp_compose}"' EXIT

# Mock curl: picks the response by URL, mirroring the probe order in
# detect_google_country(): YouTube -> Google Search -> accounts.google.com.
cat > "${tmp_bin}/curl" <<'EOF'
#!/usr/bin/env bash
for arg in "$@"; do
  case "${arg}" in
    *youtube.com*)
      printf '%s' "${MOCK_YOUTUBE_STDOUT:-}"
      exit 0
      ;;
    *www.google.com/search*)
      printf '%s' "${MOCK_SEARCH_STDOUT:-}"
      exit 0
      ;;
    *accounts.google.com*)
      printf '%s' "${MOCK_ACCOUNTS_STDOUT:-}"
      exit 0
      ;;
  esac
done
printf ''
EOF
chmod +x "${tmp_bin}/curl"

export PATH="${tmp_bin}:${PATH}"

printf 'version: "3"\n' > "${tmp_compose}/docker-compose.yml"

export TG_TOKEN="test-token"
export TG_CHAT_ID="123"
export NODE_NAME="test-node"
export COMPOSE_DIR="${tmp_compose}"
export COMPOSE_FILE="docker-compose.yml"

# shellcheck disable=SC1090
source "${SCRIPT}"

expect_country() {
  local label="$1" want="$2" out
  out="$(detect_google_country)"
  if [[ "${out}" != "${want}" ]]; then
    printf '%s: expected %q, got %q\n' "${label}" "${want}" "${out}" >&2
    exit 1
  fi
}

reset_mocks() {
  export MOCK_YOUTUBE_STDOUT='' MOCK_SEARCH_STDOUT='' MOCK_ACCOUNTS_STDOUT=''
}

# Case 1: YouTube reports RU -> primary source wins
reset_mocks
export MOCK_YOUTUBE_STDOUT='<script>{"countryCode":"RU"}</script>'
expect_country 'youtube' 'RU'

# Case 2: YouTube has nothing, Google Search reports gl=DE
reset_mocks
export MOCK_SEARCH_STDOUT='<html>{"gl":"DE"}</html>'
expect_country 'search fallback' 'DE'

# Case 3: only accounts.google.com answers
reset_mocks
export MOCK_ACCOUNTS_STDOUT='{"countryCode":"FR"}'
expect_country 'accounts fallback' 'FR'

# Case 4: no probe yields a country -> empty ("unknown"), never a guess
reset_mocks
expect_country 'all probes empty' ''

# Case 5: the primary source takes precedence over fallbacks
reset_mocks
export MOCK_YOUTUBE_STDOUT='{"countryCode":"NL"}' MOCK_SEARCH_STDOUT='{"gl":"RU"}'
expect_country 'precedence' 'NL'

exit 0
