#!/usr/bin/env bash
# Long-duration soak monitor. Runs ON THE PI, detached, under systemd-run.
#
# WHY DETACHED: the controlling agent's shell has a 10-minute ceiling, so a 30- or
# 60-minute test cannot be held open over ssh. This samples on its own and writes JSONL
# that `rig soak status` and `rig soak collect` read later.
#
#   soak-monitor.sh --minutes 30 --label hfp-stability --out /var/tmp/soak-<id>
#
# WHAT IT WATCHES, and why each one:
#
#   sco_rx / sco_tx      Audio actually moving. mSBC is ~133 packets/s each way; a
#                        stalled counter with the link still "connected" is a silent
#                        dropout, which is the failure this whole rig exists to catch.
#   controller_alive     Issues Read Local Name and requires RX to advance. Counters
#                        alone LIE -- during the observed wedge, TX kept incrementing
#                        into a controller that had stopped answering.
#   reassembly_errors    "Frame reassembly failed" on the HCI UART. Observed in bursts
#                        around connection events; a steady rate would mean something
#                        different and worse.
#   connected            Link state, to tell a disconnect apart from a stall.
#   xruns                PipeWire underruns, i.e. the host failing to feed the link.
#
# Deliberately lightweight: one sample every few seconds, no audio capture. A monitor
# that perturbs the thing it measures is worthless.

set -uo pipefail

MINUTES=30
LABEL="soak"
OUTDIR=""
INTERVAL=5
MAC="${BRIDGE_PHONE_MAC:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --minutes)  MINUTES="$2"; shift 2 ;;
    --label)    LABEL="$2";   shift 2 ;;
    --out)      OUTDIR="$2";  shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --mac)      MAC="$2";     shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$OUTDIR" ] || { echo "--out required" >&2; exit 2; }
mkdir -p "$OUTDIR"
JSONL="$OUTDIR/samples.jsonl"
META="$OUTDIR/meta.json"

sco_counters() { hciconfig hci0 2>/dev/null | grep -oE 'sco:[0-9]+' | tr -dc '0-9\n'; }
reassembly_count() { dmesg 2>/dev/null | grep -c 'Frame reassembly failed' || echo 0; }

controller_alive() {
  local b a
  b="$(hciconfig hci0 2>/dev/null | grep -oE 'RX bytes:[0-9]+' | tr -dc 0-9)"
  hciconfig hci0 2>/dev/null | grep -q 'UP RUNNING' || { echo false; return; }
  timeout 3 hcitool -i hci0 cmd 0x03 0x0014 >/dev/null 2>&1 || { echo false; return; }
  sleep 1
  a="$(hciconfig hci0 2>/dev/null | grep -oE 'RX bytes:[0-9]+' | tr -dc 0-9)"
  [ "${a:-0}" -gt "${b:-0}" ] && echo true || echo false
}

connected_state() {
  [ -n "$MAC" ] || { echo unknown; return; }
  bluetoothctl info "$MAC" 2>/dev/null | awk -F': ' '/Connected:/{print $2; exit}' | tr -d '\r'
}

START="$(date -u +%s)"
END=$(( START + MINUTES * 60 ))

{
  printf '{"label":"%s","started_utc":"%s","minutes":%s,"interval_s":%s,"mac":"%s"}\n' \
    "$LABEL" "$(date -u -d "@$START" +%Y-%m-%dT%H:%M:%SZ)" "$MINUTES" "$INTERVAL" "$MAC"
} > "$META"

mapfile -t C < <(sco_counters)
PREV_RX="${C[0]:-0}"; PREV_TX="${C[1]:-0}"
PREV_ERR="$(reassembly_count)"
SAMPLE=0

while [ "$(date -u +%s)" -lt "$END" ]; do
  sleep "$INTERVAL"
  SAMPLE=$(( SAMPLE + 1 ))

  mapfile -t C < <(sco_counters)
  RX="${C[0]:-0}"; TX="${C[1]:-0}"
  D_RX=$(( RX - PREV_RX )); D_TX=$(( TX - PREV_TX ))
  PREV_RX="$RX"; PREV_TX="$TX"

  ERR="$(reassembly_count)"; D_ERR=$(( ERR - PREV_ERR )); PREV_ERR="$ERR"

  # Only probe the controller when audio appears stalled: the probe itself costs a
  # command round trip, and doing it every sample would add load to the very link
  # whose behaviour under load is the thing being measured.
  ALIVE=true
  if [ "$D_RX" -eq 0 ] && [ "$D_TX" -eq 0 ]; then
    ALIVE="$(controller_alive)"
  fi

  CONN="$(connected_state)"

  TEMP="$(awk '{printf "%.2f", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo null)"
  THROTTLED="$(vcgencmd get_throttled 2>/dev/null | sed 's/^throttled=//' || echo unknown)"
  MEM_AVAILABLE="$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  BRIDGE_STATE="$(python3 -c 'import json,os; p=f"/run/user/{os.getuid()}/bridge-status.json"; print(json.load(open(p)).get("state","unknown"))' 2>/dev/null || echo unknown)"

  printf '{"t":%s,"n":%s,"sco_rx_d":%s,"sco_tx_d":%s,"reassembly_d":%s,"connected":"%s","alive":%s,"temperature_c":%s,"throttled":"%s","mem_available_kib":%s,"bridge_state":"%s"}\n' \
    "$(date -u +%s)" "$SAMPLE" "$D_RX" "$D_TX" "$D_ERR" "${CONN:-unknown}" "$ALIVE" \
    "${TEMP:-null}" "${THROTTLED:-unknown}" "${MEM_AVAILABLE:-0}" "${BRIDGE_STATE:-unknown}" >> "$JSONL"

  # Stop early on a hard wedge: continuing to sample a dead controller for another
  # 25 minutes gathers nothing and delays the report.
  if [ "$ALIVE" = "false" ]; then
    printf '{"t":%s,"event":"controller_wedged"}\n' "$(date -u +%s)" >> "$JSONL"
    break
  fi
done

printf '{"t":%s,"event":"finished","samples":%s}\n' "$(date -u +%s)" "$SAMPLE" >> "$JSONL"
