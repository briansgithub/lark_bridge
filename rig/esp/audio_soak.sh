#!/usr/bin/env bash
# Audio soak: the Pixel harness plays its stimulus as A2DP media to the S31 while bt_soak.py
# logs the Pixel ACL and the S31 console (test-image stats JSON at 1 Hz). Summarize afterwards:
#   python3 -B rig/esp/summarize_soak.py --pixel-timezone America/New_York <out>
#
# usage: rig/esp/audio_soak.sh <out-dir (WSL path)> <seconds> <s31-port> [bt_soak args...]
#   e.g. ... COM3 --console "10:tap lark_in"   (H6 tap records during the soak)
# Needs the harness installed and started (rig/esp/pixel-harness/README.md), the S31 bonded and
# connected, and the Pixel awake (stay_on_while_plugged_in). Read-only on both boards: the S31
# port is opened with DTR/RTS deasserted, and nothing is flashed or reset.
set -euo pipefail

OUT=${1:?out dir}
SECS=${2:?seconds}
PORT=${3:?s31 port}
shift 3
ADB=${ADB:-/mnt/c/Users/user1/AppData/Local/Android/Sdk/platform-tools/adb.exe}
PY=${PY:-/mnt/c/Windows/py.exe}
HERE=$(cd "$(dirname "$0")" && pwd)
R="-f 0x20 -n dev.larkbridge.harness/.ControlReceiver"
A=dev.larkbridge.harness
TAG=soak$(date -u +%H%M%S)

[ -e "$OUT" ] && { echo "refusing to reuse $OUT" >&2; exit 2; }
mkdir -p "$OUT"
LOOPS=$(( SECS / 3 + 20 ))   # the stimulus is 3.0 s; run a little past the logger's end

"$ADB" logcat -v raw -s LarkHarness:I > "$OUT/harness.log" 2>/dev/null &
LOGCAT=$!
cleanup() {
    "$ADB" shell am broadcast $R -a $A.STOP_STIMULUS --es tag "$TAG-stop" >/dev/null 2>&1 || true
    sleep 2
    kill "$LOGCAT" 2>/dev/null || true
}
trap cleanup EXIT

"$ADB" shell am broadcast $R -a $A.STATUS --es tag "$TAG-s0" >/dev/null
"$ADB" shell am broadcast $R -a $A.PLAY_STIMULUS --es usage media --ei loops "$LOOPS" \
    --ei progress_s 30 --es tag "$TAG" >/dev/null
echo "stimulus $TAG: $LOOPS loops, logger ${SECS}s -> $OUT"

cd "$HERE/../.."
"$PY" -3 rig/esp/bt_soak.py --port "$PORT" --adb "$(wslpath -w "$ADB")" --interval 180 \
    --seconds "$SECS" --out "$(wslpath -w "$OUT")" "$@"
"$ADB" shell am broadcast $R -a $A.STATUS --es tag "$TAG-s1" >/dev/null
