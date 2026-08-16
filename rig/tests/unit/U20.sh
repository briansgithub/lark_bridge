#!/usr/bin/env bash
# U20 — Bluetooth controller present, and advertising the roles WE need
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U20" "Bluetooth controller + advertised roles" \
  "hci0 is up and PipeWire has registered the profiles this project needs" \
  "prove any device can connect, or that SCO audio actually flows (that is E01/S1)"

require_pi
DIR="$(artifact_dir U20-bt)"

pi 'hciconfig -a; echo "--- features ---"; hciconfig hci0 features' > "$DIR/hci.txt" 2>&1
pi 'bluetoothctl show' > "$DIR/adapter.txt" 2>&1

STATE="$(grep -oE 'UP RUNNING|DOWN' "$DIR/hci.txt" | head -1)"
BD="$(grep -oE 'BD Address: [0-9A-F:]+' "$DIR/hci.txt" | head -1 | awk '{print $3}')"
SCOMTU="$(grep -oE 'SCO MTU: [0-9]+:[0-9]+' "$DIR/hci.txt" | head -1 | awk '{print $3}')"

printf '  adapter   : %s  %s\n' "$BD" "$STATE" >&2
printf '  SCO MTU   : %s\n' "$SCOMTU" >&2

fail=0
[ "$STATE" = "UP RUNNING" ] || { err "hci0 is $STATE"; fail=1; }

# Capability check. These decide the ceiling on call quality long before any phone is
# involved, so they are recorded here rather than rediscovered during E01.
for f in "SCO link" "transparent SCO" "extended SCO" "err. data report"; do
  if grep -q "<$f>" "$DIR/hci.txt"; then ok "controller supports <$f>"
  else warn "controller does NOT advertise <$f>"; fi
done
grep -q '<transparent SCO>' "$DIR/hci.txt" \
  && info "transparent SCO present -> mSBC wideband (16 kHz) is possible" \
  || warn "no transparent SCO -> CVSD 8 kHz would be the ceiling"

# The roles WE advertise. Measured convention (see PLAN.md 6.1): the config value names
# our own role, NOT the remote's.
echo >&2
declare -A WANT=( [0000110a]="Audio Source (we stream to headphones)"
                  [0000111e]="Handsfree (we are the HF unit for the phone)" )
for uuid in "${!WANT[@]}"; do
  if grep -qi "$uuid" "$DIR/adapter.txt"; then
    ok "advertising $uuid — ${WANT[$uuid]}"
  else
    err "NOT advertising $uuid — ${WANT[$uuid]}"
    err "  check bluez5.roles = [ a2dp_source hfp_hf hsp_hs ] and that"
    err "  monitor.bluez.seat-monitoring = disabled is in wireplumber.profiles (NOT properties)"
    fail=1
  fi
done
for uuid in 0000110b 0000111f; do
  grep -qi "$uuid" "$DIR/adapter.txt" \
    && warn "also advertising $uuid — we should not be a sink/gateway; check bluez5.roles"
done

if [ "$fail" -eq 0 ]; then ok "U20 PASS"; emit_result U20 PASS "$DIR" bd_addr "$BD" sco_mtu "$SCOMTU"
else err "U20 FAIL"; emit_result U20 FAIL "$DIR" bd_addr "$BD"; fi
exit "$fail"
