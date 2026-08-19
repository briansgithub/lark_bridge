#!/usr/bin/env bash
# U10 — Lark A1 receiver: enumeration, capabilities, noise floor, channel content
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U10" "Lark A1 capture device" \
  "the Lark enumerates and what formats/rates/channels it actually offers" \
  "prove audio QUALITY through the wireless link, or that the transmitter is paired"

require_pi
DIR="$(artifact_dir U10-lark)"
PY="$(command -v py 2>/dev/null || command -v python3)"
LARK_USB_ID="$(inv lark_usb_id 3547:0407)"
LARK_USB_SERIAL="$(inv lark_usb_serial '')"

CARD="$(pi "cd ~/rpi-lark-bridge && LARK_USB_ID='$LARK_USB_ID' LARK_USB_SERIAL='$LARK_USB_SERIAL' bash -c '. rig/pi/measure/devices.sh; rig_resolve; echo \$LARK_CARD'")"
[ -n "$CARD" ] || need_hardware "Lark A1 USB receiver ($LARK_USB_ID) in any Pi USB port" \
  "Plug the Hollyland receiver (3547:0407) into the Pi."
ok "Lark present by stable USB identity: $LARK_USB_ID -> card $CARD"

pi "arecord -D hw:$CARD,0 -f S16_LE -c 2 -r 48000 --dump-hw-params -d1 /dev/null 2>&1" \
  > "$DIR/hw-params.txt" 2>&1 || true

RATES="$(grep -m1 '^RATE:'     "$DIR/hw-params.txt" | sed 's/RATE: *//')"
CHANS="$(grep -m1 '^CHANNELS:' "$DIR/hw-params.txt" | sed 's/CHANNELS: *//')"
FMTS="$(grep -m1  '^FORMAT:'   "$DIR/hw-params.txt" | sed 's/FORMAT: *//')"

printf '  formats   : %s\n' "$FMTS"  >&2
printf '  rates     : %s\n' "$RATES" >&2
printf '  channels  : %s\n' "$CHANS" >&2

fail=0
case "$RATES" in
  *48000*) ok "48 kHz supported — matches the graph rate (ADR-0007), no resample at hop 1" ;;
  *) err "48 kHz NOT offered (rates: $RATES) — ADR-0007's graph rate must be revisited"; fail=1 ;;
esac

# Idle capture: room + preamp noise. This is the reference the acoustic SNR in U15 is
# measured against, so it must be recorded even though nothing is asserted about it.
info "recording 5 s idle (keep the room quiet)"
pi "cd ~/rpi-lark-bridge && arecord -D plughw:$CARD,0 -f S16_LE -r 48000 -c 2 -d 5 /tmp/rig/lark_idle.wav 2>/dev/null && \
    python3 rig/analysis/wav_level.py /tmp/rig/lark_idle.wav --json" > "$DIR/idle.json" 2>&1

NOISE="$("$PY" "$RIG_ROOT/analysis/jsonget.py" per_channel.0.rms_dbfs < "$DIR/idle.json")"
PEAK="$("$PY" "$RIG_ROOT/analysis/jsonget.py" per_channel.0.peak_dbfs < "$DIR/idle.json")"
IDENT="$("$PY" "$RIG_ROOT/analysis/jsonget.py" channels_identical < "$DIR/idle.json" || echo unknown)"
DIFF="$("$PY" "$RIG_ROOT/analysis/jsonget.py" channel_max_diff_lsb < "$DIR/idle.json" || echo '?')"

printf '  noise RMS : %s dBFS   peak %s dBFS\n' "$NOISE" "$PEAK" >&2
printf '  channels identical: %s (max diff %s LSB)\n' "$IDENT" "$DIFF" >&2

if [ "$IDENT" = "True" ]; then
  ok "both channels bit-identical -> MONO source duplicated into a stereo stream"
  info "the graph should take ONE channel; downmixing is harmless but pointless here"
else
  info "channels differ (max $DIFF LSB) — genuine stereo content or uncorrelated noise"
fi

if "$PY" -c "import sys;sys.exit(0 if float('$PEAK') < -3 else 1)"; then
  ok "idle peak has headroom"
else
  err "idle peak $PEAK dBFS — the Lark is near clipping with no signal; check its gain"
  fail=1
fi

if [ "$fail" -eq 0 ]; then ok "U10 PASS"
  emit_result U10 PASS "$DIR" card "$CARD" rates "$RATES" channels "$CHANS" \
    noise_rms_dbfs "$NOISE" channels_identical "$IDENT"
else err "U10 FAIL"; emit_result U10 FAIL "$DIR" card "$CARD" rates "$RATES"; fi
exit "$fail"
