#!/usr/bin/env bash
# Capture the Pixel's screen as a PNG the agent can read directly.
#
# This is what collapses "tell me what you see on your phone" into something observable
# without a human in the loop. Used on every state change during Android routing tests,
# so a claim about the phone's UI is always backed by an image.
#
#   rig phone-shot                 # -> artifacts/screencaps/<timestamp>.png
#   rig phone-shot out.png
#
# Note: exec-out (not shell) keeps the byte stream binary-clean. Using `adb shell` here
# corrupts PNGs on Windows because of CRLF translation.

set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

require_phone

OUT="${1:-}"
if [ -z "$OUT" ]; then
  mkdir -p "$RIG_ARTIFACTS/screencaps"
  OUT="$RIG_ARTIFACTS/screencaps/$(timestamp).png"
fi
mkdir -p "$(dirname "$OUT")"

phone exec-out screencap -p > "$OUT"

SIZE=$(wc -c < "$OUT")
if [ "$SIZE" -lt 1000 ]; then
  rm -f "$OUT"
  die "screencap produced only ${SIZE} bytes — the screen may be off, or the surface is DRM-protected"
fi

# Verify it really is a PNG rather than an error message written to the file.
if ! head -c 8 "$OUT" | od -An -tx1 | grep -q "89 50 4e 47"; then
  die "output is not a PNG — first bytes: $(head -c 16 "$OUT" | od -An -c | tr -s ' ')"
fi

ok "screencap: $OUT ($((SIZE/1024)) KB)"
printf '%s\n' "$OUT"
