#!/usr/bin/env bash
# U04 — Power integrity (GATES EVERY MEASUREMENT THAT FOLLOWS)
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U04" "Power integrity" \
  "the supply holds up, so later audio/BT faults are real faults" \
  "prove the supply will hold under FULL load — re-run after all peripherals are attached"

require_pi
DIR="$(artifact_dir U04-power)"

# Why this gates everything: a browning-out Pi 3 produces intermittent USB resets,
# audio dropouts and Bluetooth instability — symptoms indistinguishable from the bugs
# this project exists to find. A non-zero throttle word invalidates every measurement
# taken while it was set.
RAW="$(pi 'vcgencmd get_throttled')"
THROT="${RAW#throttled=}"
TEMP="$(pi 'vcgencmd measure_temp')"
VOLT="$(pi 'vcgencmd measure_volts core')"

{ echo "$RAW"; echo "$TEMP"; echo "$VOLT"; } > "$DIR/power.txt"

printf '  throttled : %s\n' "$THROT" >&2
printf '  %s\n' "$TEMP" >&2
printf '  core %s\n' "$VOLT" >&2
echo >&2

decode() {
  local v=$(( $1 )) bit="$2" msg="$3"
  if (( (v >> bit) & 1 )); then err "  bit $bit set: $msg"; return 1; fi
  return 0
}

fail=0
if [ "$THROT" = "0x0" ]; then
  ok "throttled=0x0 — clean supply, measurements are trustworthy"
else
  err "throttled=$THROT — the supply is NOT adequate"
  decode "$THROT" 0  "under-voltage NOW"                || fail=1
  decode "$THROT" 1  "ARM frequency capped NOW"         || fail=1
  decode "$THROT" 2  "currently throttled"              || fail=1
  decode "$THROT" 3  "soft temperature limit active"    || fail=1
  decode "$THROT" 16 "under-voltage HAS OCCURRED since boot" || fail=1
  decode "$THROT" 17 "ARM frequency capping has occurred"    || fail=1
  decode "$THROT" 18 "throttling has occurred"               || fail=1
  decode "$THROT" 19 "soft temperature limit has occurred"   || fail=1
  err "Fix the PSU before continuing — use a >=2.5 A supply and a short, thick cable."
  err "Every audio or Bluetooth result taken in this state is uninterpretable."
fi

TC="${TEMP#temp=}"; TC="${TC%\'C}"
if awk -v t="$TC" 'BEGIN{exit !(t>70)}'; then
  warn "SoC at ${TC}C — thermal throttling is likely under sustained load; add a heatsink"
fi

if [ "$fail" -eq 0 ]; then ok "U04 PASS"; emit_result U04 PASS "$DIR" throttled "$THROT" temp "$TC"
else err "U04 FAIL"; emit_result U04 FAIL "$DIR" throttled "$THROT" temp "$TC"; fi
exit "$fail"
