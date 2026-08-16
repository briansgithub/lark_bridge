#!/usr/bin/env bash
# Set Broadcom SCO routing to the HCI transport, and VERIFY it took.
#
# Runs as root from bridge-btfw.service. Idempotent: safe to run repeatedly.
#
# Vendor commands (Broadcom, OGF 0x3F):
#   0x1C Write_SCO_PCM_Int_Param  <routing> <rate> <frame> <sync> <clock>
#   0x1D Read_SCO_PCM_Int_Param   -> same five bytes back
#
#   routing: 0=PCM  1=Transport(HCI)  2=Codec  3=I2S
#
# We want 1 (Transport). Everything after it is the conventional Broadcom default and is
# ignored once routing is Transport.

set -euo pipefail

HCI="${BRIDGE_HCI:-hci0}"
DEV="${HCI#hci}"
WANT_ROUTING="01"

log() { printf '[bridge-btfw] %s\n' "$*"; }
die() { printf '[bridge-btfw] ERROR: %s\n' "$*" >&2; exit 1; }

command -v hcitool >/dev/null 2>&1 || die "hcitool not found (package: bluez)"

read_params() {
  # Command Complete payload: 01 1D FC <status> <5 params>
  hcitool -i "$HCI" cmd 0x3f 0x1d 2>/dev/null \
    | tr -s ' \n' ' ' \
    | grep -oE '01 1D FC [0-9A-F]{2}( [0-9A-F]{2}){5}' \
    | tail -1 \
    | awk '{print $5, $6, $7, $8, $9}'
}

BEFORE="$(read_params || true)"
log "current SCO PCM params: ${BEFORE:-<unreadable>}"

CURRENT_ROUTING="${BEFORE%% *}"
if [ "$CURRENT_ROUTING" = "$WANT_ROUTING" ]; then
  log "SCO routing already Transport(HCI) — nothing to do"
  exit 0
fi

log "SCO routing is 0x${CURRENT_ROUTING:-??} — setting to 0x01 (Transport/HCI)"
hcitool -i "$HCI" cmd 0x3f 0x1c 0x01 0x02 0x00 0x01 0x01 >/dev/null 2>&1 \
  || die "controller rejected Write_SCO_PCM_Int_Param"

sleep 1
AFTER="$(read_params || true)"
log "new SCO PCM params: ${AFTER:-<unreadable>}"

# Verify rather than assume: on some BCM firmware the command is accepted but has no
# effect, which is the failure mode the btstack-dev thread describes.
case "$AFTER" in
  "$WANT_ROUTING "*) log "verified: SCO now routed to the HCI transport" ;;
  "")                die "could not read back parameters — cannot confirm the change" ;;
  *)                 die "command accepted but routing is still 0x${AFTER%% *}; SCO audio will not reach the host" ;;
esac
