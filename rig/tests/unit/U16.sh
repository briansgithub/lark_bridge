#!/usr/bin/env bash
# U16 — FIFINE K054: portable USB identity and native capture characterization
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U16" "FIFINE K054 capture device" \
  "the configured K054 identity is unambiguous and exposes native mono S16_LE/48 kHz capture only" \
  "prove physical mute/gain behavior, acoustic quality, replacement-unit identity, or AEC"

require_pi
DIR="$(artifact_dir U16-fifine-k054)"
PY="$(command -v py 2>/dev/null || command -v python3)"
J="$RIG_ROOT/analysis/jsonget.py"
FIFINE_USB_ID="$(inv fifine_usb_id 0c76:161e)"
FIFINE_USB_SERIAL="$(inv fifine_usb_serial '')"
FIFINE_PORT="$(inv fifine_port_path '')"

CARD="$(pi "cd ~/rpi-lark-bridge && FIFINE_USB_ID='$FIFINE_USB_ID' FIFINE_USB_SERIAL='$FIFINE_USB_SERIAL' FIFINE_PORT='$FIFINE_PORT' bash -c '. rig/pi/measure/devices.sh; rig_resolve; echo \$FIFINE_CARD'")"
case "$CARD" in
  [0-9]*) ;;
  AMBIGUOUS*)
    err "K054 identity is ambiguous: $CARD"
    err "set fifine_usb_serial or fifine_port_path in rig/inventory.toml"
    emit_result U16 FAIL "$DIR" candidate_id fifine-k054 usb_id "$FIFINE_USB_ID" reason ambiguous
    exit 1
    ;;
  *)
    need_hardware "FIFINE K054 ($FIFINE_USB_ID) in a Pi USB port" \
      "Plug in the K054; portable matching intentionally does not require a fixed port."
    ;;
esac
ok "FIFINE present by configured USB identity: $FIFINE_USB_ID -> observed card $CARD"

pi "udevadm info -q property -p \$(readlink -f /sys/class/sound/card$CARD/device)" \
  > "$DIR/udev-properties.txt" 2>&1 || true
pi "arecord -D hw:$CARD,0 -f S16_LE -c 1 -r 48000 --dump-hw-params -d 1 /dev/null 2>&1" \
  > "$DIR/hw-params.txt" 2>&1 || true
pi "amixer -c $CARD contents" > "$DIR/amixer.txt" 2>&1 || true
pi "python3 ~/rpi-lark-bridge/pi/bridged/bridgectl.py microphone list --json" \
  > "$DIR/microphones.json" 2> "$DIR/microphones.err" || true

RATES="$(grep -m1 '^RATE:' "$DIR/hw-params.txt" | sed 's/RATE: *//')"
CHANS="$(grep -m1 '^CHANNELS:' "$DIR/hw-params.txt" | sed 's/CHANNELS: *//')"
FMTS="$(grep -m1 '^FORMAT:' "$DIR/hw-params.txt" | sed 's/FORMAT: *//')"
printf '  formats  : %s\n' "$FMTS" >&2
printf '  rates    : %s\n' "$RATES" >&2
printf '  channels : %s\n' "$CHANS" >&2

fail=0
case "$FMTS" in *S16_LE*) ok "native S16_LE capture is available" ;; *) err "S16_LE absent: $FMTS"; fail=1 ;; esac
case "$RATES" in *48000*) ok "native 48 kHz capture is available" ;; *) err "48 kHz absent: $RATES"; fail=1 ;; esac
case "$CHANS" in *1*) ok "mono capture is available" ;; *) err "mono capture absent: $CHANS"; fail=1 ;; esac

if pi "test -e /proc/asound/card$CARD/pcm0c && test ! -e /proc/asound/card$CARD/pcm0p"; then
  ok "device is capture-only"
else
  err "expected capture PCM and no playback PCM"
  fail=1
fi

info "recording 5 s idle; this records an absolute floor but imposes no generic -60 dBFS gate"
pi "cd ~/rpi-lark-bridge && arecord -D hw:$CARD,0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/rig/fifine_idle.wav 2>/dev/null && python3 rig/analysis/wav_level.py /tmp/rig/fifine_idle.wav --json" \
  > "$DIR/idle.json" 2> "$DIR/idle.err" || { sed 's/^/  /' "$DIR/idle.err" >&2; fail=1; }

NOISE="unknown"; PEAK="unknown"
if [ -s "$DIR/idle.json" ]; then
  NOISE="$("$PY" "$J" per_channel.0.rms_dbfs < "$DIR/idle.json")"
  PEAK="$("$PY" "$J" per_channel.0.peak_dbfs < "$DIR/idle.json")"
  printf '  idle RMS : %s dBFS   peak %s dBFS\n' "$NOISE" "$PEAK" >&2
fi

PORT_PATH="$(grep -m1 '^ID_PATH=' "$DIR/udev-properties.txt" | cut -d= -f2- || true)"
if [ "$fail" -eq 0 ]; then
  ok "U16 PASS — electronic characterization only; field-QA gates remain open"
  emit_result U16 PASS "$DIR" candidate_id fifine-k054 usb_id "$FIFINE_USB_ID" \
    configured_port "${FIFINE_PORT:-portable}" observed_path "${PORT_PATH:-unknown}" \
    format S16_LE rate 48000 channels 1 noise_rms_dbfs "$NOISE" idle_peak_dbfs "$PEAK"
else
  err "U16 FAIL"
  emit_result U16 FAIL "$DIR" candidate_id fifine-k054 usb_id "$FIFINE_USB_ID"
fi
exit "$fail"
