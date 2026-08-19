#!/usr/bin/env bash
# Windows-side closed-loop evidence and assertion workflow for wired AEC.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() {
  echo "usage: rig wired-aec baseline|capabilities|bench|speaker-cal|speaker-baseline|test|fault-test|soak|collect [args...]" >&2
  exit 2
}

speaker_series() {
  local label="$1" mode="$2" default_runs="$3"
  shift 3
  local runs="$default_runs" dir remote_base trial remote_trial local_trial escaped
  local -a bench_args=() bench_jsons=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --runs) runs="${2:?--runs requires a count}"; shift 2 ;;
      *) bench_args+=("$1"); shift ;;
    esac
  done
  [[ "$runs" =~ ^[1-9][0-9]*$ ]] || die "--runs must be a positive integer"
  require_pi
  dir="$(artifact_dir "wired-aec-$label")"
  remote_base="/var/tmp/wired-aec-$label-$(timestamp)"
  info "running $runs speaker $label trials"
  for (( trial=1; trial<=runs; trial++ )); do
    printf -v remote_trial '%s/trial-%02d' "$remote_base" "$trial"
    printf -v local_trial '%s/trial-%02d' "$dir" "$trial"
    mkdir -p "$local_trial"
    escaped=""
    for argument in "${bench_args[@]}"; do
      printf -v quoted '%q' "$argument"
      escaped+=" $quoted"
    done
    info "$label trial $trial/$runs"
    pi "cd ~/rpi-lark-bridge && python3 rig/pi/measure/aec_bench.py --out '$remote_trial'$escaped" \
      > "$local_trial/bench-run.json" 2> "$local_trial/bench-run.err" || true
    scp -q -r "$(pi_host):$remote_trial/." "$local_trial/" 2>/dev/null || true
    [ -s "$local_trial/bench.json" ] && bench_jsons+=("$local_trial/bench.json")
  done
  if "$PY" "$RIG_ROOT/analysis/aec_baseline.py" \
      --mode "$mode" --min-runs "$runs" "${bench_jsons[@]}" > "$dir/summary.json"; then
    emit_result "wired-aec-$label" PASS "$dir" runs "$runs"
    ok "$label PASS"
  else
    emit_result "wired-aec-$label" FAIL "$dir" runs "$runs"
    err "$label FAIL"
  fi
  info "evidence: $dir"
  [ -s "$dir/result.json" ] && grep -q '"verdict": "PASS"' "$dir/result.json"
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

  if adb_bin >/dev/null 2>&1 && phone get-state >/dev/null 2>&1; then
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
  capabilities)
    require_pi
    dir="$(artifact_dir wired-aec-capabilities)"
    fail=0
    pi 'cd ~/rpi-lark-bridge && python3 rig/pi/measure/aec_capabilities.py' \
      > "$dir/capabilities.json" 2> "$dir/capabilities.err" || fail=1
    if [ "$fail" -eq 0 ] && grep -q '"verdict": "PASS"' "$dir/capabilities.json"; then
      emit_result wired-aec-capabilities PASS "$dir"
      ok "capability report PASS"
    else
      emit_result wired-aec-capabilities FAIL "$dir"
      err "capability report FAIL"
    fi
    info "evidence: $dir"
    exit "$fail"
    ;;
  bench)
    require_pi
    dir="$(artifact_dir wired-aec-bench)"
    remote="/var/tmp/wired-aec-bench-$(timestamp)"
    fail=0
    info "running low-level AUX/speaker/Lark AEC capture"
    pi "cd ~/rpi-lark-bridge && python3 rig/pi/measure/aec_bench.py --out '$remote' $*" \
      > "$dir/bench-run.json" 2> "$dir/bench-run.err" || fail=1
    scp -q "$(pi_host):$remote/reference.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/raw-mic.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/clean-mic.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/bench.json" "$dir/" 2>/dev/null || true
    for artifact in module-command.txt graph-active.json links-active.txt \
      runtime-samples.json runtime-summary.json pw-top.txt pipewire-journal.txt; do
      scp -q "$(pi_host):$remote/$artifact" "$dir/" 2>/dev/null || true
    done
    if [ -s "$dir/bench.json" ] && grep -q '"verdict": "PASS"' "$dir/bench.json"; then
      emit_result wired-aec-bench PASS "$dir"
      ok "bench PASS"
    else
      fail=1
      emit_result wired-aec-bench FAIL "$dir"
      err "bench FAIL or acoustic stimulus was not measurable"
    fi
    info "evidence: $dir"
    exit "$fail"
    ;;
  speaker-cal)
    speaker_series speaker-cal calibration 3 "$@"
    ;;
  speaker-baseline)
    speaker_series speaker-baseline baseline 10 "$@"
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
