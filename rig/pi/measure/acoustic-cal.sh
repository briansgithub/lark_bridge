#!/usr/bin/env bash
# Acoustic path calibration: dongle B -> speaker -> selected microphone.
# Runs ON THE PI, emits JSON on stdout.
#
# Finds the dongle B speaker-volume setting that lands the microphone at a target peak level,
# then measures SNR there.
#
# Why this needs calibrating rather than "turn it up until you hear it": with the speaker
# close-coupled to the mic capsule the path can have NET GAIN — a -6 dBFS tone was
# observed arriving at -1.7 dBFS peak, i.e. 1.7 dB from clipping. A clipped reference
# signal makes every downstream dropout and SNR measurement meaningless, and clipping is
# not obvious by ear on a sine.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=./devices.sh
. "$HERE/devices.sh"

rig_resolve
M="${MICROPHONE_CARD:-}"; B="${DONGLE_B_CARD:-}"
[ -n "$M" ] || { echo "no unambiguous configured microphone is present" >&2; exit 78; }
[ -n "$B" ] || { echo "dongle B not present at $DONGLE_B_PORT" >&2; exit 78; }

case "$MICROPHONE_ID" in
  lark-a1) MICROPHONE_CHANNELS=2 ;;
  fifine-k054) MICROPHONE_CHANNELS=1 ;;
  *) echo "unsupported microphone identity: ${MICROPHONE_ID:-none}" >&2; exit 78 ;;
esac

TARGET_PEAK="${TARGET_PEAK:--18}"   # healthy level with real headroom
TONE=1000

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT; cd "$WORK"

python3 "$REPO/tools/audio/tone_gen.py" --mode sine --freq "$TONE" \
  --seconds 4 --rate 48000 --channels 2 --dbfs -6 --out t.wav >/dev/null

# Microphone noise floor with nothing playing — the reference for acoustic SNR.
arecord -D "plughw:$M,0" -f S16_LE -r 48000 -c "$MICROPHONE_CHANNELS" -d 3 idle.wav 2>/dev/null
NOISE="$(python3 "$REPO/rig/analysis/wav_level.py" idle.wav --json)"

measure_at_volume() {
  local vol="$1"
  amixer -c "$B" cset numid=6 "$vol,$vol" >/dev/null 2>&1
  aplay -D "plughw:$B,0" t.wav >/dev/null 2>&1 &
  local P=$!
  sleep 0.5
  arecord -D "plughw:$M,0" -f S16_LE -r 48000 -c "$MICROPHONE_CHANNELS" -d 2 c.wav 2>/dev/null
  wait "$P" 2>/dev/null || true
  python3 "$REPO/rig/analysis/wav_level.py" c.wav --tone "$TONE" --json
}

# Speaker Playback Volume on this card is 0..37 in ~1 dB steps. Coarse scan then report;
# a full binary search is not worth the extra captures at ~2.5 s each.
SWEEP="["
first=1
for VOL in 37 31 25 19 13 7; do
  M="$(measure_at_volume "$VOL")"
  [ "$first" -eq 1 ] || SWEEP="$SWEEP,"
  SWEEP="$SWEEP{\"volume\":$VOL,\"measured\":$M}"
  first=0
done
SWEEP="$SWEEP]"

printf '{"microphone_id":"%s","microphone_card":"%s","format":{"rate":48000,"format":"S16LE","channels":%s},"target_peak_dbfs":%s,"noise_raw":%s,"sweep":%s}\n' \
  "$MICROPHONE_ID" "$M" "$MICROPHONE_CHANNELS" "$TARGET_PEAK" "$NOISE" "$SWEEP" \
  | python3 "$REPO/rig/analysis/acoustic_reduce.py"
