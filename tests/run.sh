#!/usr/bin/env bash
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CASES_DIR="${TESTS_DIR}/cases"
PROJECT_ROOT="$(cd "${TESTS_DIR}/.." && pwd)"

export PROJECT_ROOT

passed=0
failed=0
failed_names=()

for case_file in "${CASES_DIR}"/*.sh; do
  [[ -f "${case_file}" ]] || continue
  name="$(basename "${case_file}")"
  printf '=== %s\n' "${name}"
  if bash "${case_file}"; then
    printf -- '--- %s: PASS\n\n' "${name}"
    passed=$(( passed + 1 ))
  else
    printf -- '--- %s: FAIL\n\n' "${name}"
    failed=$(( failed + 1 ))
    failed_names+=("${name}")
  fi
done

printf 'Passed: %d\n' "${passed}"
printf 'Failed: %d\n' "${failed}"

if (( failed > 0 )); then
  printf 'Failed cases:\n'
  for n in "${failed_names[@]}"; do
    printf '  - %s\n' "${n}"
  done
  exit 1
fi

exit 0