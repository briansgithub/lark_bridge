#!/usr/bin/env bash
# U15 — Acoustic injection path: dongle B -> speaker -> Lark transmitter mic
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U15" "Acoustic path calibration" \
  "a known stimulus reaches the Lark at a usable, non-clipping level" \
  "prove room noise is stable over hours — re-run if the bench is rearranged"

require_pi
DIR="$(artifact_dir U15-acoustic)"
PY="$(command -v py 2>/dev/null || command -v python3)"
J="$RIG_ROOT/analysis/jsonget.py"

info "sweeping speaker volume to find the setting that hits the target level"
pi "cd ~/rpi-lark-bridge && bash rig/pi/measure/acoustic-cal.sh" \
  > "$DIR/acoustic.json" 2>"$DIR/acoustic.err" \
  || { sed 's/^/  /' "$DIR/acoustic.err" >&2
       need_hardware "speaker on dongle B green, aimed at the Lark TRANSMITTER mic" \
         "Also confirm the Lark transmitter is powered and paired to its receiver."; }

VOL="$("$PY" "$J" chosen_volume        < "$DIR/acoustic.json")"
PEAK="$("$PY" "$J" chosen_peak_dbfs    < "$DIR/acoustic.json")"
SNR="$("$PY" "$J" acoustic_snr_db      < "$DIR/acoustic.json")"
NOISE="$("$PY" "$J" lark_noise_floor.rms_dbfs < "$DIR/acoustic.json")"
CLIP="$("$PY" "$J" any_clipping        < "$DIR/acoustic.json")"

printf '  Lark noise floor : %s dBFS\n' "$NOISE" >&2
printf '  chosen speaker vol: %s\n'     "$VOL"   >&2
printf '  Lark level at that: %s dBFS\n' "$PEAK" >&2
printf '  acoustic SNR      : %s dB\n'  "$SNR"   >&2
echo >&2

fail=0
[ -n "$VOL" ] && [ "$VOL" != "None" ] || { err "no non-clipping volume found"; fail=1; }

# 20 dB is the bar: below that, a dropout becomes hard to distinguish from room noise.
if "$PY" -c "import sys;sys.exit(0 if float('$SNR') >= 20 else 1)" 2>/dev/null; then
  ok "acoustic SNR ${SNR} dB (>= 20 dB required)"
else
  err "acoustic SNR ${SNR} dB is too low — move the speaker closer to the mic capsule"
  err "  proximity beats volume here: turning it up adds room reflections, not SNR"
  fail=1
fi

if [ "$CLIP" = "True" ]; then
  warn "some sweep points clipped — the chosen setting avoids it, but headroom is tight"
fi

# A clipped reference signal invalidates every downstream dropout measurement, and
# clipping is not obvious by ear on a sine. Insist on real headroom.
if "$PY" -c "import sys;sys.exit(0 if float('$PEAK') < -12 else 1)" 2>/dev/null; then
  ok "chosen level ${PEAK} dBFS leaves headroom"
else
  err "chosen level ${PEAK} dBFS is too close to full scale"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  sed -i -e "s|^cal_acoustic_snr_db .*|cal_acoustic_snr_db     = \"$SNR\"|" "$RIG_ROOT/inventory.toml"
  grep -q '^cal_speaker_volume' "$RIG_ROOT/inventory.toml" \
    || printf 'cal_speaker_volume      = "%s"   # dongle B numid=6, gives %s dBFS at the Lark\n' \
         "$VOL" "$PEAK" >> "$RIG_ROOT/inventory.toml"
  ok "recorded speaker volume $VOL and acoustic SNR $SNR dB in inventory"
  ok "U15 PASS"
  emit_result U15 PASS "$DIR" speaker_volume "$VOL" lark_peak_dbfs "$PEAK" \
    acoustic_snr_db "$SNR" lark_noise_dbfs "$NOISE"
else
  err "U15 FAIL"
  emit_result U15 FAIL "$DIR" speaker_volume "${VOL:-none}" acoustic_snr_db "${SNR:-none}"
fi
exit "$fail"
