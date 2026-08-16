#!/usr/bin/env bash
# Spike S3 — Can ONE Broadcom radio carry HFP/eSCO and A2DP at the same time?
#
# This is risk R1, the highest-scoring risk in the project. eSCO reserves periodic slots;
# A2DP's ACL stream has to fit around them; and on the Pi 3 the Wi-Fi radio shares the die.
# Nobody publishes how the BCM43438 behaves under that load, so it must be measured.
#
# This is a SMOKE TEST, not the acceptance test. It answers "is this obviously broken?" in
# ten minutes so we know whether Mode 1 or Mode 1W is the product default (ADR-0001).
# The full acceptance runs are test-matrix rows E1-E6 in PLAN.md §9.
#
# It combines objective counters we can read on the Pi (PipeWire XRUNs, A2DP transport
# state changes, btmon SCO/ACL traffic) with a human dropout count, because the thing that
# actually matters — audible gaps at the headphones — happens after the radio and cannot
# be observed from this side.
#
# Usage:
#   ./tests/stage-e-concurrent/s3-coexistence-smoke.sh --sink <a2dp-sink-name>
#   ./tests/stage-e-concurrent/s3-coexistence-smoke.sh --duration 600 --wifi-off

set -euo pipefail
# shellcheck source=../../scripts/lib/common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/lib/common.sh"

DURATION=600
SINK=""
WIFI_OFF=0

while [ $# -gt 0 ]; do
  case "$1" in
    --duration) DURATION="${2:?}"; shift 2 ;;
    --sink)     SINK="${2:?}";     shift 2 ;;
    --wifi-off) WIFI_OFF=1;        shift ;;
    -h|--help)  sed -n '2,25p' "$0"; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
done

require_linux
require_not_root
require_cmd wpctl pw-dump pw-play python3 btmon
require_bt_adapter

DIR="$(artifact_dir "s3-coexistence")"
info "artifacts: $DIR"

# ---------------------------------------------------------------- 1. find the A2DP sink

if [ -z "$SINK" ]; then
  SINK="$(pw-dump 2>/dev/null \
    | python3 -c '
import json,sys
for o in json.load(sys.stdin):
    p = (o.get("info") or {}).get("props") or {}
    if p.get("media.class") == "Audio/Sink" and "bluez_output" in str(p.get("node.name","")):
        print(p["node.name"]); break
' || true)"
fi
[ -n "$SINK" ] || die "no A2DP sink found. Connect the headphones, or pass --sink <name>.
Hint: wpctl status  |  pw-dump | grep bluez_output"
ok "A2DP sink: $SINK"

# ---------------------------------------------------------------- 2. radio environment

if [ "$WIFI_OFF" -eq 1 ]; then
  info "disabling 2.4 GHz Wi-Fi for this run (coexistence variable — PLAN.md §6.7)"
  sudo rfkill block wifi || warn "could not block wifi; continuing"
  sleep 2
fi
capture "$DIR" "rfkill"      rfkill list
capture "$DIR" "iw-dev"      iw dev
capture "$DIR" "hciconfig"   hciconfig -a
printf 'wifi_disabled: %s\n' "$([ "$WIFI_OFF" -eq 1 ] && echo yes || echo no)" > "$DIR/conditions.txt"

# ---------------------------------------------------------------- 3. baseline counters

sample_counters() {
  pw-dump 2>/dev/null | python3 -c '
import json,sys
xruns=0
for o in json.load(sys.stdin):
    info=o.get("info") or {}
    for k,v in (info.get("props") or {}).items():
        if k.endswith("xrun-count") or k=="node.xrun-count":
            try: xruns+=int(v)
            except (TypeError,ValueError): pass
    st=info.get("state")
    if st=="error": xruns+=1
print(xruns)
' 2>/dev/null || echo 0
}

TONE="$DIR/tone.wav"
python3 "$BRIDGE_REPO_ROOT/tools/audio/tone_gen.py" \
  --freq 1000 --seconds 30 --rate 48000 --channels 2 --out "$TONE" \
  || die "could not generate the test tone"

XRUN_BEFORE="$(sample_counters)"
info "baseline xrun counter: $XRUN_BEFORE"

# ---------------------------------------------------------------- 4. capture + stream

info "starting btmon capture"
sudo btmon -w "$DIR/hci.btsnoop" > "$DIR/btmon.txt" 2>&1 &
BTMON_PID=$!
cleanup_pid_on_exit "$BTMON_PID"
sleep 2

info "starting continuous A2DP playback to $SINK"
( while true; do pw-play --target "$SINK" "$TONE" >/dev/null 2>&1 || sleep 1; done ) &
PLAY_PID=$!
cleanup_pid_on_exit "$PLAY_PID"
sleep 5

prompt_user "A 1 kHz tone should now be playing in the headphones over A2DP.

  1. Confirm you can hear it cleanly BEFORE starting a call. If it is already
     glitching, stop — that is an A2DP problem, not a coexistence problem, and
     spike S3 is not the right test. Run stage D first.
  2. Now place a call on the Pixel (or start a Discord voice call).
  3. LISTEN to the tone for the next $((DURATION / 60)) minutes and COUNT audible
     dropouts. A dropout is any gap, stutter or burst of noise in the tone.

Keep the call up for the whole run."

countdown "$DURATION" "HFP + A2DP concurrent"

kill "$PLAY_PID" 2>/dev/null || true
sudo kill "$BTMON_PID" 2>/dev/null || true
sleep 1

XRUN_AFTER="$(sample_counters)"
capture "$DIR" "wpctl-status-final" wpctl status
capture "$DIR" "journal-bluetooth"  journalctl -u bluetooth --no-pager -n 300

# ---------------------------------------------------------------- 5. objective signals

count_matches() { grep -c -- "$1" "$DIR/btmon.txt" 2>/dev/null || true; }

SCO_TX="$(count_matches 'SCO Data TX')"
SCO_RX="$(count_matches 'SCO Data RX')"
SYNC_COMPLETE="$(count_matches 'Synchronous Connect Complete')"
DISCONNECTS="$(count_matches 'Disconnect Complete')"
FLOW_STALLS="$(count_matches 'Number of Completed Packets')"
XRUN_DELTA=$(( XRUN_AFTER - XRUN_BEFORE ))

echo
prompt_user "How many audible dropouts did you count? (a number, then Enter)"
read -r HUMAN_DROPOUTS
HUMAN_DROPOUTS="${HUMAN_DROPOUTS:-0}"
case "$HUMAN_DROPOUTS" in (*[!0-9]*|'') warn "not a number; recording 0"; HUMAN_DROPOUTS=0 ;; esac

DROPOUTS_PER_MIN="$(python3 -c "print(round($HUMAN_DROPOUTS / max($DURATION/60, 1), 2))")"

{
  printf 'S3 HFP + A2DP coexistence smoke test\n'
  printf '====================================\n'
  printf 'duration_s              : %s\n' "$DURATION"
  printf 'wifi_disabled           : %s\n' "$([ "$WIFI_OFF" -eq 1 ] && echo yes || echo no)"
  printf 'a2dp_sink               : %s\n' "$SINK"
  printf 'sco_connect_complete    : %s\n' "$SYNC_COMPLETE"
  printf 'sco_data_tx             : %s\n' "$SCO_TX"
  printf 'sco_data_rx             : %s\n' "$SCO_RX"
  printf 'disconnect_complete     : %s\n' "$DISCONNECTS"
  printf 'flow_control_events     : %s\n' "$FLOW_STALLS"
  printf 'pipewire_xrun_delta     : %s\n' "$XRUN_DELTA"
  printf 'audible_dropouts        : %s\n' "$HUMAN_DROPOUTS"
  printf 'audible_dropouts_per_min: %s\n' "$DROPOUTS_PER_MIN"
} | tee "$DIR/analysis.txt"

# ---------------------------------------------------------------- 6. verdict

echo
if [ "$SYNC_COMPLETE" -eq 0 ]; then
  err "VERDICT: INVALID — no SCO link was established, so nothing was tested."
  err "  Run spike S1 and S2 first."
  VERDICT="INVALID"
elif python3 -c "import sys; sys.exit(0 if $DROPOUTS_PER_MIN < 1 else 1)"; then
  ok "VERDICT: PASS — under 1 dropout/min. Mode 1 stays the primary architecture."
  ok "  Proceed to the full acceptance runs E1-E6 before trusting this."
  VERDICT="PASS"
elif python3 -c "import sys; sys.exit(0 if $DROPOUTS_PER_MIN <= 10 else 1)"; then
  warn "VERDICT: PARTIAL — $DROPOUTS_PER_MIN dropouts/min."
  warn "  Per ADR-0001, Mode 1W becomes the shipped default and Mode 1 stays supported."
  warn "  Before accepting: re-run with --wifi-off, then compare. Wi-Fi coexistence on the"
  warn "  shared die is the first thing to rule out."
  VERDICT="PARTIAL"
else
  err "VERDICT: FAIL — $DROPOUTS_PER_MIN dropouts/min. Single-radio coexistence is not viable."
  err "  Mode 1W becomes the product; Mode 1 is documented as a measured limitation."
  err "  Write it up in docs/experiments/E03 WITH the btmon capture as evidence."
  err "  Do NOT add a second Bluetooth adapter — that was explicitly ruled out."
  VERDICT="FAIL"
fi

printf 'verdict: %s\n' "$VERDICT" >> "$DIR/analysis.txt"
[ "$WIFI_OFF" -eq 1 ] && { info "re-enabling Wi-Fi"; sudo rfkill unblock wifi || true; }

info "Record this run in docs/experiments/E03-hfp-a2dp-coexistence.md — verdict=$VERDICT dir=$DIR"
case "$VERDICT" in PASS) exit 0 ;; PARTIAL) exit 0 ;; *) exit 1 ;; esac
