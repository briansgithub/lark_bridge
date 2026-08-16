#!/usr/bin/env bash
# U05 — USB subsystem health and device inventory
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U05" "USB subsystem health" \
  "the shared USB bus enumerates cleanly and is not resetting devices" \
  "prove the bus stays clean under audio LOAD — that is measured again during soaks"

require_pi
DIR="$(artifact_dir U05-usb)"

pi 'lsusb; echo "--- tree ---"; lsusb -t; echo "--- port paths ---"; \
    for d in /sys/bus/usb/devices/*/; do \
      [ -f "$d/idVendor" ] || continue; \
      printf "%-12s %s:%s %s\n" "$(basename "$d")" "$(cat "$d/idVendor")" "$(cat "$d/idProduct")" \
        "$(cat "$d/product" 2>/dev/null || echo -)"; \
    done' > "$DIR/usb.txt" 2>&1

sed 's/^/  /' "$DIR/usb.txt" >&2
echo >&2

# Everything on a Pi 3B hangs off one 480 Mbit/s link through the LAN9514 hub, which
# also carries Ethernet. Bus resets here would masquerade as audio dropouts later, so
# the count must be zero before any audio device is trusted.
RESETS="$(pi 'sudo dmesg | grep -ciE "usb .*(reset|disconnect)" || true')"
ERRORS="$(pi 'sudo dmesg --level=err,crit,alert,emerg 2>/dev/null | wc -l')"

printf '  usb resets/disconnects : %s\n' "$RESETS" >&2
printf '  kernel error lines     : %s\n' "$ERRORS" >&2

fail=0
if [ "${RESETS:-0}" -eq 0 ]; then ok "no USB resets or disconnects since boot"
else err "$RESETS USB reset/disconnect events — suspect power (U04) or a bad cable"; fail=1; fi

if [ "${ERRORS:-0}" -eq 0 ]; then ok "no kernel errors"
else warn "$ERRORS kernel error lines — see $DIR/dmesg-err.txt"
     pi 'sudo dmesg --level=err,crit,alert,emerg' > "$DIR/dmesg-err.txt" 2>&1 || true; fi

# Expected baseline with nothing attached: root hub + LAN9514 hub + its Ethernet.
COUNT="$(pi 'lsusb | wc -l')"
printf '  usb devices present    : %s\n' "$COUNT" >&2
if [ "${COUNT:-0}" -le 3 ]; then
  info "baseline only (root hub + LAN9514 hub + ethernet) — correct with no peripherals attached"
  info "audio peripherals are validated by U10-U12, which will pause if they are missing"
else
  ok "$COUNT USB devices — peripherals attached"
fi

if [ "$fail" -eq 0 ]; then ok "U05 PASS"; emit_result U05 PASS "$DIR" usb_resets "$RESETS" usb_devices "$COUNT"
else err "U05 FAIL"; emit_result U05 FAIL "$DIR" usb_resets "$RESETS" usb_devices "$COUNT"; fi
exit "$fail"
