#!/usr/bin/env bash
# Windows-side closed-loop evidence and assertion workflow for wired AEC.
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

usage() {
  echo "usage: rig wired-aec baseline|capabilities|bench|speaker-cal|speaker-baseline|speaker-paired|speaker-thermal|test|fault-test|soak|collect [args...]" >&2
  exit 2
}

run_speaker_preflight() {
  local local_dir="$1" remote rc
  remote="/tmp/larkbridge-speaker-preflight-$(timestamp)"
  mkdir -p "$local_dir"
  pi "cd ~/rpi-lark-bridge && python3 rig/pi/measure/speaker_preflight.py --out '$remote'" \
    > "$local_dir/speaker-preflight.json" 2> "$local_dir/speaker-preflight.err"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    ok "speaker preflight PASS"
  elif [ "$rc" -eq 78 ]; then
    warn "speaker not detected at the Lark; wake or reconnect it before continuing"
  else
    err "speaker preflight failed; no acoustic benchmark was started"
  fi
  return "$rc"
}

speaker_series() {
  local label="$1" mode="$2" default_runs="$3"
  shift 3
  local runs="$default_runs" dir remote_base trial remote_trial local_trial escaped
  local has_signal=0 has_profile=0 signal
  local -a bench_args=() bench_jsons=() trial_args=()
  local -a baseline_signals=(sine multitone speech)
  while [ $# -gt 0 ]; do
    case "$1" in
      --runs) runs="${2:?--runs requires a count}"; shift 2 ;;
      --signal) bench_args+=("$1" "${2:?--signal requires a value}"); has_signal=1; shift 2 ;;
      --profile-name) bench_args+=("$1" "${2:?--profile-name requires a value}"); has_profile=1; shift 2 ;;
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
    trial_args=("${bench_args[@]}")
    if [ "$has_signal" -eq 0 ]; then
      if [ "$mode" = baseline ]; then
        signal="${baseline_signals[$(( (trial - 1) % ${#baseline_signals[@]} ))]}"
      else
        signal=sine
      fi
      trial_args+=(--signal "$signal")
    fi
    if [ "$has_profile" -eq 0 ]; then
      trial_args+=(--profile-name "$label-trial-$trial")
    fi
    run_speaker_preflight "$local_trial" || return $?
    escaped=""
    for argument in "${trial_args[@]}"; do
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

speaker_paired() {
  local candidate="" baseline_summary="" pairs=10 seed=1307 dir remote_base pair profile order remote_trial local_trial escaped argument quoted
  local -a common_args=() candidate_args=() bench_jsons=() schedule=() profiles=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --candidate) candidate="${2:?--candidate requires a value}"; shift 2 ;;
      --baseline-summary) baseline_summary="${2:?--baseline-summary requires a path}"; shift 2 ;;
      --pairs) pairs="${2:?--pairs requires a count}"; shift 2 ;;
      --seed) seed="${2:?--seed requires a value}"; shift 2 ;;
      --high-pass-filter|--no-high-pass-filter|--noise-suppression|--no-noise-suppression|--gain-control|--no-gain-control|--transient-suppression|--no-transient-suppression)
        die "paired tuning flags are selected by --candidate"
        ;;
      *) common_args+=("$1"); shift ;;
    esac
  done
  [[ "$pairs" =~ ^[1-9][0-9]*$ ]] || die "--pairs must be a positive integer"
  [[ "$seed" =~ ^[0-9]+$ ]] || die "--seed must be a non-negative integer"
  [ -n "$baseline_summary" ] || die "speaker-paired requires --baseline-summary from a passing speaker baseline"
  [ -s "$baseline_summary" ] || die "baseline summary does not exist: $baseline_summary"
  grep -q '"verdict": "PASS"' "$baseline_summary" || die "paired trials are gated on a passing speaker baseline"
  case "$candidate" in
    high-pass-off) candidate_args=(--no-high-pass-filter) ;;
    noise-suppression) candidate_args=(--noise-suppression) ;;
    gain-control) candidate_args=(--gain-control) ;;
    transient-off) candidate_args=(--no-transient-suppression) ;;
    extended-filter) die "webrtc.extended_filter is absent from the installed SPA library" ;;
    *) die "--candidate must be high-pass-off, noise-suppression, gain-control, or transient-off" ;;
  esac

  require_pi
  dir="$(artifact_dir "wired-aec-paired-$candidate")"
  remote_base="/var/tmp/wired-aec-paired-$candidate-$(timestamp)"
  mapfile -t schedule < <("$PY" -c 'import random,sys; r=random.Random(int(sys.argv[2])); print("\n".join("candidate-first" if r.randrange(2) else "baseline-first" for _ in range(int(sys.argv[1]))))' "$pairs" "$seed")
  printf '%s\n' "${schedule[@]}" > "$dir/order.txt"
  info "running $pairs randomized paired trials for $candidate (seed $seed)"

  for (( pair=1; pair<=pairs; pair++ )); do
    order="${schedule[$((pair - 1))]}"
    if [ "$order" = candidate-first ]; then
      profiles=(candidate baseline)
    else
      profiles=(baseline candidate)
    fi
    for profile in "${profiles[@]}"; do
      printf -v remote_trial '%s/pair-%02d-%s' "$remote_base" "$pair" "$profile"
      printf -v local_trial '%s/pair-%02d-%s' "$dir" "$pair" "$profile"
      mkdir -p "$local_trial"
      local -a run_args=(
        "${common_args[@]}"
        --high-pass-filter --no-noise-suppression --no-gain-control --transient-suppression
        --profile-name "pair-$pair-$profile"
      )
      [ "$profile" = baseline ] || run_args+=("${candidate_args[@]}")
      run_speaker_preflight "$local_trial" || return $?
      escaped=""
      for argument in "${run_args[@]}"; do
        printf -v quoted '%q' "$argument"
        escaped+=" $quoted"
      done
      info "pair $pair/$pairs: $profile"
      pi "cd ~/rpi-lark-bridge && python3 rig/pi/measure/aec_bench.py --out '$remote_trial'$escaped" \
        > "$local_trial/bench-run.json" 2> "$local_trial/bench-run.err" || true
      scp -q -r "$(pi_host):$remote_trial/." "$local_trial/" 2>/dev/null || true
      [ -s "$local_trial/bench.json" ] && bench_jsons+=("$local_trial/bench.json")
    done
  done

  if "$PY" "$RIG_ROOT/analysis/aec_pairs.py" --candidate "$candidate" \
      --min-pairs "$pairs" --seed "$seed" "${bench_jsons[@]}" > "$dir/summary.json"; then
    emit_result "wired-aec-paired-$candidate" PASS "$dir" pairs "$pairs"
    ok "paired $candidate trial PASS (speaker-only, preliminary)"
  else
    emit_result "wired-aec-paired-$candidate" FAIL "$dir" pairs "$pairs"
    err "paired $candidate trial FAIL"
  fi
  info "evidence: $dir"
  [ -s "$dir/result.json" ] && grep -q '"verdict": "PASS"' "$dir/result.json"
}

speaker_thermal() {
  local baseline_summary="" dir remote escaped="" argument quoted fail=0
  local -a thermal_args=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --baseline-summary) baseline_summary="${2:?--baseline-summary requires a path}"; shift 2 ;;
      *) thermal_args+=("$1"); shift ;;
    esac
  done
  [ -n "$baseline_summary" ] || die "speaker-thermal requires --baseline-summary from a passing speaker baseline"
  [ -s "$baseline_summary" ] || die "baseline summary does not exist: $baseline_summary"
  grep -q '"verdict": "PASS"' "$baseline_summary" || die "thermal screen is gated on a passing speaker baseline"
  require_pi
  dir="$(artifact_dir wired-aec-speaker-thermal)"
  remote="/var/tmp/wired-aec-speaker-thermal-$(timestamp)"
  for argument in "${thermal_args[@]}"; do
    printf -v quoted '%q' "$argument"
    escaped+=" $quoted"
  done
  info "running gated speaker-only AEC thermal screen"
  run_speaker_preflight "$dir" || return $?
  pi "cd ~/rpi-lark-bridge && python3 rig/pi/measure/aec_thermal.py --out '$remote'$escaped" \
    > "$dir/thermal-run.json" 2> "$dir/thermal-run.err" || fail=1
  for artifact in thermal.json runtime-samples.json runtime-summary.json pw-top.txt \
      pipewire-journal.txt module-command.txt links-active.txt; do
    scp -q "$(pi_host):$remote/$artifact" "$dir/" 2>/dev/null || true
  done
  if [ "$fail" -eq 0 ] && [ -s "$dir/thermal.json" ] && grep -q '"verdict": "PASS"' "$dir/thermal.json"; then
    emit_result wired-aec-speaker-thermal PASS "$dir"
    ok "speaker thermal screen PASS"
  else
    fail=1
    emit_result wired-aec-speaker-thermal FAIL "$dir"
    err "speaker thermal screen FAIL"
  fi
  info "evidence: $dir"
  return "$fail"
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
    run_speaker_preflight "$dir" || exit $?
    pi "cd ~/rpi-lark-bridge && python3 rig/pi/measure/aec_bench.py --out '$remote' $*" \
      > "$dir/bench-run.json" 2> "$dir/bench-run.err" || fail=1
    scp -q "$(pi_host):$remote/stimulus.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/reference.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/raw-mic.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/clean-mic.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/aec-internal.wav" "$dir/" 2>/dev/null || true
    scp -q "$(pi_host):$remote/bench.json" "$dir/" 2>/dev/null || true
    for artifact in module-command.txt graph-active.json links-active.txt links-recording.txt \
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
  speaker-paired)
    speaker_paired "$@"
    ;;
  speaker-thermal)
    speaker_thermal "$@"
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
