#!/usr/bin/env bash
# selftest.sh - READ-ONLY health check of the rig Pi. Prints one JSON object.
#
#   rig-selftest            run as the rig user (PipeWire checks need its session)
#
# Exit 0 when no check failed, 1 otherwise. It changes nothing: no Bluetooth power, no
# pins, no lease. The plan's stop rule "a rig self-test failure" keys on this exit code.
#
# Each check is {"status": pass|warn|fail|skip, "detail": "..."}.
set -euo pipefail
HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$HERE/lib.sh"
rig_load_conf
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

RESULTS="$(mktemp)"
trap 'rm -f "$RESULTS"' EXIT

check() {   # check NAME STATUS DETAIL
  printf '%s\t%s\t%s\n' "$1" "$2" "${3//$'\n'/ }" >> "$RESULTS"
}
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- identity

reasons="$(product_card_reasons ""; live_overlay_reasons)"
if [ -z "$reasons" ]; then check not_product_card pass "no overlayroot or LarkBridge install"
else check not_product_card fail "$reasons"; fi

if [ ! -r "$RIG_CONF" ]; then
  check config fail "$RIG_CONF missing: run provision.sh"
elif is_bdaddr "${RIG_BT_ADDR:-}"; then
  check config pass "$RIG_CONF, pinned controller ${RIG_BT_ADDR}"
else
  check config fail "RIG_BT_ADDR not set in $RIG_CONF: re-run provision.sh --bt-addr"
fi

# ---------------------------------------------------------------- bluetooth

if ! have btmgmt; then
  check controller_present fail "btmgmt not installed (bluez)"
else
  controllers="$(bt_controllers || true)"
  count="$(printf '%s' "$controllers" | grep -c . || true)"
  if is_bdaddr "${RIG_BT_ADDR:-}" && grep -q " ${RIG_BT_ADDR}\$" <<<"$controllers"; then
    check controller_present pass "$(grep " ${RIG_BT_ADDR}\$" <<<"$controllers")"
  else
    check controller_present fail "pinned ${RIG_BT_ADDR:-<unset>} not in: ${controllers:-none}"
  fi
  if [ "$count" -le 1 ]; then check single_controller pass "$count controller(s)"
  else check single_controller warn "$count controllers (the USB-BT500 should be unbound): $controllers"; fi
fi
if have systemctl; then
  state="$(systemctl is-active larkbridge-rig-bt-pin.service 2>/dev/null || true)"
  if [ "$state" = active ]; then check bt_pin_service pass active
  else check bt_pin_service fail "larkbridge-rig-bt-pin.service is ${state:-unknown}"; fi
else
  check bt_pin_service skip "no systemctl"
fi

# ---------------------------------------------------------------- tools

missing=()
for tool in bluetoothctl btmon btmgmt busctl pw-record pw-dump wpctl aplay arecord sox openocd python3 rig-lease; do
  have "$tool" || missing+=("$tool")
done
if have python3 && ! python3 -c 'import serial, dbus, gi' 2>/dev/null; then
  missing+=("python3-serial/python3-dbus/python3-gi")
fi
if [ "${#missing[@]}" = 0 ]; then check tools pass "all present"
else check tools fail "missing: ${missing[*]}"; fi

rp2040_cfg=""
for path in /usr/share/openocd/scripts/target/rp2040.cfg /usr/local/share/openocd/scripts/target/rp2040.cfg; do
  [ -f "$path" ] && { rp2040_cfg="$path"; break; }
done
if [ -n "$rp2040_cfg" ]; then check openocd_rp2040 pass "$rp2040_cfg"
else check openocd_rp2040 fail "target/rp2040.cfg not found in the OpenOCD scripts"; fi

# ---------------------------------------------------------------- audio

if have pw-dump; then
  if pw-dump 2>/dev/null | grep -q "\"node.name\": \"${RIG_FAKE_CAR_SINK:-larkbridge_fake_car}\""; then
    check fake_car_sink pass "${RIG_FAKE_CAR_SINK:-larkbridge_fake_car} present"
  else
    check fake_car_sink fail "recording tap not found (PipeWire not running for $(id -un)?)"
  fi
else
  check fake_car_sink skip "pw-dump not installed"
fi

# ---------------------------------------------------------------- Pico UART, ssh

uart="${RIG_UART:-/dev/serial0}"
if [ ! -e "$uart" ]; then check pico_uart fail "$uart missing (enable_uart=1 not applied?)"
elif grep -Eq '(^|[[:space:]])console=(serial0|ttyS0|ttyAMA0)' /proc/cmdline 2>/dev/null; then
  check pico_uart fail "kernel console is still on the serial port"
else check pico_uart pass "$uart -> $(readlink -f "$uart")"; fi

dropin=/etc/ssh/sshd_config.d/10-larkbridge-rig.conf
if [ -r "$dropin" ] && grep -q '^PasswordAuthentication no' "$dropin"; then
  check ssh_keys_only pass "$dropin"
else check ssh_keys_only fail "$dropin missing"; fi

# ---------------------------------------------------------------- health

read -r avail_kb use_pct < <(df -Pk / | awk 'NR == 2 { print $4, $5 }')
avail_mb=$((avail_kb / 1024))
if [ "$avail_mb" -lt 1024 ]; then check disk fail "${avail_mb} MB free on / (${use_pct} used)"
elif [ "$avail_mb" -lt 2048 ]; then check disk warn "${avail_mb} MB free on / (${use_pct} used)"
else check disk pass "${avail_mb} MB free on / (${use_pct} used)"; fi

temp=""
if have vcgencmd; then
  temp="$(vcgencmd measure_temp 2>/dev/null | sed -n "s/^temp=\([0-9.]*\).*/\1/p")"
fi
if [ -z "$temp" ] && [ -r /sys/class/thermal/thermal_zone0/temp ]; then
  temp="$(awk '{ printf "%.1f", $1 / 1000 }' /sys/class/thermal/thermal_zone0/temp)"
fi
if [ -z "$temp" ]; then check temperature skip "no sensor"
else
  t="${temp%.*}"
  if [ "$t" -ge 80 ]; then check temperature fail "${temp} C"
  elif [ "$t" -ge 70 ]; then check temperature warn "${temp} C"
  else check temperature pass "${temp} C"; fi
fi

if have vcgencmd; then
  thr="$(vcgencmd get_throttled 2>/dev/null | sed -n 's/^throttled=//p')"
  if [ -z "$thr" ]; then check throttling skip "vcgencmd gave no answer"
  elif (( (thr & 0xF) != 0 )); then check throttling fail "throttled=$thr (under-voltage/throttling NOW)"
  elif (( (thr & 0xF0000) != 0 )); then check throttling warn "throttled=$thr (happened since boot)"
  else check throttling pass "throttled=$thr"; fi
else
  check throttling skip "vcgencmd not installed"
fi

# ---------------------------------------------------------------- lease (informational)

lease_json='null'
if have rig-lease; then lease_json="$(rig-lease status 2>/dev/null || printf 'null')"
elif [ -f "$HERE/rig_lease.py" ]; then lease_json="$(python3 "$HERE/rig_lease.py" status 2>/dev/null || printf 'null')"
fi

python3 - "$RESULTS" "$lease_json" "$(hostname)" <<'PY'
import json, sys
from datetime import datetime, timezone
results, lease, host = sys.argv[1], sys.argv[2], sys.argv[3]
checks = {}
with open(results, encoding="utf-8") as handle:
    for line in handle:
        name, status, detail = (line.rstrip("\n").split("\t", 2) + ["", ""])[:3]
        checks[name] = {"status": status, "detail": detail}
try:
    lease = json.loads(lease)
except ValueError:
    lease = None
failed = sorted(k for k, v in checks.items() if v["status"] == "fail")
print(json.dumps({
    "ok": not failed,
    "failed": failed,
    "host": host,
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "checks": checks,
    "lease": lease,
}, sort_keys=True))
sys.exit(1 if failed else 0)
PY
