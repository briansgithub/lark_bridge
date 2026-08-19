#!/usr/bin/env bash
# Closed-loop health summary for the Windows -> Pi -> Pixel control path.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

DIR="$(artifact_dir doctor)"
fail=0

info "checking Ethernet SSH, Pi services, stable Lark identity, power, and Pixel ADB"

if pi true >/dev/null 2>&1; then
  ok "Pi reachable over non-interactive Ethernet SSH"
else
  err "Pi SSH unavailable"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  pi 'uname -a; cat /etc/os-release; pipewire --version; wireplumber --version; bluetoothctl --version' \
    > "$DIR/versions.txt" 2>&1 || fail=1
  pi 'systemctl is-active bluetooth bridge-btfw bridge-btwatchdog bridge-tuning; systemctl --user is-active pipewire wireplumber bridge-supervisor' \
    > "$DIR/services.txt" 2>&1 || fail=1
  pi "cd ~/rpi-lark-bridge && LARK_USB_ID='$(inv lark_usb_id 3547:0407)' LARK_USB_SERIAL='$(inv lark_usb_serial '')' bash rig/pi/measure/devices.sh" \
    > "$DIR/devices.txt" 2>&1 || fail=1
  pi 'vcgencmd get_throttled; vcgencmd measure_temp; vcgencmd measure_clock arm' \
    > "$DIR/power.txt" 2>&1 || fail=1
  grep -q 'throttled=0x0' "$DIR/power.txt" || { err "Pi reports throttling or undervoltage history"; fail=1; }
  grep -qE '^lark[[:space:]]+3547:0407[[:space:]]+[0-9]+' "$DIR/devices.txt" \
    || { err "Lark was not resolved by stable USB identity"; fail=1; }
fi

if require_phone >/dev/null 2>&1 && phone get-state > "$DIR/adb-state.txt" 2>&1; then
  ok "Pixel reachable over ADB"
else
  err "Pixel ADB unavailable"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  ok "doctor PASS"
  emit_result doctor PASS "$DIR"
else
  err "doctor FAIL"
  emit_result doctor FAIL "$DIR"
fi
info "evidence: $DIR"
exit "$fail"
