#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SCRIPT="${PROJECT_ROOT}/bin/warp-bot.sh"

tmp_bin="$(mktemp -d)"
trap 'rm -rf "${tmp_bin}"' EXIT

# Mock curl: looks at the URL and returns the corresponding stdout.
cat > "${tmp_bin}/curl" <<'EOF'
#!/usr/bin/env bash
for arg in "$@"; do
  case "${arg}" in
    *play.google.com*)
      printf '%s' "${MOCK_PLAY_STDOUT:-}"
      exit 0
      ;;
    *www.google.com*)
      printf '%s' "${MOCK_GOOGLE_STDOUT:-}"
      exit 0
      ;;
  esac
done
printf ''
EOF
chmod +x "${tmp_bin}/curl"

export PATH="${tmp_bin}:${PATH}"

tmp_compose="$(mktemp -d)"
trap 'rm -rf "${tmp_bin}" "${tmp_compose}"' EXIT
printf 'version: "3"\n' > "${tmp_compose}/docker-compose.yml"

export TG_TOKEN="test-token"
export TG_CHAT_ID="123"
export NODE_NAME="test-node"
export COMPOSE_DIR="${tmp_compose}"
export COMPOSE_FILE="docker-compose.yml"

# shellcheck disable=SC1090
source "${SCRIPT}"

# Case 1: www.google.com contains ru_RU → expect RU
export MOCK_GOOGLE_STDOUT='<html>"ru_RU"</html>'
export MOCK_PLAY_STDOUT=''
out="$(detect_google_country)"
if [[ "${out}" != "RU" ]]; then
  printf 'expected RU, got %q\n' "${out}" >&2
  exit 1
fi

# Case 2: www.google.com is empty, play.google.com returns countryCode DE
export MOCK_GOOGLE_STDOUT=''
export MOCK_PLAY_STDOUT='"countryCode":"DE"'
out="$(detect_google_country)"
if [[ "${out}" != "DE" ]]; then
  printf 'expected DE, got %q\n' "${out}" >&2
  exit 1
fi

exit 0