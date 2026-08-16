#!/usr/bin/env bash
# U33 — Screen capture, so the agent can see the phone instead of being told about it
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U33" "Screencap" \
  "the agent can read the phone's UI state directly, without narration" \
  "capture DRM/secure surfaces, or anything while the screen is off"

require_phone
DIR="$(artifact_dir U33-screencap)"
fail=0

# Wake the screen first: screencap on a sleeping display returns a black or tiny image,
# which would look like a capture failure rather than a powered-down panel.
SCREEN="$(phone shell 'dumpsys power | grep -m1 "mWakefulness="' | tr -d '\r' | tr -d ' ')"
printf '  %s\n' "$SCREEN" >&2
case "$SCREEN" in
  *Asleep*|*Dozing*)
    info "screen asleep — waking it (KEYCODE_WAKEUP)"
    phone shell 'input keyevent 224' >/dev/null 2>&1 || true
    sleep 2
    ;;
esac

OUT="$DIR/screen.png"
if "$RIG_ROOT/adb/screencap.sh" "$OUT" >/dev/null 2>&1; then
  SIZE=$(wc -c < "$OUT")
  ok "captured $((SIZE/1024)) KB PNG"
else
  err "screencap failed"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  # Confirm it decodes and has sane dimensions — a valid PNG header is not enough,
  # a 1x1 image would pass that and be useless.
  PY="$(command -v py 2>/dev/null || command -v python3)"
  DIM="$("$PY" - "$OUT" <<'EOF'
import struct, sys
with open(sys.argv[1], "rb") as f:
    hdr = f.read(33)
w, h = struct.unpack(">II", hdr[16:24])
print(f"{w}x{h}")
EOF
)"
  printf '  dimensions: %s\n' "$DIM" >&2
  W="${DIM%x*}"
  if [ "${W:-0}" -lt 400 ]; then err "image only ${DIM} — not a real screen capture"; fail=1
  else ok "plausible screen dimensions"; fi
fi

if [ "$fail" -eq 0 ]; then ok "U33 PASS"; emit_result U33 PASS "$DIR" dimensions "${DIM:-?}" png "$OUT"
else err "U33 FAIL"; emit_result U33 FAIL "$DIR"; fi
exit "$fail"
