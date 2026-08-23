#!/usr/bin/env bash
# Count A2DP dropouts against E03's acceptance bar. Runs ON THE PI, emits a JSON manifest.
#
#   Pi -> SBC -> air -> A2DP receiver -> 3.5 mm line-out -> dongle B pink -> this capture
#
# THIS CAPTURES; IT DOES NOT ANALYSE. glitch_detect.py's own docstring says a 20 s clip
# takes "a couple of minutes on a Pi 3", so analysing a 60-minute run here would take
# longer than the run itself and would compete with the graph it is measuring. Segments
# are pulled to the control PC and analysed there.
#
# WHY SEGMENTS RATHER THAN ONE LONG WAV
# -------------------------------------
# The bar is "<1 dropout/MINUTE over 60 minutes", which is a rate, not a total. One
# 345 MB WAV yields a single number and hides everything interesting: whether dropouts
# cluster, whether they start after N minutes, whether they coincide with a transport
# state change. Per-segment counts give the rate directly and keep each file small enough
# to copy while the run is still going.
#
# Link state is sampled per segment for the same reason E03 wanted btmon evidence: a
# dropout burst that lines up with an AVDTP transport leaving `active` is a different
# finding from one that does not.
#
#   DURATION=600 SEG=30 LABEL=orientation-a ./a2dp-dropouts.sh
#
# Requires the A2DP link UP. Deliberately does NOT establish it -- establishing during
# active SCO is its own failure mode (E07), so making that implicit would confound runs.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=./devices.sh
. "$HERE/devices.sh"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

A2DP_MAC="${A2DP_MAC:-50:D7:1B:74:34:D6}"
PHONE_MAC="${PHONE_MAC:-5C:33:7B:CB:BF:C5}"
DURATION="${DURATION:-600}"
SEG="${SEG:-30}"
LABEL="${LABEL:-run}"
TONE="${TONE:-1000}"
# Measured by a2dp-cal.sh on this fixture: gain 30 lands the tone at -19.73 dBFS peak
# against a -89.6 dBFS floor, with no clipping at any sweep point.
CAPTURE_GAIN="${CAPTURE_GAIN:-30}"
OUTDIR="${OUTDIR:-/tmp/rig/a2dp-dropouts/$LABEL}"

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

rig_resolve
B="${DONGLE_B_CARD:-}"
[ -n "$B" ] || { printf '{"error":"dongle B (instrument) not present"}\n'; exit 78; }
# A combo-jack adapter drops its capture interface when a plug is inserted
# (docs/hardware/loopback-rig.md). Silently measuring nothing is the worst outcome here.
card_has_capture "$B" || { printf '{"error":"instrument card %s has no capture interface"}\n' "$B"; exit 78; }

SINK="$(pw-dump 2>/dev/null | python3 "$REPO/rig/analysis/find_node.py" --prefix bluez_output --contains "${A2DP_MAC//:/_}")"
[ -n "$SINK" ] || { printf '{"error":"no A2DP sink node for %s"}\n' "$A2DP_MAC"; exit 78; }

dev_path="/org/bluez/$A2DP_HCI/dev_${A2DP_MAC//:/_}"

transport_state() {
  # The fd object is a SIBLING of sep on this BlueZ, not a child: the real path is
  # /org/bluez/hci0/dev_XX/fd2, while an earlier BlueZ nested it under sep1. A regex that
  # required the sep level silently matched nothing and made transport_state() return
  # "gone" for a perfectly healthy stream -- which scored a live link as survival_s=1 and
  # would have been written up as "A2DP dies instantly". Accept both shapes.
  local p
  for p in $(busctl --system tree org.bluez 2>/dev/null \
             | grep -oE "${dev_path}(/sep[0-9]+)?/fd[0-9]+" | sort -u); do
    busctl --system get-property org.bluez "$p" org.bluez.MediaTransport1 State 2>/dev/null \
      | awk '{gsub(/"/,"");print $2}'
    return
  done
  echo "gone"
}

connected() {
  busctl --system get-property org.bluez "$dev_path" org.bluez.Device1 Connected 2>/dev/null \
    | awk '{print $2}'
}

sco_count() {
  [ -n "$CALL_HCI" ] || { echo 0; return; }
  hciconfig "$CALL_HCI" 2>/dev/null | grep -oE 'sco:[0-9]+' | head -1 | tr -dc 0-9
}

mkdir -p "$OUTDIR"
rm -f "$OUTDIR"/seg-*.wav "$OUTDIR/segments.jsonl"

STIM="$OUTDIR/stimulus.wav"
# One tone file long enough for the whole run: regenerating per segment would put a gap
# at every boundary and each gap would read as a dropout.
python3 "$REPO/tools/audio/tone_gen.py" --mode sine --freq "$TONE" \
  --seconds "$((DURATION + 60))" --rate 48000 --channels 2 --dbfs -6 --out "$STIM" >/dev/null

amixer -c "$B" cset numid=8 "$CAPTURE_GAIN" >/dev/null 2>&1
# Unity on the sink: a PipeWire volume below 1.0 silently costs headroom and would be
# misread as a weak line-out (same reasoning as a2dp-cal.sh).
wpctl set-volume "$SINK" 1.0 >/dev/null 2>&1 || true

systemctl --user stop a2dpdrop 2>/dev/null || true
systemd-run --user --unit=a2dpdrop --collect pw-play --target "$SINK" "$STIM" >/dev/null 2>&1
# A2DP buffers 150-250 ms and the sink ramps on stream start; a2dp-cal.sh measured 2 s of
# settle as not enough, so the first segment starts after the same 4 s it uses.
sleep 4

SEGMENTS=0
START="$(date +%s)"
SCO_PREV="$(sco_count)"

while [ "$(( $(date +%s) - START ))" -lt "$DURATION" ]; do
  IDX="$(printf '%04d' "$SEGMENTS")"
  WAV="$OUTDIR/seg-$IDX.wav"
  T0="$(date +%s)"
  ST_BEFORE="$(transport_state)"
  arecord -D "plughw:$B,0" -f S16_LE -r 48000 -c 1 -d "$SEG" "$WAV" 2>/dev/null
  ST_AFTER="$(transport_state)"
  CN="$(connected)"
  SCO_NOW="$(sco_count)"
  printf '{"index":%s,"t":%s,"wav":"%s","transport_before":"%s","transport_after":"%s","connected":%s,"sco_delta":%s}\n' \
    "$SEGMENTS" "$T0" "$WAV" "$ST_BEFORE" "$ST_AFTER" "${CN:-false}" \
    "$(( ${SCO_NOW:-0} - ${SCO_PREV:-0} ))" >> "$OUTDIR/segments.jsonl"
  SCO_PREV="$SCO_NOW"
  SEGMENTS=$(( SEGMENTS + 1 ))
  # A disconnected sink cannot produce audio, so continuing would bank silence and score
  # it as a perfect run. Stop and let the manifest say why.
  if [ "$CN" != "true" ]; then
    break
  fi
done

systemctl --user stop a2dpdrop 2>/dev/null || true
ELAPSED="$(( $(date +%s) - START ))"

ALIVE=false
timeout 3 hcitool -i "$A2DP_HCI" cmd 0x03 0x0014 >/dev/null 2>&1 && ALIVE=true
CALL_ALIVE=false
[ -n "$CALL_HCI" ] && timeout 3 hcitool -i "$CALL_HCI" cmd 0x03 0x0014 >/dev/null 2>&1 && CALL_ALIVE=true
SCO_ACTIVE=false
[ -n "$CALL_HCI" ] && hcitool -i "$CALL_HCI" con 2>/dev/null | grep -q eSCO && SCO_ACTIVE=true

printf '{"label":"%s","outdir":"%s","sink":"%s","a2dp_hci":"%s","call_hci":"%s","same_controller":%s,"sco_active":%s,"requested_s":%s,"elapsed_s":%s,"segment_s":%s,"segments":%s,"capture_gain":%s,"tone_hz":%s,"a2dp_controller_alive":%s,"call_controller_alive":%s}\n' \
  "$LABEL" "$OUTDIR" "$SINK" "$A2DP_HCI" "${CALL_HCI:-none}" \
  "$([ "$A2DP_HCI" = "${CALL_HCI:-none}" ] && echo true || echo false)" \
  "$SCO_ACTIVE" "$DURATION" "$ELAPSED" "$SEG" "$SEGMENTS" "$CAPTURE_GAIN" "$TONE" \
  "$ALIVE" "$CALL_ALIVE"
