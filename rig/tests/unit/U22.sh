#!/usr/bin/env bash
# U22 — A2DP capture loop: the rig can HEAR what a Bluetooth headphone hears
#
# This is the test that makes spike S3 automatic. Without it, Stage E means a human
# listening to a tone for sixty minutes and counting glitches by ear. With it, the Pi
# captures the receiver's line-out and a script counts them.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U22" "A2DP capture loop" \
  "audio sent over A2DP is recoverable at dongle B at a usable level" \
  "prove dropout-free operation — that is the Stage E soak, not this test"

require_pi
DIR="$(artifact_dir U22-a2dp-loop)"
PY="$(command -v py 2>/dev/null || command -v python3)"
J="$RIG_ROOT/analysis/jsonget.py"

info "sweeping capture gain over the A2DP path (~1 min)"
pi 'cd ~/rpi-lark-bridge && bash rig/pi/measure/a2dp-cal.sh' \
  > "$DIR/a2dp.json" 2>"$DIR/a2dp.err" \
  || { sed 's/^/  /' "$DIR/a2dp.err" >&2
       need_hardware "A2DP receiver connected, line-out -> dongle B pink jack" \
         "Run rig unit U21 first to confirm the receiver is connected."; }

GAIN="$("$PY" "$J" chosen_gain            < "$DIR/a2dp.json")"
PEAK="$("$PY" "$J" chosen_peak_dbfs       < "$DIR/a2dp.json")"
SNR="$("$PY" "$J" snr_vs_noise_floor_db   < "$DIR/a2dp.json")"
NOISE="$("$PY" "$J" capture_noise_floor_dbfs < "$DIR/a2dp.json")"
ATTEN="$("$PY" "$J" attenuator_needed     < "$DIR/a2dp.json")"

printf '  capture noise floor : %s dBFS\n' "$NOISE" >&2
printf '  chosen capture gain : %s\n'      "$GAIN"  >&2
printf '  captured level      : %s dBFS\n' "$PEAK"  >&2
printf '  level above floor   : %s dB\n'   "$SNR"   >&2
echo >&2

fail=0
[ -n "$GAIN" ] && [ "$GAIN" != "None" ] || { err "no usable capture gain found"; fail=1; }

# 40 dB is the bar: a dropout must be unambiguously distinguishable from silence.
if "$PY" -c "import sys;sys.exit(0 if float('$SNR') >= 40 else 1)" 2>/dev/null; then
  ok "captured signal ${SNR} dB above the noise floor (>= 40 dB required)"
else
  err "only ${SNR} dB above the noise floor — dropouts would be hard to distinguish"
  fail=1
fi

if [ "$ATTEN" = "False" ]; then
  ok "no clipping at ANY capture gain — the inline attenuator is NOT required"
  info "the receiver's line-out is weak, not hot: the opposite of the original concern"
else
  warn "clipping observed — an inline attenuator IS required for this source"
fi

# Verify the tone survived the codec: tone level should track the signal peak. A large
# gap means the analysis window caught stream ramp-up rather than steady state.
BAD="$("$PY" - "$DIR/a2dp.json" <<'EOF'
import json, sys
sweep = json.load(open(sys.argv[1]))["sweep"]
print(sum(1 for p in sweep if p["tone_dbfs"] is None or abs(p["tone_dbfs"] - p["peak_dbfs"]) > 2.0))
EOF
)"
if [ "${BAD:-9}" -eq 0 ]; then
  ok "tone tracks signal peak at every gain — steady-state capture confirmed"
else
  warn "$BAD sweep points where tone and peak disagree by >2 dB"
  warn "  usually insufficient settle time after starting the A2DP stream"
fi

if [ "$fail" -eq 0 ]; then
  grep -q '^cal_a2dp_capture_gain' "$RIG_ROOT/inventory.toml" \
    || printf 'cal_a2dp_capture_gain   = "%s"   # dongle B numid=8, gives %s dBFS from the A2DP receiver\n' \
         "$GAIN" "$PEAK" >> "$RIG_ROOT/inventory.toml"
  ok "recorded A2DP capture gain $GAIN in inventory"
  ok "U22 PASS — the Stage E measurement loop is closed"
  emit_result U22 PASS "$DIR" capture_gain "$GAIN" level_dbfs "$PEAK" snr_db "$SNR" attenuator_needed "$ATTEN"
else
  err "U22 FAIL — Stage E would have to stay manual"
  emit_result U22 FAIL "$DIR" capture_gain "${GAIN:-none}" snr_db "${SNR:-none}"
fi
exit "$fail"
