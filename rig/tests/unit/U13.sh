#!/usr/bin/env bash
# U13 — Rig calibration: measure the INSTRUMENT's own error floor
#
# The most important test in the rig. Until we know the measurement chain's own noise,
# linearity and dropout behaviour, no result taken with it can be trusted -- we could not
# tell a rig fault from a product fault. Everything measured later is reported relative to
# the numbers this test produces, and anything at or below them is "below rig resolution",
# never a measurement.
#
# Requires: 3.5mm male-male cable looping dongle B green (out) -> pink (in). Nothing else.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U13" "Rig calibration (dongle B loopback)" \
  "the instrument's noise floor, linearity, headroom and usable dynamic range" \
  "prove long-term dropout freedom -- that needs the 60-min soak (rig soak start U13)"

require_pi
DIR="$(artifact_dir U13-calibration)"
PY="$(command -v py 2>/dev/null || command -v python3)"
R="$RIG_ROOT"
REMOTE='cd ~/rpi-lark-bridge'

info "applying measurement mixer state"
pi "$REMOTE && bash rig/pi/measure/set-mixer.sh" > "$DIR/mixer.txt" 2>&1 || die "mixer setup failed"

# Run the whole sweep on the Pi in one shot: each ssh round-trip is ~300 ms and this
# would otherwise be dominated by connection overhead.
info "running loopback sweep on the Pi"
pi "$REMOTE && bash rig/pi/measure/calibrate.sh" > "$DIR/calibration.json" 2>"$DIR/calibration.err" \
  || { cat "$DIR/calibration.err" >&2; die "calibration run failed"; }

j() { "$PY" "$R/analysis/jsonget.py" "$1" < "$DIR/calibration.json"; }

NOISE_RMS="$(j noise_floor.rms_dbfs)"
NOISE_PEAK="$(j noise_floor.peak_dbfs)"
DC="$(j noise_floor.dc_offset)"
GAIN="$(j loopback_gain_db)"
LINERR="$(j linearity_max_error_db)"
CLIPAT="$(j clipping_onset_dbfs)"
SNR="$(j snr_db)"
DR="$(j dynamic_range_db)"

printf '  noise floor (RMS)     : %s dBFS\n' "$NOISE_RMS" >&2
printf '  noise floor (peak)    : %s dBFS\n' "$NOISE_PEAK" >&2
printf '  DC offset             : %s\n'      "$DC" >&2
printf '  loopback gain         : %s dB\n'   "$GAIN" >&2
printf '  linearity max error   : %s dB\n'   "$LINERR" >&2
printf '  clipping onset        : %s\n'      "$CLIPAT" >&2
printf '  SNR at nominal        : %s dB\n'   "$SNR" >&2
printf '  usable dynamic range  : %s dB\n'   "$DR" >&2
echo >&2

fail=0

# A cheap USB card should manage better than -60 dBFS RMS with AGC off. Much worse than
# that usually means AGC crept back on, or a ground loop through the USB bus.
if "$PY" -c "import sys;sys.exit(0 if float('$NOISE_RMS') < -60 else 1)"; then
  ok "noise floor below -60 dBFS"
else
  err "noise floor $NOISE_RMS dBFS is too high — check AGC is off and the cable is seated"
  fail=1
fi

# Non-linearity is the signature of AGC, a compressor, or clipping. Any of them would
# corrupt every level measurement made afterwards, silently.
if "$PY" -c "import sys;sys.exit(0 if abs(float('$LINERR')) < 1.5 else 1)"; then
  ok "linear within ${LINERR} dB across the sweep — no AGC or compression"
else
  err "non-linear (${LINERR} dB error) — something is applying dynamic gain"
  fail=1
fi

if "$PY" -c "import sys;sys.exit(0 if float('$DR') > 50 else 1)"; then
  ok "dynamic range ${DR} dB — sufficient to detect dropouts against the noise floor"
else
  err "dynamic range ${DR} dB is too small for reliable dropout detection"
  fail=1
fi

if [ "$CLIPAT" = "none" ]; then
  ok "no clipping even at 0 dBFS output — the input has headroom for a line-level source"
  info "an inline attenuator is likely NOT required for the Bluetooth receiver"
else
  warn "clipping begins at $CLIPAT dBFS output — a hotter source will need attenuation"
fi

# Persist the numbers so later tests can cite them instead of re-deriving them.
if [ "$fail" -eq 0 ]; then
  sed -i \
    -e "s|^cal_noise_floor_dbfs .*|cal_noise_floor_dbfs   = \"$NOISE_RMS\"|" \
    -e "s|^cal_acoustic_snr_db .*|cal_acoustic_snr_db     = \"\"|" \
    "$RIG_ROOT/inventory.toml"
  ok "recorded calibration constants in rig/inventory.toml"
  ok "U13 PASS"
  emit_result U13 PASS "$DIR" noise_floor_dbfs "$NOISE_RMS" loopback_gain_db "$GAIN" \
    linearity_err_db "$LINERR" dynamic_range_db "$DR" clipping_onset "$CLIPAT"
else
  err "U13 FAIL — do not trust any measurement taken with this rig until it passes"
  emit_result U13 FAIL "$DIR" noise_floor_dbfs "$NOISE_RMS" linearity_err_db "$LINERR"
fi
exit "$fail"
