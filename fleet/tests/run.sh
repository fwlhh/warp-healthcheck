#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
TMP="$(mktemp -d)"
COORD_PID=""

cleanup() {
  if [[ -n "${COORD_PID}" ]]; then
    kill "${COORD_PID}" 2>/dev/null || true
    wait "${COORD_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMP}"
}
trap cleanup EXIT

export TG_TOKEN="dummy"
export TG_CHAT_ID="1"
export DB_PATH="${TMP}/c.db"
export HTTP_HOST="127.0.0.1"
export HTTP_PORT="18080"

python3 "${ROOT}/fleet/coordinator/coordinator.py" add-node test-node >"${TMP}/node.txt"
grep -q '^NODE_NAME=test-node$' "${TMP}/node.txt"

python3 "${ROOT}/fleet/coordinator/coordinator.py" run &
COORD_PID=$!

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:18080/commands" -H 'X-Node: x' -H 'X-Token: x' 2>/dev/null; then
    break
  fi
  sleep 0.2
done

TOKEN="$(awk -F= '/^NODE_TOKEN=/{print $2}' "${TMP}/node.txt")"

curl -fsS -X POST \
  -H "X-Node: test-node" -H "X-Token: ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"google_country":"DE","warp_alive":true,"last_restart":0}' \
  "http://127.0.0.1:18080/heartbeat" >/dev/null

python3 - "${DB_PATH}" <<'PY'
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
c.execute(
    "INSERT INTO commands (node, command, created_at) VALUES (?, ?, ?)",
    ("test-node", "restart", int(time.time())),
)
c.commit()
PY

OUT="$(curl -fsS -H "X-Node: test-node" -H "X-Token: ${TOKEN}" \
  http://127.0.0.1:18080/commands)"

echo "${OUT}" | grep -q '"cmd": "restart"'
echo "fleet tests OK"