#!/usr/bin/env bash
# Windows-side closed-loop evidence and assertion workflow for wired AEC.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() {
  echo "usage: rig wired-aec baseline|test|fault-test|soak|collect [args...]" >&2
  exit 2
}

PY="$(command -v py 2>/dev/null || command -v python3 2>/dev/null || true)"
[ -n "$PY" ] || die "Python is required on the control PC"

snapshot() {
  local label="$1" expectation="$2" dir fail=0 status_path
  require_pi
  dir="$(artifact_dir "wired-aec-$label")"
  status_path='/run/user/$(id -u)/bridge-status.json'

  info "collecting $label snapshot"
  git -C "$REPO_ROOT" status --short --branch > "$dir/git-status.txt"
  git -C "$REPO_ROOT" rev-parse HEAD > "$dir/git-head.txt"
  pi 'uname -a; cat /etc/os-release; pipewire --version; wireplumber --version; bluetoothctl --version; dpkg-query -W pipewire libpipewire-0.3-modules libspa-0.2-modules wireplumber 2>/dev/null' \
    > "$dir/versions.txt" 2>&1 || fail=1
  pi 'sha256sum ~/rpi-lark-bridge/pi/bridged/bridge_supervisor.py ~/rpi-lark-bridge/pi/bridged/bt_watchdog.py' \
    > "$dir/deployed-hashes.txt" 2>&1 || fail=1
  pi 'systemctl is-active bluetooth bridge-btfw bridge-btwatchdog bridge-tuning; systemctl --user is-active pipewire wireplumber bridge-supervisor' \
    > "$dir/services.txt" 2>&1 || fail=1
  pi 'pw-dump' > "$dir/pw-dump.json" 2>&1 || fail=1
  pi 'pw-link -l' > "$dir/pw-links.txt" 2>&1 || fail=1
  pi 'timeout 6 pw-top -b -n 3 2>&1 || true' > "$dir/pw-top.txt" 2>&1
  pi 'cd ~/rpi-lark-bridge && python3 rig/analysis/system_health.py' \
    > "$dir/system-health.json" 2>&1 || fail=1
  pi "cat $status_path 2>/dev/null || true" > "$dir/bridge-status.json"
  pi 'journalctl --user -u bridge-supervisor --since=-10min --no-pager 2>&1 || true' \
    > "$dir/bridge-journal.txt"
  pi 'journalctl -u bluetooth -u bridge-btfw -u bridge-btwatchdog --since=-10min --no-pager 2>&1 || true' \
    > "$dir/system-journal.txt"

  if require_phone >/dev/null 2>&1; then
    "$RIG_ROOT/adb/audio-state.sh" --save "$dir" > "$dir/android-audio.txt" 2>&1 || true
  else
    warn "Pixel ADB unavailable; Android state omitted"
  fi

  "$PY" "$RIG_ROOT/analysis/aec_status.py" "$dir/bridge-status.json" --expect "$expectation" \
    > "$dir/assertions.json" || fail=1

  if [ "$fail" -eq 0 ]; then
    emit_result "wired-aec-$label" PASS "$dir" expectation "$expectation"
    ok "$label PASS"
  else
    emit_result "wired-aec-$label" FAIL "$dir" expectation "$expectation"
    err "$label FAIL"
  fi
  info "evidence: $dir"
  return "$fail"
}

command="${1:-}"; shift || true
case "$command" in
  baseline)
    snapshot baseline baseline
    ;;
  test)
    snapshot test active
    ;;
  fault-test)
    require_pi
    info "restarting only bridge-supervisor; PipeWire, WirePlumber, Bluetooth, and SSH stay up"
    pi 'systemctl --user restart bridge-supervisor'
    sleep 12
    if pi 'grep -q '"'"'"state": "ACTIVE"'"'"' /run/user/$(id -u)/bridge-status.json 2>/dev/null'; then
      snapshot fault-test active
    else
      snapshot fault-test safe
    fi
    ;;
  soak)
    exec "$RIG_ROOT/pi/soak/start.sh" wired-aec "$@"
    ;;
  collect)
    exec "$RIG_ROOT/pi/soak/collect.sh" "$@"
    ;;
  *) usage ;;
esac
