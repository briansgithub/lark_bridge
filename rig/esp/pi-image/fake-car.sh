#!/usr/bin/env bash
# fake-car.sh - the rig Pi's onboard Bluetooth as a FAKE CAR (A2DP sink + AVRCP CT).
#
#   fake-car pair-accept [seconds] [--until-paired]   open a pairing window (default 120 s)
#   fake-car record <seconds> <out.wav>               record what the 32UE streams to us
#   fake-car avrcp play|pause|next|prev [--device ADDR]
#   fake-car status                                   JSON; exit 0 when the car is ready
#
# Everything runs against the controller pinned in /etc/larkbridge-rig/rig.conf
# (RIG_BT_ADDR), never "the default adapter". Missing hardware exits 78, like rig/lib.
# Hold the rig lease (rig-lease acquire) before pair-accept, record or avrcp.
set -euo pipefail
HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$HERE/lib.sh"
rig_load_conf

# A non-login SSH session has no XDG_RUNTIME_DIR, and PipeWire tools then find no daemon.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
SINK="${RIG_FAKE_CAR_SINK:-larkbridge_fake_car}"
EX_NOHW=78

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

adapter_path() {
  local addr="${RIG_BT_ADDR:-}" idx
  is_bdaddr "$addr" || { err "RIG_BT_ADDR is not set in $RIG_CONF"; exit "$EX_NOHW"; }
  idx="$(bt_pinned_index "$addr")" || { err "pinned controller $addr is not present"; exit "$EX_NOHW"; }
  printf '/org/bluez/%s\n' "$idx"
}

cmd_pair_accept() {
  local seconds=120 until=() adapter idx rc=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --until-paired) until=(--until-paired) ;;
      *[!0-9]*|'') die "pair-accept: seconds must be a number, got '$1'" ;;
      *) seconds="$1" ;;
    esac
    shift
  done
  [ "$seconds" -ge 5 ] && [ "$seconds" -le 600 ] || die "pair-accept: seconds must be 5..600"
  adapter="$(adapter_path)"
  idx="${adapter##*/hci}"
  info "pairing window open for ${seconds}s on $adapter"
  python3 "$HERE/fake_car_agent.py" --adapter "$adapter" --seconds "$seconds" "${until[@]}" || rc=$?
  # BlueZ 5.82 clears Connectable when Discoverable/Pairable are turned off;
  # re-assert connectable so bonded peers (32UE) can reconnect after reboots.
  (sleep 0.5) | timeout 5 btmgmt --index "$idx" connectable on >/dev/null 2>&1 ||
    (sleep 0.5) | timeout 5 sudo -n btmgmt --index "$idx" connectable on >/dev/null 2>&1 ||
    warn "could not re-enable connectable on hci$idx after pair-accept"
  return "$rc"
}

a2dp_streams() {
  pw-dump 2>/dev/null | python3 -c '
import json, sys
try:
    objs = json.load(sys.stdin)
except ValueError:
    objs = []
for o in objs:
    name = ((o.get("info") or {}).get("props") or {}).get("node.name", "")
    if o.get("type") == "PipeWire:Interface:Node" and name.startswith("bluez_input."):
        print(name)
'
}

cmd_record() {
  [ $# -eq 2 ] || die "usage: fake-car record <seconds> <out.wav>"
  local seconds="$1" out="$2" streams rc=0
  case "$seconds" in *[!0-9]*|'') die "record: seconds must be a whole number" ;; esac
  [ "$seconds" -ge 1 ] && [ "$seconds" -le 7200 ] || die "record: seconds must be 1..7200"
  command -v pw-record >/dev/null 2>&1 || die "pw-record not installed (pipewire-bin)"
  # Read all of pw-dump so pipefail cannot turn grep's early exit into a false miss.
  pw-dump 2>/dev/null | grep -F "\"node.name\": \"$SINK\"" >/dev/null ||
    { err "recording tap $SINK not found: is the rig user's PipeWire running?"; exit "$EX_NOHW"; }
  streams="$(a2dp_streams | paste -sd, -)"
  [ -n "$streams" ] || warn "no A2DP stream is connected yet; the recording will be silence"
  mkdir -p "$(dirname "$out")"
  # timeout sends SIGINT so pw-record finalizes the WAV header; 124 is the normal exit.
  timeout --signal=INT --kill-after=5 "$seconds" \
    pw-record --target "$SINK" -P '{ stream.capture.sink = true }' \
      --rate 48000 --channels 2 --format s16 "$out" || rc=$?
  [ "$rc" = 0 ] || [ "$rc" = 124 ] || die "pw-record exited $rc"
  python3 - "$out" "$seconds" "$streams" <<'PY'
import json, sys, wave
path, requested, streams = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with wave.open(path, "rb") as w:
    frames, rate, channels = w.getnframes(), w.getframerate(), w.getnchannels()
print(json.dumps({"ok": True, "path": path, "seconds_requested": requested,
                  "seconds_recorded": round(frames / rate, 3) if rate else 0,
                  "rate": rate, "channels": channels,
                  "a2dp_streams_at_start": [s for s in streams.split(",") if s]}))
PY
}

cmd_avrcp() {
  local action="${1:-}" device="" adapter method player="" path
  shift || true
  [ "${1:-}" = --device ] && { device="$(printf '%s' "${2:?--device needs an address}" | tr '[:lower:]' '[:upper:]')"; }
  case "$action" in
    play) method=Play ;; pause) method=Pause ;; next) method=Next ;; prev) method=Previous ;;
    *) die "usage: fake-car avrcp play|pause|next|prev [--device ADDR]" ;;
  esac
  adapter="$(adapter_path)"
  local dev_prefix="$adapter/dev_"
  [ -n "$device" ] && dev_prefix="$adapter/dev_${device//:/_}"
  # Prefer the MediaPlayer1 object BlueZ creates for a remote AVRCP target.
  while IFS= read -r path; do
    case "$path" in "$dev_prefix"*/player*) player="$path"; break ;; esac
  done < <(busctl --list tree org.bluez 2>/dev/null)
  if [ -n "$player" ]; then
    busctl call org.bluez "$player" org.bluez.MediaPlayer1 "$method"
    printf '{"ok": true, "action": "%s", "via": "MediaPlayer1", "path": "%s"}\n' "$action" "$player"
    return 0
  fi
  # Fallback: the older MediaControl1 interface on the connected device itself.
  while IFS= read -r path; do
    case "$path" in "$dev_prefix"*) ;; *) continue ;; esac
    [[ "$path" =~ /dev_[0-9A-F_]{17}$ ]] || continue
    if [ "$(busctl get-property org.bluez "$path" org.bluez.Device1 Connected 2>/dev/null)" = "b true" ]; then
      busctl call org.bluez "$path" org.bluez.MediaControl1 "$method"
      printf '{"ok": true, "action": "%s", "via": "MediaControl1", "path": "%s"}\n' "$action" "$path"
      return 0
    fi
  done < <(busctl --list tree org.bluez 2>/dev/null)
  printf '{"ok": false, "action": "%s", "error": "no connected AVRCP target%s"}\n' \
    "$action" "${device:+ for $device}"
  return 1
}

cmd_status() {
  local adapter tmp rc=0
  adapter="$(adapter_path)"
  tmp="$(mktemp -d)"
  busctl --json=short call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects \
    > "$tmp/objects.json" 2>/dev/null || true
  pw-dump > "$tmp/pw.json" 2>/dev/null || true
  python3 "$HERE/fake_car_status.py" --objects "$tmp/objects.json" --pwdump "$tmp/pw.json" \
    --adapter "$adapter" --sink "$SINK" || rc=$?
  rm -rf "$tmp"
  return "$rc"
}

command="${1:-}"
shift || true
case "$command" in
  pair-accept) cmd_pair_accept "$@" ;;
  record) cmd_record "$@" ;;
  avrcp) cmd_avrcp "$@" ;;
  status) cmd_status ;;
  -h|--help|'') usage ;;
  *) usage >&2; exit 2 ;;
esac
