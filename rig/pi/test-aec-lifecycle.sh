#!/usr/bin/env bash
# Exercise transactional AEC construction without a real call or Bluetooth mutation.
set -euo pipefail

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
STATUS="${BRIDGE_STATUS:-$XDG_RUNTIME_DIR/bridge-status.json}"
PHONE_MAC="${BRIDGE_PHONE_MAC:-5C:33:7B:CB:BF:C5}"
MAC_NODE="${PHONE_MAC//:/_}"
HFP_SINK="bluez_output.${MAC_NODE}.1"
HFP_SOURCE="bluez_input.${MAC_NODE}.0"
CYCLES="${1:-10}"
HOLD_SECONDS="${HOLD_SECONDS:-0}"

module_ids=()
cleanup() {
  local index
  for (( index=${#module_ids[@]}-1; index>=0; index-- )); do
    pactl unload-module "${module_ids[$index]}" >/dev/null 2>&1 || true
  done
  module_ids=()
}
trap cleanup EXIT INT TERM

node_exists() {
  local nodes
  nodes="$(pw-cli ls Node 2>/dev/null)" || return 1
  grep -Fq "node.name = \"$1\"" <<<"$nodes"
}

status_value() {
  python3 - "$STATUS" "$1" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split('.'):
    value=value[part]
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

wait_state() {
  local wanted="$1" deadline=$((SECONDS + 25)) current=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    current="$(status_value state 2>/dev/null || true)"
    [ "$current" = "$wanted" ] && return 0
    sleep 1
  done
  echo "timed out waiting for $wanted (last state ${current:-missing})" >&2
  return 1
}

wait_active_verified() {
  local deadline=$((SECONDS + 25)) state="" verified=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    state="$(status_value state 2>/dev/null || true)"
    verified="$(status_value aec.verified 2>/dev/null || true)"
    [ "$state" = "ACTIVE" ] && [ "$verified" = "true" ] && return 0
    sleep 1
  done
  echo "timed out waiting for verified ACTIVE (state ${state:-missing}, verified ${verified:-missing})" >&2
  return 1
}

load_fake_hfp() {
  local backing output source
  backing="$(pactl load-module module-null-sink \
    sink_name=bridge.test.hfp.sourceback rate=48000 channels=1 \
    sink_properties='priority.session=1')"
  module_ids+=("$backing")
  output="$(pactl load-module module-null-sink \
    sink_name="$HFP_SINK" rate=48000 channels=1 sink_properties='priority.session=1')"
  module_ids+=("$output")
  source="$(pactl load-module module-remap-source \
    master=bridge.test.hfp.sourceback.monitor source_name="$HFP_SOURCE" \
    rate=48000 channels=1 source_properties='priority.session=1')"
  module_ids+=("$source")
}

[ -s "$STATUS" ] || { echo "bridge status is absent" >&2; exit 1; }
[ "$(status_value aec.enabled)" = "true" ] \
  || { echo "AEC must be enabled before lifecycle testing" >&2; exit 1; }
if node_exists "$HFP_SINK" || node_exists "$HFP_SOURCE"; then
  echo "real or pre-existing HFP nodes are present; refusing synthetic test" >&2
  exit 78
fi

max_temp=0
min_mem=999999999
for (( cycle=1; cycle<=CYCLES; cycle++ )); do
  echo "cycle $cycle/$CYCLES: create" >&2
  load_fake_hfp
  wait_active_verified
  [ "$(status_value aec.verified)" = "true" ]
  [ "$(status_value graph.unexpected_links)" = "[]" ]
  [ "$(status_value graph.missing_links)" = "[]" ]
  node_exists bridge.aec.source
  node_exists bridge.aec.sink
  if [ "$HOLD_SECONDS" != "0" ]; then
    echo "cycle $cycle: holding ACTIVE for ${HOLD_SECONDS}s" >&2
    sleep "$HOLD_SECONDS"
  fi

  if [ "$cycle" -eq $(( (CYCLES + 1) / 2 )) ]; then
    echo "cycle $cycle: restart supervisor with synthetic call active" >&2
    systemctl --user restart bridge-supervisor
    wait_active_verified
    [ "$(status_value aec.verified)" = "true" ]
  fi

  temp="$(status_value system.temperature_c)"
  mem="$(status_value system.mem_available_kib)"
  max_temp="$(awk -v a="$max_temp" -v b="$temp" 'BEGIN{print (b>a)?b:a}')"
  [ "$mem" -lt "$min_mem" ] && min_mem="$mem"

  echo "cycle $cycle/$CYCLES: destroy" >&2
  cleanup
  wait_state CALL_DOWN
  if node_exists bridge.aec.source || node_exists bridge.aec.sink; then
    echo "stale AEC nodes after cycle $cycle" >&2
    exit 1
  fi
done

throttled="$(vcgencmd get_throttled | sed 's/^throttled=//')"
[ "$throttled" = "0x0" ] || { echo "throttle word is $throttled" >&2; exit 1; }
printf '{"verdict":"PASS","cycles":%s,"max_temperature_c":%s,"min_mem_available_kib":%s,"throttled":"%s"}\n' \
  "$CYCLES" "$max_temp" "$min_mem" "$throttled"
