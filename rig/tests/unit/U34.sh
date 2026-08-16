#!/usr/bin/env bash
# U34 — Call control is available (verifies the mechanism; dials nothing by default)
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U34" "Call control" \
  "the rig can start and end calls on demand, and read call state" \
  "place any call — dialing rings a real person and needs an explicit number"

require_phone
DIR="$(artifact_dir U34-call)"
fail=0

"$RIG_ROOT/adb/call.sh" check 2>&1 | tee "$DIR/call-check.txt" >&2

SIM="$(phone shell 'getprop gsm.sim.state' | tr -d '\r')"
DIALER="$(phone shell 'cmd package resolve-activity -a android.intent.action.CALL -d tel:0000000000 2>/dev/null | grep -m1 packageName' | tr -d '\r')"

[ -n "$DIALER" ] || { err "CALL intent does not resolve — no default phone app?"; fail=1; }

# ENDCALL must be deliverable even with no call up; this proves the input path works
# without needing an active call to test against.
if phone shell 'input keyevent 6' >/dev/null 2>&1; then
  ok "KEYCODE_ENDCALL deliverable (no-op with no active call)"
else
  err "cannot deliver key events"; fail=1
fi

# VoIP apps matter as much as cellular here: PLAN.md's matrix rows J2/J3 test Discord and
# similar, and dumpsys already shows Discord driving MODE_IN_COMMUNICATION on this phone.
echo >&2
info "VoIP apps installed (test-matrix rows J2/J3):"
for pkg in com.discord com.whatsapp org.thoughtcrime.securesms com.google.android.apps.tachyon com.google.android.dialer; do
  if phone shell "pm list packages $pkg" 2>/dev/null | grep -q "$pkg"; then
    printf '    present : %s\n' "$pkg" >&2
  else
    printf '    absent  : %s\n' "$pkg" >&2
  fi
done

echo >&2
warn "This test deliberately does NOT dial. To place a real call:"
warn "    rig phone-call place <number>     then     rig phone-hangup"

if [ "$fail" -eq 0 ]; then ok "U34 PASS"; emit_result U34 PASS "$DIR" sim_state "$SIM" dialer "${DIALER:-none}"
else err "U34 FAIL"; emit_result U34 FAIL "$DIR" sim_state "$SIM"; fi
exit "$fail"
