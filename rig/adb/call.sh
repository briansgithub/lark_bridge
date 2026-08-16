#!/usr/bin/env bash
# Call control on the Pixel.
#
# SAFETY: placing a call dials a real number over a real network and rings a real person.
# That is an outward-facing, hard-to-take-back action, so this script never dials unless
# given an explicit number, and `check` verifies the mechanism WITHOUT dialing anything.
# Automated test loops should use `check` plus a number the operator has explicitly
# nominated — never a hardcoded one.
#
#   rig phone-call check              # can we place calls? (dials nothing)
#   rig phone-call place <number>     # dials — requires an explicit number
#   rig phone-hangup

set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

require_phone
ACTION="${1:-check}"; shift || true

case "$ACTION" in

  check)
    # Resolve the CALL intent without firing it, and read telephony readiness.
    STATE="$(phone shell 'dumpsys telephony.registry 2>/dev/null | grep -m1 "mCallState" || echo "mCallState=unknown"' | tr -d '\r')"
    SIM="$(phone shell 'getprop gsm.sim.state' | tr -d '\r')"
    OP="$(phone shell 'getprop gsm.operator.alpha' | tr -d '\r')"
    DIALER="$(phone shell 'cmd package resolve-activity -a android.intent.action.CALL -d tel:0000000000 2>/dev/null | grep -m1 packageName' | tr -d '\r')"

    printf '  sim state    : %s\n' "${SIM:-unknown}" >&2
    printf '  operator     : %s\n' "${OP:-<none>}" >&2
    printf '  call state   : %s\n' "$STATE" >&2
    printf '  dialer       : %s\n' "${DIALER:-<unresolved>}" >&2

    if [ -n "$DIALER" ]; then
      ok "CALL intent resolves — calls are placeable"
    else
      warn "CALL intent did not resolve; check the default phone app"
    fi
    case "$SIM" in
      READY) ok "SIM ready — cellular calls possible" ;;
      *)     warn "SIM state '$SIM' — cellular tests may be unavailable; VoIP apps still work" ;;
    esac
    ;;

  place)
    NUM="${1:-}"
    [ -n "$NUM" ] || die "refusing to dial: no number given.
  Placing a call rings a real person. Pass the number explicitly:
      rig phone-call place +15551234567"
    warn "dialing $NUM — this places a REAL call"
    phone shell "am start -a android.intent.action.CALL -d tel:$NUM" >/dev/null
    sleep 3
    phone shell 'dumpsys telephony.registry | grep -m1 mCallState' | tr -d '\r'
    ok "dialed $NUM"
    ;;

  hangup)
    # KEYCODE_ENDCALL = 6
    phone shell 'input keyevent 6' >/dev/null
    sleep 1
    ok "sent ENDCALL"
    ;;

  *) die "unknown action: $ACTION (check|place|hangup)" ;;
esac
