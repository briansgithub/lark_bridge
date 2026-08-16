#!/usr/bin/env bash
# U30 — ADB transport to the Pixel from the control PC
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U30" "ADB transport" \
  "the agent can reach the phone and issue shell commands unattended" \
  "prove audio routing works, or that the transport survives a Mode 2 cable swap"

DIR="$(artifact_dir U30-adb)"

adb_bin >/dev/null 2>&1 || die "adb missing — run: rig setup-adb"
A="$(adb_bin)"

"$A" devices -l > "$DIR/devices.txt" 2>&1
N="$(grep -cE '\sdevice ' "$DIR/devices.txt" || true)"

if [ "${N:-0}" -eq 0 ]; then
  need_hardware "Pixel 7a over ADB" \
    "Either plug USB-C into THIS PC and accept the debugging prompt, or enable wireless debugging on abridge-village_5G."
fi

sed 's/^/  /' "$DIR/devices.txt" >&2

MODEL="$("$A" shell getprop ro.product.model 2>/dev/null | tr -d '\r')"
REL="$("$A" shell getprop ro.build.version.release 2>/dev/null | tr -d '\r')"
SDK="$("$A" shell getprop ro.build.version.sdk 2>/dev/null | tr -d '\r')"
SER="$("$A" shell getprop ro.serialno 2>/dev/null | tr -d '\r')"

printf '  model     : %s\n' "$MODEL" >&2
printf '  android   : %s (API %s)\n' "$REL" "$SDK" >&2
printf '  serial    : %s\n' "$SER" >&2

# Which transport is live matters: Mode 1 testing wants USB so the phone's radio can be
# switched off entirely; Mode 2 requires wireless because the Pico occupies the USB port.
TRANSPORT=usb
grep -qE '^adb-.*_adb-tls-connect' "$DIR/devices.txt" && TRANSPORT=wireless
grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+' "$DIR/devices.txt" && TRANSPORT=wireless
printf '  transport : %s\n' "$TRANSPORT" >&2
[ "$TRANSPORT" = wireless ] && info "wireless — correct for Mode 2; switch to USB-C for Mode 1 Bluetooth tests"

fail=0
[ "$MODEL" = "Pixel 7a" ] || { err "expected Pixel 7a, got '$MODEL'"; fail=1; }
[ "${SDK:-0}" -ge 34 ]    || { err "expected Android 14 (API 34+), got API $SDK"; fail=1; }

# Three fresh invocations: the agent's shell is stateless, so a one-shot success could
# hide an authorisation that does not persist.
for i in 1 2 3; do
  "$A" shell true >/dev/null 2>&1 || { err "repeat shell call $i failed"; fail=1; }
done
[ "$fail" -eq 0 ] && ok "3 consecutive shell invocations succeeded"

if [ "$fail" -eq 0 ]; then ok "U30 PASS"; emit_result U30 PASS "$DIR" model "$MODEL" android "$REL" sdk "$SDK" serial "$SER" transport "$TRANSPORT"
else err "U30 FAIL"; emit_result U30 FAIL "$DIR" model "$MODEL" transport "$TRANSPORT"; fi
exit "$fail"
