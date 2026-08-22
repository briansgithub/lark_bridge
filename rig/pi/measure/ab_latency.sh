#!/bin/bash
# A/B the AEC graph timing during a live call, alternating conditions in one call.
#
# E10 measured real-call AEC CPU at ~16.7% of one core -- far lighter than the four-core
# load that reproduced the crackle in E11. A clean call at 1920 therefore proves nothing
# on its own: it might be equally clean at the broken setting. Only the paired comparison
# is evidence, which is why this alternates rather than measuring one condition.
#
#     ab_latency.sh [SECONDS_PER] [ROUNDS]
#
# THE CONTROL IS 480, NOT "ABSENT". Commenting node_latency_frames out of bridge.toml
# does NOT reproduce pre-fix behaviour: the key is optional and its CODE default is now
# 1920, so an absent key still yields 1920. A first attempt at this A/B ran all four
# conditions at 1920 and looked like a clean result. TOML has no null, so there is no
# config-only way to express "send no node.latency at all".
#
# 480 is the right proxy regardless. E11 measured production-unset and an explicit 480 as
# indistinguishable -- both request a 10 ms quantum and both drag the onboard sink down to
# min-quantum 256, which is the actual defect.
#
# RESTARTS ARE THE EXPENSIVE PART, not the recording. Restarting the supervisor during an
# active SCO call is the suspected trigger for the E08 controller wedge (four restarts in
# ~2.5 minutes preceded one), and recovering from that costs a human reconnecting the
# phone. So this skips the restart whenever the config already holds the wanted value,
# and the caller should prefer few rounds.
set -uo pipefail

SECONDS_PER=${1:-20}
ROUNDS=${2:-1}
CFG="$HOME/rpi-lark-bridge/config/bridge.toml"
PROBE="$HOME/rpi-lark-bridge/rig/pi/measure/call_capture.py"
BACKUP="/tmp/e12/bridge.toml.ab-backup"
PINNED=1920
CONTROL=480

mkdir -p /tmp/e12
cp "$CFG" "$BACKUP"

restore() {
    echo "--- restoring config to $PINNED ---"
    cp "$BACKUP" "$CFG"
    systemctl --user restart bridge-supervisor.service
}
trap restore EXIT INT TERM

current_value() {
    sed -n 's/^node_latency_frames *= *\([0-9]*\).*/\1/p' "$CFG" | head -1
}

wait_active() {
    local state=""
    for _ in $(seq 1 30); do
        state=$(python3 -c "import json;print(json.load(open('/run/user/1000/bridge-status.json'))['state'])" 2>/dev/null)
        [ "$state" = "ACTIVE" ] && return 0
        sleep 1
    done
    echo "TIMEOUT: supervisor never returned to ACTIVE (last state: ${state:-unknown})" >&2
    return 1
}

set_condition() {
    local want="$1"
    if [ "$(current_value)" = "$want" ]; then
        echo "  (already at $want -- no restart needed)"
        wait_active || return 1
        return 0
    fi
    sed -i "s/^node_latency_frames *=.*/node_latency_frames = $want/" "$CFG"
    systemctl --user restart bridge-supervisor.service
    sleep 2
    wait_active || return 1
    sleep 3   # the graph was just rebuilt; let it settle before measuring
}

for round in $(seq 1 "$ROUNDS"); do
    for want in "$PINNED" "$CONTROL"; do
        label="f${want}r${round}"
        echo "===================== $label (node_latency_frames = $want) ====================="
        if ! set_condition "$want"; then
            echo "ABORT: call graph did not come back for $label" >&2
            exit 1
        fi
        python3 "$PROBE" --label "$label" --seconds "$SECONDS_PER" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  configured latency:', (d.get('aec_before') or {}).get('node_latency_frames'))
print('  links_verified:', d.get('links_verified'), '| error:', d.get('error'))
print('  health:', (d.get('health_after') or {}).get('temperature'), (d.get('health_after') or {}).get('throttled'))
for n,v in sorted((d.get('pwtop') or {}).items()):
    print(f\"    {n:52s} QUANT={v['quantum']:5d} ERRdelta={v['err_delta']:5d} (last={v['err_last']})\")
"
    done
done
echo "===================== done ====================="
