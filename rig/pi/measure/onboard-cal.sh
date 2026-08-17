#!/usr/bin/env bash
# Characterise the Pi's ONBOARD 3.5 mm output. Runs ON THE PI, emits JSON on stdout.
#
# Requires: Pi 3.5 mm jack cabled to dongle B pink (in). Nothing plugged into the Pi's jack
# except that cable.
#
# WHY THIS EXISTS
# ---------------
# E07 runs 11-12 moved the product's Mode 1W output from a USB dongle to the Pi's own jack,
# because every USB audio device shortens time-to-desync on the Bluetooth HCI UART. That
# swapped in an output path nobody had ever measured. The Pi 3B's analog out is PWM-based
# with a known-mediocre reputation (low level, noise that tracks CPU load, DC offset), and it
# now feeds a car aux input.
#
# This is a ONE-SHOT measurement: dongle B is borrowed and is the only capture device the rig
# has (dongle A is output-only per trap 6, the Lark is capture-only, the Pi has no analog in).
# Once it goes back, output quality is unmeasurable and becomes a matter of opinion.
#
# WHAT IT CANNOT TELL YOU
# -----------------------
# The measured noise and distortion are the SUM of the Pi's output and dongle B's mic input.
# The pink jack is a MIC input -- bias-fed, high-gain, and not designed for line level -- so
# expect it to be the dominant error source at high output levels. Read the sweep for the
# usable range and the shape of the curve, not as an absolute spec for the Pi's DAC.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=./devices.sh
. "$HERE/devices.sh"

rig_resolve
B="${DONGLE_B_CARD:-}"
[ -n "$B" ] || { echo "dongle B not present at $DONGLE_B_PORT" >&2; exit 78; }

# The onboard codec is matched by NAME, not by index: card numbering shifts as USB devices
# come and go, and this test exists precisely because USB devices are being added/removed.
ONBOARD="$(awk '/bcm2835_headpho|bcm2835 Headphones/ { print $1; exit }' /proc/asound/cards || true)"
[ -n "$ONBOARD" ] || { echo "onboard headphone codec not found in /proc/asound/cards" >&2; exit 78; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

TONE=1000
LEVELS="-40 -30 -20 -12 -6 -3 0"

lvl() { python3 "$REPO/rig/analysis/wav_level.py" "$1" --tone "$TONE" --json; }

# This dongle exposes only Speaker / Mic / Auto Gain Control -- there is no separate
# "Capture" control, so `Mic` IS the capture level. It must be enabled with `cap`; using
# `nocap` here mutes the input being measured and yields a silent recording.
#
# AGC off matters more than the level: with it on, the dongle chases the tone and flattens the
# sweep into a straight line, which reads as a beautifully linear output stage and is an
# artefact of the instrument.
amixer -c "$B" sset 'Auto Gain Control' off >/dev/null 2>&1 || true
amixer -c "$B" sset Mic 40% cap             >/dev/null 2>&1 || true
amixer -c "$ONBOARD" sset PCM 100%          >/dev/null 2>&1 || true

# --- noise floor: capture with the Pi's jack idle but ACTIVE -------------------------
# Measured with a stream PLAYING, not with the output closed. The bcm2835 driver powers the
# output stage down when idle, which would report a floor the product never sees.
#
# tone_gen has no silence mode (sine/chirp/pips only). -90 dBFS is below the LSB of a 16-bit
# file, so this is digitally silent while still holding the output stage open -- which is
# exactly the condition we want to measure.
python3 "$REPO/tools/audio/tone_gen.py" --mode sine --freq "$TONE" --seconds 4 \
  --rate 48000 --channels 2 --dbfs -90 --out sil.wav >/dev/null
aplay -D "plughw:$ONBOARD,0" sil.wav >/dev/null 2>&1 &
PLY=$!
sleep 0.5
arecord -D "plughw:$B,0" -f S16_LE -r 48000 -c 1 -d 3 silence.wav 2>/dev/null
wait "$PLY" 2>/dev/null || true
NOISE="$(lvl silence.wav)"

# --- level sweep ---------------------------------------------------------------------
SWEEP="["
first=1
for L in $LEVELS; do
  python3 "$REPO/tools/audio/tone_gen.py" --mode sine --freq "$TONE" \
    --seconds 3 --rate 48000 --channels 2 --dbfs "$L" --out t.wav >/dev/null
  # Playback first, then capture, so the whole capture window is steady tone.
  aplay -D "plughw:$ONBOARD,0" t.wav >/dev/null 2>&1 &
  PLY=$!
  sleep 0.5
  arecord -D "plughw:$B,0" -f S16_LE -r 48000 -c 1 -d 2 c.wav 2>/dev/null
  wait "$PLY" 2>/dev/null || true
  M="$(lvl c.wav)"
  [ "$first" -eq 1 ] || SWEEP="$SWEEP,"
  SWEEP="$SWEEP{\"out_dbfs\":$L,\"measured\":$M}"
  first=0
done
SWEEP="$SWEEP]"

printf '{"source":"onboard","onboard_card":"%s","noise_floor_raw":%s,"sweep":%s}\n' \
  "$ONBOARD" "$NOISE" "$SWEEP" \
  | python3 "$REPO/rig/analysis/calibration_reduce.py"
