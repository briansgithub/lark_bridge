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
PHONE_MAC="${PHONE_MAC:-5C:33:7B:CB:BF:C5}"
MAX_S="${MAX_S:-180}"
LABEL="${LABEL:-baseline}"

# Which controller carries which link. Until 2026-08-23 both were hci0 and the literal was
# harmless; with two radios the whole point of the experiment is that they DIFFER, so a
# hardcoded hci0 would poll the speaker's transport on the wrong adapter and read SCO
# counters off a controller carrying no call.
adapter_for() {
  python3 -c "
import sys
sys.path.insert(0, '$REPO/pi/bridged')
import btadapters
adapter = btadapters.adapter_for_device('$1')
print(adapter.hci if adapter else '')
" 2>/dev/null
}

# Auto-resolution is the default; the overrides exist because the coexistence experiment
# deliberately moves links between controllers, and a run must never be blocked by a
# resolution hiccup when the operator already knows the answer.
A2DP_HCI="${A2DP_HCI:-$(adapter_for "$A2DP_MAC")}"
CALL_HCI="${CALL_HCI:-$(adapter_for "$PHONE_MAC")}"
[ -n "$A2DP_HCI" ] || { printf '{"error":"no bond for A2DP device %s"}\n' "$A2DP_MAC"; exit 78; }
[ -n "$CALL_HCI" ] || { printf '{"error":"no bond for phone %s"}\n' "$PHONE_MAC"; exit 78; }

dev_path="/org/bluez/$A2DP_HCI/dev_${A2DP_MAC//:/_}"

transport_state() {
  # The fd object is a SIBLING of sep on this BlueZ, not a child: the real path is
  # /org/bluez/hci0/dev_XX/fd2, while an earlier BlueZ nested it under sep1. A regex that
  # required the sep level silently matched nothing and made transport_state() return
  # "gone" for a perfectly healthy stream -- which scored a live link as survival_s=1 and
  # would have been written up as "A2DP dies instantly". Accept both shapes.
  # The transport object number (sep1/sep2/...) varies by device, so discover it.
  local p
  for p in $(busctl --system tree org.bluez 2>/dev/null | grep -oE "${dev_path}(/sep[0-9]+)?/fd[0-9]+" | sort -u); do
    busctl --system get-property org.bluez "$p" org.bluez.MediaTransport1 State 2>/dev/null \
      | awk '{gsub(/"/,"");print $2}'
    return
  done
  echo "gone"
}

connected() {
  # Reads the device's own D-Bus object rather than asking bluetoothctl, which resolves a
  # MAC against whatever adapter it last called default -- the dongle, since 2026-08-23 --
  # and answers "Device not available" for a device connected on the other controller.
  # That made this precondition abort a run whose links were all perfectly up.
  if [ "$(busctl --system get-property org.bluez "$dev_path" org.bluez.Device1 Connected \
          2>/dev/null | awk '{print $2}')" = "true" ]; then
    echo yes
  else
    echo no
  fi
}

sco_rate() {
  local a b
  a="$(hciconfig "$CALL_HCI" 2>/dev/null | grep -oE 'sco:[0-9]+' | head -1 | tr -dc 0-9)"
  sleep 1
  b="$(hciconfig "$CALL_HCI" 2>/dev/null | grep -oE 'sco:[0-9]+' | head -1 | tr -dc 0-9)"
  echo $(( b - a ))
}

# --- preconditions: all three links must already be up ---------------------------------
[ "$(connected)" = "yes" ] || { echo '{"error":"a2dp not connected"}'; exit 78; }
hcitool -i "$CALL_HCI" con 2>/dev/null | grep -q eSCO || { echo '{"error":"no eSCO link - is a call routed to the bridge?"}'; exit 78; }

SINK="$(pw-dump 2>/dev/null | python3 "$REPO/rig/analysis/find_node.py" --prefix bluez_output --contains "${A2DP_MAC//:/_}")"
[ -n "$SINK" ] || SINK="$(pw-dump 2>/dev/null | python3 "$REPO/rig/analysis/find_node.py" --prefix bluez_output)"
[ -n "$SINK" ] || { echo '{"error":"no a2dp sink node"}'; exit 78; }

WORK=/tmp/rig; mkdir -p "$WORK"; cd "$WORK" || exit 1
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
# Both controllers, separately. E03 could say "the controller wedged" because there was
# only one; now a wedge on the speaker radio and a wedge on the call radio are different
# events with different consequences, and collapsing them throws that distinction away.
ALIVE=false
timeout 3 hcitool -i "$A2DP_HCI" cmd 0x03 0x0014 >/dev/null 2>&1 && ALIVE=true
CALL_ALIVE=false
timeout 3 hcitool -i "$CALL_HCI" cmd 0x03 0x0014 >/dev/null 2>&1 && CALL_ALIVE=true

systemctl --user stop a2dpsurv 2>/dev/null || true

printf '{"label":"%s","survival_s":%s,"reason":"%s","max_s":%s,"sco_pps_at_end":%s,"reassembly_delta":%s,"controller_alive":%s,"a2dp_hci":"%s","call_hci":"%s","call_controller_alive":%s,"same_controller":%s}\n' \
  "$LABEL" "$SURV" "$REASON" "$MAX_S" "$SCO_AT_END" "$(( ERR1 - ERR0 ))" "$ALIVE" \
  "$A2DP_HCI" "$CALL_HCI" "$CALL_ALIVE" \
  "$([ "$A2DP_HCI" = "$CALL_HCI" ] && echo true || echo false)"
