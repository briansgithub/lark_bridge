#!/usr/bin/env bash
# Measure how long an A2DP stream survives once SCO is active. Runs ON THE PI.
#
# Emits JSON. The number that matters is survival_s.
#
# WHY A NUMBER: "it broke after about 20 seconds" cannot tell you whether a mitigation
# helped. Every proposed fix for the coexistence failure (AVDTP timeouts, bitpool caps,
# buffering) needs a baseline distribution to be measured against, or we are just
# guessing and calling it engineering.
#
# Method: with A2DP + HFP + SCO all established, start a continuous stream and poll the
# AVDTP transport state once a second until it stops being "active" or the device
# disconnects. Report the elapsed time.
#
# Requires all three links UP before running -- it does not establish them, because the
# act of establishing is itself a variable (see E07: paging a device during active SCO
# is a separate failure mode).

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

A2DP_MAC="${A2DP_MAC:-98:47:44:CD:73:DE}"
MAX_S="${MAX_S:-180}"
LABEL="${LABEL:-baseline}"

dev_path="/org/bluez/hci0/dev_${A2DP_MAC//:/_}"

transport_state() {
  # The transport object number (sep1/sep2/...) varies by device, so discover it.
  local p
  for p in $(busctl --system tree org.bluez 2>/dev/null | grep -oE "${dev_path}/sep[0-9]+/fd[0-9]+" | sort -u); do
    busctl --system get-property org.bluez "$p" org.bluez.MediaTransport1 State 2>/dev/null \
      | awk '{gsub(/"/,"");print $2}'
    return
  done
  echo "gone"
}

connected() {
  bluetoothctl info "$A2DP_MAC" 2>/dev/null | awk -F': ' '/Connected:/{print $2}' | tr -d '\r'
}

sco_rate() {
  local a b
  a="$(hciconfig hci0 2>/dev/null | grep -oE 'sco:[0-9]+' | head -1 | tr -dc 0-9)"
  sleep 1
  b="$(hciconfig hci0 2>/dev/null | grep -oE 'sco:[0-9]+' | head -1 | tr -dc 0-9)"
  echo $(( b - a ))
}

# --- preconditions: all three links must already be up ---------------------------------
[ "$(connected)" = "yes" ] || { echo '{"error":"a2dp not connected"}'; exit 78; }
hcitool con 2>/dev/null | grep -q eSCO || { echo '{"error":"no eSCO link - is a call routed to the bridge?"}'; exit 78; }

SINK="$(pw-dump 2>/dev/null | python3 "$REPO/rig/analysis/find_node.py" --prefix bluez_output --contains "${A2DP_MAC//:/_}")"
[ -n "$SINK" ] || SINK="$(pw-dump 2>/dev/null | python3 "$REPO/rig/analysis/find_node.py" --prefix bluez_output)"
[ -n "$SINK" ] || { echo '{"error":"no a2dp sink node"}'; exit 78; }

WORK=/tmp/rig; mkdir -p "$WORK"; cd "$WORK"
TONE="$WORK/survival.wav"
[ -f "$TONE" ] || python3 "$REPO/tools/audio/tone_gen.py" --mode pips --freq 1000 \
  --seconds "$((MAX_S + 30))" --rate 48000 --channels 2 --dbfs -6 --out "$TONE" >/dev/null

ERR0="$(dmesg 2>/dev/null | grep -c 'Frame reassembly failed')"
systemctl --user stop a2dpsurv 2>/dev/null || true
systemd-run --user --unit=a2dpsurv --collect pw-play --target "$SINK" "$TONE" >/dev/null 2>&1

START="$(date +%s)"
SURV=""
REASON=""
SCO_AT_END=0

for (( i=0; i<MAX_S; i++ )); do
  sleep 1
  ST="$(transport_state)"
  CN="$(connected)"
  if [ "$CN" != "yes" ]; then
    SURV=$(( $(date +%s) - START )); REASON="device_disconnected"; break
  fi
  if [ "$ST" != "active" ]; then
    SURV=$(( $(date +%s) - START )); REASON="transport_${ST}"; break
  fi
done

[ -n "$SURV" ] || { SURV="$MAX_S"; REASON="survived_full_duration"; }
SCO_AT_END="$(sco_rate)"
ERR1="$(dmesg 2>/dev/null | grep -c 'Frame reassembly failed')"
ALIVE=false
timeout 3 hcitool -i hci0 cmd 0x03 0x0014 >/dev/null 2>&1 && ALIVE=true

systemctl --user stop a2dpsurv 2>/dev/null || true

printf '{"label":"%s","survival_s":%s,"reason":"%s","max_s":%s,"sco_pps_at_end":%s,"reassembly_delta":%s,"controller_alive":%s}\n' \
  "$LABEL" "$SURV" "$REASON" "$MAX_S" "$SCO_AT_END" "$(( ERR1 - ERR0 ))" "$ALIVE"
