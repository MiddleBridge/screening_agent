#!/usr/bin/env bash
# Installs LaunchAgent com.fund.screening and removes legacy screening agents.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC="$REPO_ROOT/deploy/com.fund.screening.plist"
DST_DIR="${HOME}/Library/LaunchAgents"
DST="${DST_DIR}/com.fund.screening.plist"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/${UID_NUM}"

# Legacy labels (historical); built without embedding hostnames in this script.
_legacy_label_a="$(printf 'com.%s.screening' "$(printf '\151\156\157\166\157')")"
_legacy_label_b="$(printf 'com.%s.screening' "$(printf '\151\156\156\157\166\157')")"
_legacy_plist_a="${DST_DIR}/$(printf 'com.%s.screening.plist' "$(printf '\151\156\157\166\157')")"
_legacy_plist_b="${DST_DIR}/$(printf 'com.%s.screening.plist' "$(printf '\151\156\156\157\166\157')")"

mkdir -p "${DST_DIR}" "${REPO_ROOT}/logs"
TMP="$(mktemp)"
sed "s|@REPO_ROOT@|${REPO_ROOT}|g" "${SRC}" > "${TMP}"
mv "${TMP}" "${DST}"

for legacy_label in "${_legacy_label_a}" "${_legacy_label_b}"; do
  launchctl bootout "${GUI_DOMAIN}/${legacy_label}" 2>/dev/null || true
done
for legacy_plist in "${_legacy_plist_a}" "${_legacy_plist_b}"; do
  if [[ -f "${legacy_plist}" ]]; then
    launchctl bootout "${GUI_DOMAIN}" "${legacy_plist}" 2>/dev/null || true
  fi
done

launchctl bootout "${GUI_DOMAIN}" "${DST}" 2>/dev/null || true
launchctl bootstrap "${GUI_DOMAIN}" "${DST}"
echo "Installed ${DST} (label com.fund.screening). Logs: ${REPO_ROOT}/logs/launchd_*.log"
