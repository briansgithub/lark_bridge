#!/usr/bin/env bash
# Loopback calibration sweep. Runs ON THE PI, emits JSON on stdout.
#
# Requires dongle B green (out) cabled to dongle B pink (in), nothing else attached.
#
# The whole sweep runs in one Pi-side invocation rather than being driven step by step
# over ssh: each round trip costs ~300 ms and would otherwise dominate the measurement.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=./devices.sh
. "$HERE/devices.sh"

rig_resolve
B="${DONGLE_B_CARD:-}"
[ -n "$B" ] || { echo "dongle B not present at $DONGLE_B_PORT" >&2; exit 78; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

TONE=1000
LEVELS="-40 -30 -20 -12 -6 -3 0"

lvl() { python3 "$REPO/rig/analysis/wav_level.py" "$1" --tone "$TONE" --json; }

# --- noise floor: capture with nothing playing ---------------------------------------
arecord -D "plughw:$B,0" -f S16_LE -r 48000 -c 1 -d 3 silence.wav 2>/dev/null
NOISE="$(lvl silence.wav)"

# --- level sweep ---------------------------------------------------------------------
SWEEP="["
first=1
for L in $LEVELS; do
  python3 "$REPO/tools/audio/tone_gen.py" --mode sine --freq "$TONE" \
    --seconds 3 --rate 48000 --channels 2 --dbfs "$L" --out t.wav >/dev/null
  # Start PLAYBACK first, then capture, so the whole capture window is steady tone.
  aplay -D "plughw:$B,0" t.wav >/dev/null 2>&1 &
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

# --- reduce to the constants the rig cites --------------------------------------------
# calibration_reduce.py is a real file, not an inline heredoc: `python3 - <<'PY'` in a
# pipeline makes Python read its PROGRAM from stdin, so the piped data never reaches
# json.load(). That bug has been hit twice here already.
printf '{"noise_floor_raw":%s,"sweep":%s}\n' "$NOISE" "$SWEEP" \
  | python3 "$REPO/rig/analysis/calibration_reduce.py"
