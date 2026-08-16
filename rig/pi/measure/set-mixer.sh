#!/usr/bin/env bash
# Put dongle B (the instrument) into its standard MEASUREMENT state.
#
# Runs ON THE PI. Must be re-run after a reboot or a replug — ALSA mixer state does not
# reliably persist, and a silently-different mixer invalidates every level measurement
# taken afterwards.
#
# The critical one is AUTO GAIN CONTROL. C-Media capture paths ship with AGC ON, which
# continuously rescales the input. That would flatten exactly the level differences we
# are trying to measure and could hide dropouts by pumping the gain up during silence.
# An instrument with AGC is not an instrument.
#
# Controls are set by NUMID, not by name: `amixer sset` name-matching is ambiguous on
# this card ('Mic' carries both a playback and a capture volume) and fails silently.

set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/devices.sh"

rig_resolve
CARD="${DONGLE_B_CARD:-}"
[ -n "$CARD" ] || { echo "dongle B not present at port $DONGLE_B_PORT" >&2; exit 78; }

# numid map for C-Media 0d8c:0014. Verify with: amixer -c <n> contents
set_ctl() {
  local numid="$1" val="$2" label="$3"
  if amixer -c "$CARD" cset "numid=$numid" "$val" >/dev/null 2>&1; then
    printf '  numid=%-3s %-26s -> %s\n' "$numid" "$label" "$val"
  else
    printf '  numid=%-3s %-26s -> FAILED\n' "$numid" "$label" >&2
    return 1
  fi
}

echo "configuring card $CARD (port $DONGLE_B_PORT) for measurement:"
set_ctl 9 off    "Auto Gain Control"
set_ctl 8 0      "Mic Capture Volume"      # minimum, -12 dB
set_ctl 7 on     "Mic Capture Switch"
set_ctl 3 off    "Mic Playback (sidetone)" # must be off, or input leaks into output
set_ctl 6 37,37  "Speaker Playback Volume" # 0 dB reference

echo
echo "verification:"
for n in 3 6 7 8 9; do
  amixer -c "$CARD" cget "numid=$n" \
    | awk -v N="$n" '/name=/{gsub(/.*name=/,"");nm=$0} /: values=/{gsub(/  *: values=/,"");print "  numid=" N "  " nm " = " $0}'
done
