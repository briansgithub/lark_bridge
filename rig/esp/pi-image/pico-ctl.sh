#!/usr/bin/env bash
# pico-ctl.sh - drive the Pico fake Lark from the rig Pi (hil-rig.md section 3.4).
#
#   pico-ctl flash <file.elf>    program over GPIO SWD with OpenOCD, verify, reset
#   pico-ctl reset               pulse RUN (BCM26) low for 100 ms
#   pico-ctl send <text...>      send one line on the control UART (GPIO14 TXD)
#   pico-ctl recv [seconds]      print control-UART lines for N seconds (default 2)
#   pico-ctl idle                every Pico-facing pin to a floating input
#   pico-ctl status              JSON: pin functions
#
#   --dry-run (anywhere) prints the commands instead of running them.
#
# STUBS for Stage E. The control-UART protocol is not designed yet (rig/esp/fake-lark/),
# so send/recv move raw lines only.
#
# Pin rule: the Pico is powered only from the S31's USB port, and a powered Pi must not
# drive an unpowered Pico. So SWCLK (BCM25), SWDIO (BCM24), RUN (BCM26) and the UART TX
# (BCM14) are floating inputs whenever no command is running. Every command that drives
# a pin restores that on exit, including on failure (EXIT trap). The 1 kOhm series
# resistors on the harness are the hardware half of the same rule.
set -euo pipefail
HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$HERE/lib.sh"
rig_load_conf

SWCLK="${PICO_SWCLK_GPIO:-25}"
SWDIO="${PICO_SWDIO_GPIO:-24}"
RUN_PIN="${PICO_RUN_GPIO:-26}"
TX_PIN="${PICO_UART_TX_GPIO:-14}"
UART="${RIG_UART:-/dev/serial0}"
BAUD="${RIG_UART_BAUD:-115200}"
DRIVER="${PICO_OPENOCD_DRIVER:-bcm2835gpio}"   # or linuxgpiod (slower, no /dev/gpiomem)
SWD_KHZ="${PICO_SWD_KHZ:-1000}"
DRY_RUN="${DRY_RUN:-0}"

args=()
for a in "$@"; do
  if [ "$a" = --dry-run ]; then DRY_RUN=1; else args+=("$a"); fi
done
set -- "${args[@]+"${args[@]}"}"

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

run() {
  if [ "$DRY_RUN" = 1 ]; then printf 'would run:' >&2; printf ' %q' "$@" >&2; printf '\n' >&2; return 0; fi
  "$@"
}

# ---------------------------------------------------------------- pins

# pin_set PIN MODE   MODE: in | low | a5 (mini-UART TXD1 on BCM14)
pin_set() {
  local pin="$1" mode="$2"
  if command -v pinctrl >/dev/null 2>&1 || [ "$DRY_RUN" = 1 ]; then
    case "$mode" in
      in)  run pinctrl set "$pin" ip pn ;;
      low) run pinctrl set "$pin" op dl ;;
      a5)  run pinctrl set "$pin" a5 ;;
    esac
  elif command -v raspi-gpio >/dev/null 2>&1; then
    case "$mode" in
      in)  run raspi-gpio set "$pin" ip pn ;;
      low) run raspi-gpio set "$pin" op dl ;;
      a5)  run raspi-gpio set "$pin" a5 ;;
    esac
  elif [ "$mode" = in ] && command -v gpioget >/dev/null 2>&1; then
    # Requesting a line as an input leaves it an input when released.
    run gpioget -c gpiochip0 "$pin" >/dev/null 2>&1 || run gpioget gpiochip0 "$pin" >/dev/null
  else
    die "no pin tool (pinctrl, raspi-gpio or gpiod) to set BCM$pin to $mode"
  fi
}

pins_idle() {
  local pin rc=0
  for pin in "$SWCLK" "$SWDIO" "$RUN_PIN" "$TX_PIN"; do
    pin_set "$pin" in || rc=1
  done
  return "$rc"
}

# Restore the idle state however the command ends.
idle_on_exit() { trap 'pins_idle || err "could not return the Pico pins to inputs"' EXIT; }

# ---------------------------------------------------------------- commands

peripheral_base() {
  local model
  model="$(tr -d '\0' 2>/dev/null < /proc/device-tree/model || true)"
  case "$model" in
    *"Pi 4"*|*"Compute Module 4"*) printf '0xFE000000\n' ;;
    *) printf '0x3F000000\n' ;;   # Pi 2/3, the rig's 3B
  esac
}

cmd_flash() {
  local elf="${1:-}"
  [ -n "$elf" ] || die "usage: pico-ctl flash <file.elf>"
  [ -f "$elf" ] || die "no such file: $elf"
  [ "$(head -c 4 "$elf" | od -An -tx1 | tr -d ' \n')" = 7f454c46 ] || die "$elf is not an ELF file"
  [ "$DRY_RUN" = 1 ] || command -v openocd >/dev/null 2>&1 || die "openocd not installed"
  local cfg=()
  case "$DRIVER" in
    bcm2835gpio)
      cfg=(-c "adapter driver bcm2835gpio"
           -c "bcm2835gpio peripheral_base ${PICO_PERIPHERAL_BASE:-$(peripheral_base)}"
           -c "adapter gpio swclk $SWCLK"
           -c "adapter gpio swdio $SWDIO") ;;
    linuxgpiod)
      cfg=(-c "adapter driver linuxgpiod"
           -c "adapter gpio swclk $SWCLK -chip 0"
           -c "adapter gpio swdio $SWDIO -chip 0") ;;
    *) die "PICO_OPENOCD_DRIVER must be bcm2835gpio or linuxgpiod" ;;
  esac
  idle_on_exit
  # RUN stays an input: OpenOCD resets the RP2040 over SWD (SYSRESETREQ), so it never
  # needs to drive BCM26.
  run openocd "${cfg[@]}" \
    -c "transport select swd" \
    -c "adapter speed $SWD_KHZ" \
    -f target/rp2040.cfg \
    -c "program {$elf} verify reset exit"
  ok "flashed $elf"
}

cmd_reset() {
  idle_on_exit
  pin_set "$RUN_PIN" low
  run sleep 0.1
  pin_set "$RUN_PIN" in   # the Pico's own pull-up on RUN releases reset
  ok "pulsed RUN"
}

cmd_send() {
  [ $# -gt 0 ] || die "usage: pico-ctl send <text...>"
  local line="$*"
  idle_on_exit
  pin_set "$TX_PIN" a5
  run python3 - "$UART" "$BAUD" "$line" <<'PY'
import sys
import serial
port, baud, line = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with serial.Serial(port, baud, timeout=1) as s:
    s.write((line + "\n").encode())
    s.flush()   # tcdrain: returns once the last byte left, before TX goes back to input
PY
}

cmd_recv() {
  local seconds="${1:-2}"
  case "$seconds" in *[!0-9]*|'') die "recv: seconds must be a whole number" ;; esac
  run python3 - "$UART" "$BAUD" "$seconds" <<'PY'
import sys, time
import serial
port, baud, seconds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
deadline = time.monotonic() + seconds
with serial.Serial(port, baud, timeout=0.2) as s:
    while time.monotonic() < deadline:
        line = s.readline()
        if line:
            sys.stdout.write(line.decode(errors="replace"))
            sys.stdout.flush()
PY
}

cmd_status() {
  local pin raw first=1
  printf '{"uart": "%s", "pins": {' "$(json_escape "$UART")"
  for pin in "$SWCLK" "$SWDIO" "$RUN_PIN" "$TX_PIN"; do
    raw=""
    if command -v pinctrl >/dev/null 2>&1; then raw="$(pinctrl get "$pin" 2>/dev/null || true)"
    elif command -v raspi-gpio >/dev/null 2>&1; then raw="$(raspi-gpio get "$pin" 2>/dev/null || true)"
    fi
    [ "$first" = 1 ] || printf ', '
    first=0
    printf '"%s": "%s"' "$pin" "$(json_escape "$raw")"
  done
  printf '}}\n'
}

command="${1:-}"
shift || true
case "$command" in
  flash) cmd_flash "$@" ;;
  reset) cmd_reset ;;
  send) cmd_send "$@" ;;
  recv) cmd_recv "$@" ;;
  idle) pins_idle && ok "Pico pins are inputs" ;;
  status) cmd_status ;;
  -h|--help|'') usage ;;
  *) usage >&2; exit 2 ;;
esac
