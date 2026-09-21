#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="warp-bot"
INSTALL_BIN="/usr/local/bin"
INSTALL_UNIT="/etc/systemd/system"
INSTALL_ENV="/etc/warp-bot.env"
STATE_DIR="/var/lib/warp-bot"

die() { printf 'uninstall: %s\n' "$*" >&2; exit 1; }

if [[ "${EUID}" -ne 0 ]]; then
  die "must be run as root (use sudo)"
fi

if systemctl list-unit-files "${SCRIPT_NAME}.service" >/dev/null 2>&1; then
  systemctl stop "${SCRIPT_NAME}.service" 2>/dev/null || true
  systemctl disable "${SCRIPT_NAME}.service" 2>/dev/null || true
fi

rm -f "${INSTALL_UNIT}/${SCRIPT_NAME}.service"
rm -f "${INSTALL_BIN}/${SCRIPT_NAME}.sh"

systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

read -r -p "Remove ${INSTALL_ENV}? [y/N] " ans
case "${ans,,}" in
  y|yes) rm -f "${INSTALL_ENV}"; printf 'Removed %s\n' "${INSTALL_ENV}" ;;
  *) printf 'Kept %s\n' "${INSTALL_ENV}" ;;
esac

read -r -p "Remove state directory ${STATE_DIR}? [y/N] " ans
case "${ans,,}" in
  y|yes) rm -rf "${STATE_DIR}"; printf 'Removed %s\n' "${STATE_DIR}" ;;
  *) printf 'Kept %s\n' "${STATE_DIR}" ;;
esac

printf 'Uninstall complete.\n'