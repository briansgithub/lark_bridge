#!/usr/bin/env bash
# Resolve rig audio roles to ALSA card numbers, by USB PORT PATH.
#
# Runs ON THE PI. Source it, or call it to print the mapping.
#
# Why port paths and never card numbers or names:
#   - ALSA card order follows enumeration order and CHANGES across reboots.
#   - The two AB13X dongles report an identical USB serial (202405280846), so serial
#     numbers cannot distinguish them either.
#   - The port path is the physical socket, which is the only thing that stays put.
#
# Roles (see rig/inventory.toml):
#   lark      1-1.3  Hollyland 3547:0407 — the microphone under test
#   dongle_a  1-1.5  AB13X 001f:0b26     — DUT wired output (Mode 1W)
#   dongle_b  1-1.4  C-Media 0d8c:0014   — INSTRUMENT: out drives speaker, in captures

set -euo pipefail

LARK_PORT="${LARK_PORT:-1-1.3}"
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

card_has_capture()  { [ -e "/proc/asound/card$1/pcm0c" ]; }
card_has_playback() { [ -e "/proc/asound/card$1/pcm0p" ]; }

rig_resolve() {
  LARK_CARD="$(card_for_port "$LARK_PORT" || echo '')"
  DONGLE_A_CARD="$(card_for_port "$DONGLE_A_PORT" || echo '')"
  DONGLE_B_CARD="$(card_for_port "$DONGLE_B_PORT" || echo '')"
  export LARK_CARD DONGLE_A_CARD DONGLE_B_CARD
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  rig_resolve
  printf '%-10s %-8s %-6s %-14s %s\n' ROLE PORT CARD ID STREAMS
  for spec in "lark:$LARK_PORT:$LARK_CARD" \
              "dongle_a:$DONGLE_A_PORT:$DONGLE_A_CARD" \
              "dongle_b:$DONGLE_B_PORT:$DONGLE_B_CARD"; do
    role="${spec%%:*}"; rest="${spec#*:}"; port="${rest%%:*}"; card="${rest##*:}"
    if [ -z "$card" ]; then
      printf '%-10s %-8s %-6s %-14s %s\n' "$role" "$port" '-' '-' 'NOT PRESENT'
      continue
    fi
    s=""
    card_has_capture  "$card" && s="${s}capture "
    card_has_playback "$card" && s="${s}playback"
    printf '%-10s %-8s %-6s %-14s %s\n' \
      "$role" "$port" "$card" "$(cat "/proc/asound/card$card/id")" "$s"
  done
fi
