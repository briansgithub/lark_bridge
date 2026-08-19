#!/usr/bin/env bash
# Resolve rig audio roles to ALSA card numbers.
#
# Runs ON THE PI. Source it, or call it to print the mapping.
#
# Why stable USB identity for the Lark, and port paths for the instrument dongles:
#   - ALSA card order follows enumeration order and CHANGES across reboots.
#   - The Lark has a unique VID:PID and may be plugged into any Pi USB port.
#   - The two AB13X dongles report an identical USB serial (202405280846), so serial
#     numbers cannot distinguish them either.
#   - Port paths remain necessary only to distinguish those otherwise identical rig tools.
#
# Roles (see rig/inventory.toml):
#   lark      any port Hollyland 3547:0407 — the microphone under test
#   dongle_a  1-1.5  AB13X 001f:0b26     — DUT wired output (Mode 1W)
#   dongle_b  1-1.4  C-Media 0d8c:0014   — INSTRUMENT: out drives speaker, in captures

set -euo pipefail

LARK_USB_ID="${LARK_USB_ID:-3547:0407}"
LARK_USB_SERIAL="${LARK_USB_SERIAL:-}"
DONGLE_A_PORT="${DONGLE_A_PORT:-1-1.5}"
DONGLE_B_PORT="${DONGLE_B_PORT:-1-1.4}"

card_for_port() {
  local port="$1" c n
  for c in /sys/class/sound/card[0-9]*; do
    [ -e "$c" ] || continue
    n="$(basename "$c" | tr -cd '0-9')"
    if readlink -f "$c/device" 2>/dev/null | grep -qE "/${port}(/|\$)"; then
      printf '%s\n' "$n"
      return 0
    fi
  done
  return 1
}

usb_identity_for_card() {
  local card="$1" path vendor product serial=""
  path="$(readlink -f "/sys/class/sound/card${card}/device" 2>/dev/null)" || return 1
  while [ "$path" != "/" ] && [ -n "$path" ]; do
    if [ -r "$path/idVendor" ] && [ -r "$path/idProduct" ]; then
      vendor="$(tr '[:upper:]' '[:lower:]' < "$path/idVendor")"
      product="$(tr '[:upper:]' '[:lower:]' < "$path/idProduct")"
      [ -r "$path/serial" ] && serial="$(cat "$path/serial")"
      printf '%s:%s:%s\n' "$vendor" "$product" "$serial"
      return 0
    fi
    path="${path%/*}"
    [ -n "$path" ] || path="/"
  done
  return 1
}

card_for_usb_identity() {
  local wanted_id wanted_serial c n identity id serial
  wanted_id="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  wanted_serial="${2:-}"
  for c in /sys/class/sound/card[0-9]*; do
    [ -e "$c" ] || continue
    n="$(basename "$c" | tr -cd '0-9')"
    identity="$(usb_identity_for_card "$n" || true)"
    [ -n "$identity" ] || continue
    id="${identity%:*}"
    serial="${identity##*:}"
    if [ "$id" = "$wanted_id" ] && { [ -z "$wanted_serial" ] || [ "$serial" = "$wanted_serial" ]; }; then
      printf '%s\n' "$n"
      return 0
    fi
  done
  return 1
}

card_has_capture()  { [ -e "/proc/asound/card$1/pcm0c" ]; }
card_has_playback() { [ -e "/proc/asound/card$1/pcm0p" ]; }

rig_resolve() {
  LARK_CARD="$(card_for_usb_identity "$LARK_USB_ID" "$LARK_USB_SERIAL" || echo '')"
  DONGLE_A_CARD="$(card_for_port "$DONGLE_A_PORT" || echo '')"
  DONGLE_B_CARD="$(card_for_port "$DONGLE_B_PORT" || echo '')"
  export LARK_CARD DONGLE_A_CARD DONGLE_B_CARD
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  rig_resolve
  printf '%-10s %-12s %-6s %-18s %s\n' ROLE SELECTOR CARD ID STREAMS
  for spec in "lark:$LARK_USB_ID:$LARK_CARD" \
              "dongle_a:$DONGLE_A_PORT:$DONGLE_A_CARD" \
              "dongle_b:$DONGLE_B_PORT:$DONGLE_B_CARD"; do
    role="${spec%%:*}"; rest="${spec#*:}"; card="${rest##*:}"
    selector="${rest%:*}"
    if [ -z "$card" ]; then
      printf '%-10s %-12s %-6s %-18s %s\n' "$role" "$selector" '-' '-' 'NOT PRESENT'
      continue
    fi
    s=""
    card_has_capture  "$card" && s="${s}capture "
    card_has_playback "$card" && s="${s}playback"
    printf '%-10s %-12s %-6s %-18s %s\n' \
      "$role" "$selector" "$card" "$(cat "/proc/asound/card$card/id")" "$s"
  done
fi
