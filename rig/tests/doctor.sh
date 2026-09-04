#!/usr/bin/env bash
# Closed-loop health summary for the Windows -> Pi -> Pixel control path.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

DIR="$(artifact_dir doctor)"
fail=0

info "checking Ethernet SSH, Pi services, microphone policy, power, and Pixel ADB"

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
  pi "cd ~/rpi-lark-bridge && LARK_USB_ID='$(inv lark_usb_id 3547:0407)' LARK_USB_SERIAL='$(inv lark_usb_serial '')' FIFINE_K053_USB_ID='$(inv fifine_k053_usb_id 0c76:161f)' FIFINE_K053_USB_SERIAL='$(inv fifine_k053_usb_serial '')' FIFINE_K053_PORT='$(inv fifine_k053_port_path '')' FIFINE_USB_ID='$(inv fifine_usb_id 0c76:161e)' FIFINE_USB_SERIAL='$(inv fifine_usb_serial '')' FIFINE_PORT='$(inv fifine_port_path '')' bash rig/pi/measure/devices.sh" \
    > "$DIR/devices.txt" 2>&1 || fail=1
  pi 'cat "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bridge-status.json"' \
    > "$DIR/bridge-status.json" 2>&1 || fail=1
  pi 'vcgencmd get_throttled; vcgencmd measure_temp; vcgencmd measure_clock arm' \
    > "$DIR/power.txt" 2>&1 || fail=1
  grep -q 'throttled=0x0' "$DIR/power.txt" || { err "Pi reports throttling or undervoltage history"; fail=1; }
  PY="$(command -v python3 2>/dev/null || command -v py 2>/dev/null || true)"
  if [ -z "$PY" ] || ! "$PY" -c '
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
m = s.get("microphone") or {}
selected = m.get("selected") or {}
candidates = m.get("candidates") or []
if not selected.get("id") or not selected.get("node"):
    raise SystemExit("no usable selected microphone")
if selected.get("id") != "lark-a1" and any(
    c.get("id") == "lark-a1" and c.get("state") in {"usable", "selected"}
    for c in candidates
):
    raise SystemExit("microphone priority inversion")
' "$DIR/bridge-status.json"; then
    err "microphone selection is absent, ambiguous, or violates priority"
    fail=1
  fi
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
