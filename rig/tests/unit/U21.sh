#!/usr/bin/env bash
# U21 — A2DP receiver pairs, connects, and appears as a PipeWire sink
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U21" "A2DP receiver connection" \
  "the receiver is paired/trusted and PipeWire exposes it as a sink we can play to" \
  "prove audio quality, or that it survives contention with SCO (that is Stage E)"

require_pi
DIR="$(artifact_dir U21-a2dp)"
MAC="$(inv bt_receiver_mac '')"
[ -n "$MAC" ] || die "bt_receiver_mac not set in rig/inventory.toml"

pi "bluetoothctl info $MAC" > "$DIR/device.txt" 2>&1 || true

grep -q "Device $MAC" "$DIR/device.txt" 2>/dev/null || grep -qi "Name:" "$DIR/device.txt" \
  || need_hardware "A2DP receiver $MAC" "Power it on and put it in pairing mode."

NAME="$(awk -F': ' '/Name:/{print $2; exit}' "$DIR/device.txt" | tr -d '\r')"
for k in Paired Trusted Connected; do
  v="$(awk -F': ' -v K="$k:" '$0 ~ K {print $2; exit}' "$DIR/device.txt" | tr -d '\r')"
  printf '  %-10s: %s\n' "$k" "$v" >&2
  eval "V_$k=\$v"
done
printf '  name      : %s\n' "$NAME" >&2

fail=0
[ "${V_Paired:-no}" = "yes" ]    || { err "not paired";    fail=1; }
[ "${V_Trusted:-no}" = "yes" ]   || { err "not trusted — it will not auto-reconnect"; fail=1; }
[ "${V_Connected:-no}" = "yes" ] || { err "not connected"; fail=1; }

# The sink is the thing that actually matters: pairing without a PipeWire sink means
# the profile negotiated but the audio path does not exist.
SINK="$(pi 'export XDG_RUNTIME_DIR=/run/user/$(id -u); pw-dump 2>/dev/null | python3 ~/rpi-lark-bridge/rig/analysis/find_node.py --prefix bluez_output' 2>/dev/null || true)"
if [ -n "$SINK" ]; then
  ok "PipeWire sink: $SINK"
else
  err "no bluez_output sink in the graph — profile connected but no audio path"
  fail=1
fi

# Transport UUID confirms which side we are. 0000110a means WE are the A2DP source.
TUUID="$(pi "busctl --system get-property org.bluez /org/bluez/hci0/dev_${MAC//:/_}/sep1/fd0 org.bluez.MediaTransport1 UUID 2>/dev/null" || true)"
printf '  transport : %s\n' "$TUUID" >&2
case "$TUUID" in
  *110a*) ok "transport UUID 0000110a — we are the A2DP SOURCE, correct" ;;
  *110b*) err "transport UUID 0000110b — we are the SINK; bluez5.roles is backwards"; fail=1 ;;
  *)      warn "could not read transport UUID (device may be idle)" ;;
esac

if [ "$fail" -eq 0 ]; then ok "U21 PASS"
  emit_result U21 PASS "$DIR" mac "$MAC" name "$NAME" sink "$SINK"
else err "U21 FAIL"; emit_result U21 FAIL "$DIR" mac "$MAC" name "$NAME"; fi
exit "$fail"
