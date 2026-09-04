#!/usr/bin/env bash
# U17 — FIFINE K053: portable USB identity and native capture/playback characterization
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U17" "FIFINE K053 lavalier device" \
  "the configured K053 identity is unambiguous and exposes mono S16_LE/48 kHz capture plus stereo playback" \
  "prove physical controls, acoustic quality, replacement-unit identity, AEC, or bridge output policy"

require_pi
DIR="$(artifact_dir U17-fifine-k053)"
PY="$(command -v py 2>/dev/null || command -v python3)"
J="$RIG_ROOT/analysis/jsonget.py"
FIFINE_K053_USB_ID="$(inv fifine_k053_usb_id 0c76:161f)"
FIFINE_K053_USB_SERIAL="$(inv fifine_k053_usb_serial '')"
FIFINE_K053_PORT="$(inv fifine_k053_port_path '')"

CARD="$(pi "cd ~/rpi-lark-bridge && FIFINE_K053_USB_ID='$FIFINE_K053_USB_ID' FIFINE_K053_USB_SERIAL='$FIFINE_K053_USB_SERIAL' FIFINE_K053_PORT='$FIFINE_K053_PORT' bash -c '. rig/pi/measure/devices.sh; rig_resolve; echo \$FIFINE_K053_CARD'")"
case "$CARD" in
  [0-9]*) ;;
  AMBIGUOUS*)
    err "K053 identity is ambiguous: $CARD"
    err "set fifine_k053_usb_serial or fifine_k053_port_path in rig/inventory.toml"
    emit_result U17 FAIL "$DIR" candidate_id fifine-k053 usb_id "$FIFINE_K053_USB_ID" reason ambiguous
    exit 1
    ;;
  *)
    need_hardware "FIFINE K053 ($FIFINE_K053_USB_ID) in a Pi USB port" \
      "Plug in the K053; portable matching intentionally does not require a fixed port."
    ;;
esac
ok "K053 present by configured USB identity: $FIFINE_K053_USB_ID -> observed card $CARD"

pi "udevadm info -q property -p \$(readlink -f /sys/class/sound/card$CARD/device)" \
  > "$DIR/udev-properties.txt" 2>&1 || true
pi "arecord -D hw:$CARD,0 -f S16_LE -c 1 -r 48000 --dump-hw-params -d 1 /dev/null 2>&1" \
  > "$DIR/capture-hw-params.txt" 2>&1 || true
pi "aplay -D hw:$CARD,0 -t raw -f S16_LE -c 2 -r 48000 --dump-hw-params -d 1 /dev/zero 2>&1" \
  > "$DIR/playback-hw-params.txt" 2>&1 || true
pi "amixer -c $CARD contents" > "$DIR/amixer.txt" 2>&1 || true
pi "python3 ~/rpi-lark-bridge/pi/bridged/bridgectl.py microphone list --json" \
  > "$DIR/microphones.json" 2> "$DIR/microphones.err" || true

CAPTURE_RATES="$(grep -m1 '^RATE:' "$DIR/capture-hw-params.txt" | sed 's/RATE: *//')"
CAPTURE_CHANS="$(grep -m1 '^CHANNELS:' "$DIR/capture-hw-params.txt" | sed 's/CHANNELS: *//')"
CAPTURE_FMTS="$(grep -m1 '^FORMAT:' "$DIR/capture-hw-params.txt" | sed 's/FORMAT: *//')"
PLAYBACK_RATES="$(grep -m1 '^RATE:' "$DIR/playback-hw-params.txt" | sed 's/RATE: *//')"
PLAYBACK_CHANS="$(grep -m1 '^CHANNELS:' "$DIR/playback-hw-params.txt" | sed 's/CHANNELS: *//')"
PLAYBACK_FMTS="$(grep -m1 '^FORMAT:' "$DIR/playback-hw-params.txt" | sed 's/FORMAT: *//')"
printf '  capture formats  : %s\n' "$CAPTURE_FMTS" >&2
printf '  capture rates    : %s\n' "$CAPTURE_RATES" >&2
printf '  capture channels : %s\n' "$CAPTURE_CHANS" >&2
printf '  playback formats : %s\n' "$PLAYBACK_FMTS" >&2
printf '  playback rates   : %s\n' "$PLAYBACK_RATES" >&2
printf '  playback channels: %s\n' "$PLAYBACK_CHANS" >&2

fail=0
case "$CAPTURE_FMTS" in *S16_LE*) ok "native S16_LE capture is available" ;; *) err "S16_LE capture absent: $CAPTURE_FMTS"; fail=1 ;; esac
case "$CAPTURE_RATES" in *48000*) ok "native 48 kHz capture is available" ;; *) err "48 kHz capture absent: $CAPTURE_RATES"; fail=1 ;; esac
case "$CAPTURE_CHANS" in *1*) ok "mono capture is available" ;; *) err "mono capture absent: $CAPTURE_CHANS"; fail=1 ;; esac
case "$PLAYBACK_FMTS" in *S16_LE*) ok "native S16_LE playback is available" ;; *) err "S16_LE playback absent: $PLAYBACK_FMTS"; fail=1 ;; esac
case "$PLAYBACK_RATES" in *48000*) ok "native 48 kHz playback is available" ;; *) err "48 kHz playback absent: $PLAYBACK_RATES"; fail=1 ;; esac
case "$PLAYBACK_CHANS" in *2*) ok "stereo playback is available" ;; *) err "stereo playback absent: $PLAYBACK_CHANS"; fail=1 ;; esac

if pi "test -e /proc/asound/card$CARD/pcm0c && test -e /proc/asound/card$CARD/pcm0p"; then
  ok "device exposes both capture and playback PCMs"
else
  err "expected both capture and playback PCMs"
  fail=1
fi

info "recording 5 s idle; this records an absolute floor but imposes no generic -60 dBFS gate"
pi "cd ~/rpi-lark-bridge && arecord -D hw:$CARD,0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/rig/fifine_k053_idle.wav 2>/dev/null && python3 rig/analysis/wav_level.py /tmp/rig/fifine_k053_idle.wav --json" \
  > "$DIR/idle.json" 2> "$DIR/idle.err" || { sed 's/^/  /' "$DIR/idle.err" >&2; fail=1; }

NOISE="unknown"; PEAK="unknown"
if [ -s "$DIR/idle.json" ]; then
  NOISE="$("$PY" "$J" per_channel.0.rms_dbfs < "$DIR/idle.json")"
  PEAK="$("$PY" "$J" per_channel.0.peak_dbfs < "$DIR/idle.json")"
  printf '  idle RMS : %s dBFS   peak %s dBFS\n' "$NOISE" "$PEAK" >&2
fi

PORT_PATH="$(grep -m1 '^ID_PATH=' "$DIR/udev-properties.txt" | cut -d= -f2- || true)"
if [ "$fail" -eq 0 ]; then
  ok "U17 PASS — electronic characterization only; bridge policy and field-QA gates remain separate"
  emit_result U17 PASS "$DIR" candidate_id fifine-k053 usb_id "$FIFINE_K053_USB_ID" \
    configured_port "${FIFINE_K053_PORT:-portable}" observed_path "${PORT_PATH:-unknown}" \
    capture_format S16_LE capture_rate 48000 capture_channels 1 \
    playback_format S16_LE playback_rate 48000 playback_channels 2 \
    noise_rms_dbfs "$NOISE" idle_peak_dbfs "$PEAK"
else
  err "U17 FAIL"
  emit_result U17 FAIL "$DIR" candidate_id fifine-k053 usb_id "$FIFINE_K053_USB_ID"
fi
exit "$fail"
