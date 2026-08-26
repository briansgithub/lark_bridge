#!/usr/bin/env bash
# Resolve rig audio roles to ALSA card numbers.
#
# Runs ON THE PI. Source it, or call it to print the mapping.
#
# Why stable USB identity for microphones, and port paths for the instrument dongles:
#   - ALSA card order follows enumeration order and CHANGES across reboots.
#   - The Lark has a unique VID:PID and may be plugged into any Pi USB port.
#   - The K054 has a generic VID:PID and no serial; an optional port can pin it.
#   - The two AB13X dongles report an identical USB serial (202405280846), so serial
#     numbers cannot distinguish them either.
#   - Port paths remain necessary only to distinguish those otherwise identical rig tools.
#
# Roles (see rig/inventory.toml):
#   lark      any port Hollyland 3547:0407 — preferred microphone
#   fifine    any port (or configured port) 0c76:161e — fallback microphone
#   dongle_a  1-1.5  AB13X 001f:0b26     — DUT wired output (Mode 1W)
#   dongle_b  1-1.4  C-Media 0d8c:0014   — INSTRUMENT: out drives speaker, in captures

set -euo pipefail

LARK_USB_ID="${LARK_USB_ID:-3547:0407}"
LARK_USB_SERIAL="${LARK_USB_SERIAL:-}"
FIFINE_USB_ID="${FIFINE_USB_ID:-0c76:161e}"
FIFINE_USB_SERIAL="${FIFINE_USB_SERIAL:-}"
FIFINE_PORT="${FIFINE_PORT:-}"
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
  local wanted_id wanted_serial wanted_port c n identity id serial path matches=""
  wanted_id="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  wanted_serial="${2:-}"
  wanted_port="${3:-}"
  for c in /sys/class/sound/card[0-9]*; do
    [ -e "$c" ] || continue
    n="$(basename "$c" | tr -cd '0-9')"
    identity="$(usb_identity_for_card "$n" || true)"
    [ -n "$identity" ] || continue
    id="${identity%:*}"
    serial="${identity##*:}"
    path="$(readlink -f "$c/device" 2>/dev/null || true)"
    if [ "$id" = "$wanted_id" ] \
      && { [ -z "$wanted_serial" ] || [ "$serial" = "$wanted_serial" ]; } \
      && { [ -z "$wanted_port" ] || printf '%s\n' "$path" | grep -qE "/${wanted_port}(/|\$)"; }; then
      matches="${matches}${matches:+ }${n}"
    fi
  done
  case "$matches" in
    "") return 1 ;;
    *" "*) printf 'AMBIGUOUS(%s)\n' "$matches"; return 2 ;;
    *) printf '%s\n' "$matches"; return 0 ;;
  esac
}

card_has_capture()  { [ -e "/proc/asound/card$1/pcm0c" ]; }
card_has_playback() { [ -e "/proc/asound/card$1/pcm0p" ]; }

rig_resolve() {
  LARK_CARD="$(card_for_usb_identity "$LARK_USB_ID" "$LARK_USB_SERIAL" "" || true)"
  FIFINE_CARD="$(card_for_usb_identity "$FIFINE_USB_ID" "$FIFINE_USB_SERIAL" "$FIFINE_PORT" || true)"
  case "$LARK_CARD" in
    [0-9]*) MICROPHONE_CARD="$LARK_CARD"; MICROPHONE_ID="lark-a1" ;;
    AMBIGUOUS*) MICROPHONE_CARD=""; MICROPHONE_ID="" ;;
    *)
      case "$FIFINE_CARD" in
        [0-9]*) MICROPHONE_CARD="$FIFINE_CARD"; MICROPHONE_ID="fifine-k054" ;;
        *) MICROPHONE_CARD=""; MICROPHONE_ID="" ;;
      esac
      ;;
  esac
  DONGLE_A_CARD="$(card_for_port "$DONGLE_A_PORT" || echo '')"
  DONGLE_B_CARD="$(card_for_port "$DONGLE_B_PORT" || echo '')"
  export LARK_CARD FIFINE_CARD MICROPHONE_CARD MICROPHONE_ID DONGLE_A_CARD DONGLE_B_CARD
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  rig_resolve
  printf '%-10s %-12s %-6s %-18s %s\n' ROLE SELECTOR CARD ID STREAMS
  for spec in "lark:$LARK_USB_ID:$LARK_CARD" \
              "fifine:$FIFINE_USB_ID:$FIFINE_CARD" \
              "dongle_a:$DONGLE_A_PORT:$DONGLE_A_CARD" \
              "dongle_b:$DONGLE_B_PORT:$DONGLE_B_CARD"; do
    role="${spec%%:*}"; rest="${spec#*:}"; card="${rest##*:}"
    selector="${rest%:*}"
    if [ -z "$card" ]; then
      printf '%-10s %-12s %-6s %-18s %s\n' "$role" "$selector" '-' '-' 'NOT PRESENT'
      continue
    fi
    case "$card" in
      AMBIGUOUS*)
        printf '%-10s %-12s %-6s %-18s %s\n' "$role" "$selector" '-' '-' "$card"
        continue
        ;;
    esac
    s=""
    card_has_capture  "$card" && s="${s}capture "
    card_has_playback "$card" && s="${s}playback"
    printf '%-10s %-12s %-6s %-18s %s\n' \
      "$role" "$selector" "$card" "$(cat "/proc/asound/card$card/id")" "$s"
  done
fi
