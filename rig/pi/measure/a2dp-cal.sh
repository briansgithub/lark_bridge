#!/usr/bin/env bash
# A2DP capture-loop calibration. Runs ON THE PI, emits JSON on stdout.
#
# Path under test:
#   PipeWire -> SBC -> air -> iWorld A2DP receiver -> 3.5 mm line-out -> dongle B pink
#
# This is the loop that makes spike S3 automatic: it is how the Pi hears what a Bluetooth
# headphone would have heard, so dropouts get counted by a script instead of by a human
# listening for an hour.
#
# Sweeps dongle B's capture gain to find the setting that lands the captured tone at a
# usable level. Written as a file rather than an ssh one-liner because the nested quoting
# of python-inside-bash-inside-ssh is unmaintainable.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
# shellcheck source=./devices.sh
. "$HERE/devices.sh"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

rig_resolve
B="${DONGLE_B_CARD:-}"
[ -n "$B" ] || { echo "dongle B not present at $DONGLE_B_PORT" >&2; exit 78; }

SINK="$(pw-dump | python3 "$REPO/rig/analysis/find_node.py" --prefix bluez_output)"
[ -n "$SINK" ] || { echo "no bluez_output sink — is the A2DP receiver connected?" >&2; exit 78; }

TARGET_PEAK="${TARGET_PEAK:--20}"
TONE=1000

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT; cd "$WORK"

# Unity gain on the sink: a PipeWire sink volume below 1.0 silently costs headroom and
# would be mistaken for a weak line-out.
wpctl set-volume "$SINK" 1.0 >/dev/null 2>&1 || true

python3 "$REPO/tools/audio/tone_gen.py" --mode sine --freq "$TONE" \
  --seconds 12 --rate 48000 --channels 2 --dbfs -6 --out t.wav >/dev/null

# Idle noise on the capture side with A2DP connected but silent — the reference for SNR.
arecord -D "plughw:$B,0" -f S16_LE -r 48000 -c 1 -d 2 idle.wav 2>/dev/null
NOISE="$(python3 "$REPO/rig/analysis/wav_level.py" idle.wav --json)"

SWEEP="["
first=1
for G in 0 8 16 24 30 35; do
  amixer -c "$B" cset numid=8 "$G" >/dev/null 2>&1
  pw-play --target "$SINK" t.wav >/dev/null 2>&1 &
  P=$!
  # A2DP buffers 150-250 ms and the sink ramps on stream start; 2 s of settle
  # was not enough and produced tone readings 15-20 dB below the signal peak.
  sleep 4
  arecord -D "plughw:$B,0" -f S16_LE -r 48000 -c 1 -d 3 c.wav 2>/dev/null
  kill "$P" 2>/dev/null || true
  wait "$P" 2>/dev/null || true
  M="$(python3 "$REPO/rig/analysis/wav_level.py" c.wav --tone "$TONE" --search-hz 5 --json)"
  [ "$first" -eq 1 ] || SWEEP="$SWEEP,"
  SWEEP="$SWEEP{\"gain\":$G,\"measured\":$M}"
  first=0
done
SWEEP="$SWEEP]"

printf '{"target_peak_dbfs":%s,"sink":"%s","noise_raw":%s,"sweep":%s}\n' \
  "$TARGET_PEAK" "$SINK" "$NOISE" "$SWEEP" \
  | python3 "$REPO/rig/analysis/a2dp_reduce.py"
