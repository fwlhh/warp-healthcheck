#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="warp-bot"
INSTALL_BIN="/usr/local/bin"
INSTALL_UNIT="/etc/systemd/system"
INSTALL_ENV="/etc/warp-bot.env"

die() { printf 'install: %s\n' "$*" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then
  die "must be run as root (use sudo)"
fi

for cmd in install systemctl; do
  command -v "${cmd}" >/dev/null 2>&1 || die "required command not found: ${cmd}"
done

for dep in curl jq docker; do
  command -v "${dep}" >/dev/null 2>&1 || die "runtime dependency not found: ${dep}. Install it first."
done

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ -f "${SOURCE_DIR}/bin/${SCRIPT_NAME}.sh" ]] || die "bin/${SCRIPT_NAME}.sh not found"
[[ -f "${SOURCE_DIR}/etc/systemd/${SCRIPT_NAME}.service" ]] || die "etc/systemd/${SCRIPT_NAME}.service not found"
[[ -f "${SOURCE_DIR}/etc/${SCRIPT_NAME}.env.example" ]] || die "etc/${SCRIPT_NAME}.env.example not found"

install -m 0755 "${SOURCE_DIR}/bin/${SCRIPT_NAME}.sh" "${INSTALL_BIN}/${SCRIPT_NAME}.sh"
install -m 0644 "${SOURCE_DIR}/etc/systemd/${SCRIPT_NAME}.service" "${INSTALL_UNIT}/${SCRIPT_NAME}.service"

if [[ ! -f "${INSTALL_ENV}" ]]; then
  install -m 0600 "${SOURCE_DIR}/etc/${SCRIPT_NAME}.env.example" "${INSTALL_ENV}"
  printf 'Created %s from example. Edit it before starting the service.\n' "${INSTALL_ENV}"
else
  printf 'Keeping existing %s (not overwritten).\n' "${INSTALL_ENV}"
fi

systemctl daemon-reload

printf '\nInstall complete.\n\n'
printf 'Next steps:\n'
printf '  1. Edit %s and set TG_TOKEN, TG_CHAT_ID, NODE_NAME.\n' "${INSTALL_ENV}"
printf '  2. Run: sudo systemctl enable --now %s\n' "${SCRIPT_NAME}"
printf '  3. Check: sudo journalctl -u %s -f\n' "${SCRIPT_NAME}"