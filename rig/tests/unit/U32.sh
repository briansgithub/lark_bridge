#!/usr/bin/env bash
# U32 — Android audio routing is observable and parseable
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U32" "Routing observability (dumpsys audio)" \
  "the rig can assert what Android is ACTUALLY routing communication audio to" \
  "prove the bridge is selected — only that we can see which device is, when it is"

require_phone
DIR="$(artifact_dir U32-routing)"
PY="$(command -v py 2>/dev/null || command -v python3)"

bash "$RIG_ROOT/adb/audio-state.sh" --save "$DIR" >/dev/null

# Read JSON via STDIN, never by embedding the path in a -c string.
# Git Bash (MSYS2) rewrites POSIX paths to Windows form only when they are separate
# ARGUMENTS to a native binary. A path pasted inside a code string stays as "/b/..."
# which Windows Python cannot open. Redirection is performed by bash itself, so it
# always works. Same rule applies anywhere else in the rig that calls py.exe.
jq_field() { "$PY" "$RIG_ROOT/analysis/jsonget.py" "$1" < "$DIR/audio-state.json"; }

MATCHED="$(jq_field raw_lines_matched)"

# The whole point of this test: if dumpsys' format drifts, the parser returns a tidy
# object full of nulls that reads exactly like "Android routed nowhere". This threshold
# is what tells a format change apart from a real measurement.
fail=0
if [ "${MATCHED:-0}" -lt 5 ]; then
  err "parser matched only $MATCHED fields — dumpsys audio format has likely changed"
  err "Fix rig/analysis/audio_state.py before trusting ANY routing assertion."
  fail=1
else
  ok "parser matched $MATCHED known fields"
fi

# NB: do not inline this as a heredoc with `< file` — a heredoc and an input
# redirection both bind stdin, the redirection wins, and Python ends up reading the
# JSON as its own source. Keep it a real script.
"$PY" "$RIG_ROOT/analysis/audio_summary.py" < "$DIR/audio-state.json" >&2

# These fields are what every later Android assertion depends on. Their PRESENCE is the
# test; their values are merely today's state.
for f in audio_mode_actual sco_audio_state sco_audio_mode; do
  if ! jq_field "$f" >/dev/null; then err "field '$f' not found — parser incomplete"; fail=1; fi
done
[ "$fail" -eq 0 ] && ok "all required routing fields present"

MODE="$(jq_field audio_mode_actual)"
SCO="$(jq_field sco_audio_state)"

if [ "$fail" -eq 0 ]; then ok "U32 PASS"; emit_result U32 PASS "$DIR" fields_matched "$MATCHED" audio_mode "$MODE" sco_state "$SCO"
else err "U32 FAIL"; emit_result U32 FAIL "$DIR" fields_matched "$MATCHED"; fi
exit "$fail"
