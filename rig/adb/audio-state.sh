#!/usr/bin/env bash
# Snapshot Android's audio routing state as normalised JSON.
#
# This is the rig's primary window into "what is Android ACTUALLY doing", as opposed to
# what we hope it is doing. The load-bearing field is active_communication_device.
#
#   rig phone-state              # JSON to stdout
#   rig phone-state --summary    # one human line
#   rig phone-state --save DIR   # JSON + raw dumpsys into DIR

set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

MODE=json
SAVE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --summary) MODE=summary; shift ;;
    --save)    SAVE="${2:?--save needs a directory}"; shift 2 ;;
    *)         die "unknown argument: $1" ;;
  esac
done

require_phone
PY="$(command -v py 2>/dev/null || command -v python3)"
PARSER="$RIG_ROOT/analysis/audio_state.py"

RAW="$(phone shell dumpsys audio 2>/dev/null)"
[ -n "$RAW" ] || die "dumpsys audio returned nothing — is the device still authorised?"

JSON="$(printf '%s\n' "$RAW" | "$PY" "$PARSER")"

if [ -n "$SAVE" ]; then
  mkdir -p "$SAVE"
  printf '%s\n' "$RAW"  > "$SAVE/dumpsys-audio.txt"
  printf '%s\n' "$JSON" > "$SAVE/audio-state.json"
fi

if [ "$MODE" = "summary" ]; then
  printf '%s\n' "$JSON" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
acd=d.get("active_communication_device")
dev = "none" if not acd else f'"'"'{acd.get("type")}({acd.get("name") or acd.get("addr") or "?"})'"'"'
print(f"mode={d.get(\"audio_mode_actual\")} sco={d.get(\"sco_audio_state\")} comm_device={dev}")
'
else
  printf '%s\n' "$JSON"
fi
