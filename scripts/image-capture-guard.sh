#!/usr/bin/env bash
# Quiesce and recover the live appliance around a full-card read.
#
# Install this file in /run before use.  `arm` records the units that were active,
# schedules an independent recovery timer, stops persistent-state writers, syncs,
# and remounts LARKDATA read-only.  `restore` is idempotent and may be called by
# either the capture host or the timer.

set -euo pipefail

STATE_DIR="${LARKBRIDGE_IMAGE_STATE_DIR:-/run/larkbridge-image-capture}"
STATE_FILE="$STATE_DIR/active-units"
TIMER_UNIT="larkbridge-image-capture-recovery"
DATA_MOUNT="/var/lib/larkbridge-persist"
USER_ID="${LARKBRIDGE_USER_ID:-1000}"
USER_NAME="${LARKBRIDGE_USER_NAME:-admin}"
RECOVERY_SECONDS="${LARKBRIDGE_IMAGE_RECOVERY_SECONDS:-2700}"

SYSTEM_UNITS=(
  bluetooth.service
  bridge-btwatchdog@call.service
  bridge-pairing-seal.timer
)
USER_UNITS=(
  bridge-supervisor.service
  bridge-output-remote.service
  pipewire-pulse.service
  wireplumber.service
  pipewire.service
)

user_systemctl() {
  runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR="/run/user/$USER_ID" \
    systemctl --user "$@"
}

record_active_units() {
  : >"$STATE_FILE"
  local unit
  for unit in "${SYSTEM_UNITS[@]}"; do
    if systemctl is-active --quiet "$unit"; then
      printf 'system\t%s\n' "$unit" >>"$STATE_FILE"
    fi
  done
  for unit in "${USER_UNITS[@]}"; do
    if user_systemctl is-active --quiet "$unit"; then
      printf 'user\t%s\n' "$unit" >>"$STATE_FILE"
    fi
  done
}

arm_recovery() {
  systemctl stop "$TIMER_UNIT.timer" "$TIMER_UNIT.service" 2>/dev/null || true
  systemctl reset-failed "$TIMER_UNIT.service" 2>/dev/null || true
  systemd-run \
    --unit="$TIMER_UNIT" \
    --on-active="${RECOVERY_SECONDS}s" \
    --timer-property=AccuracySec=1s \
    /bin/bash /run/larkbridge-image-capture-guard restore >/dev/null
}

stop_units() {
  local unit
  for unit in "${USER_UNITS[@]}"; do
    user_systemctl stop "$unit" 2>/dev/null || true
  done
  for unit in "${SYSTEM_UNITS[@]}"; do
    systemctl stop "$unit" 2>/dev/null || true
  done
}

start_recorded_units() {
  local unit
  [ -f "$STATE_FILE" ] || return 0
  for unit in "${SYSTEM_UNITS[@]}"; do
    grep -Fqx $'system\t'"$unit" "$STATE_FILE" && systemctl start "$unit" || true
  done
  # Start the media session before its consumers, regardless of the order in
  # which active units were recorded.
  for unit in pipewire.service wireplumber.service pipewire-pulse.service \
    bridge-output-remote.service bridge-supervisor.service; do
    grep -Fqx $'user\t'"$unit" "$STATE_FILE" && user_systemctl start "$unit" || true
  done
}

restore() {
  mount -o remount,rw "$DATA_MOUNT" 2>/dev/null || true
  start_recorded_units
  sync
}

case "${1:-}" in
  arm)
    [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
    mkdir -p "$STATE_DIR"
    record_active_units
    arm_recovery
    stop_units
    sync
    mount -o remount,ro "$DATA_MOUNT"
    findmnt -no OPTIONS "$DATA_MOUNT" | grep -qw ro || {
      echo "$DATA_MOUNT did not become read-only" >&2
      restore
      exit 1
    }
    ;;
  restore)
    [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
    restore
    ;;
  disarm)
    [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
    systemctl stop "$TIMER_UNIT.timer" "$TIMER_UNIT.service" 2>/dev/null || true
    systemctl reset-failed "$TIMER_UNIT.service" 2>/dev/null || true
    ;;
  status)
    findmnt -no TARGET,FSTYPE,OPTIONS "$DATA_MOUNT"
    systemctl is-active "$TIMER_UNIT.timer" 2>/dev/null || true
    ;;
  *)
    echo "usage: image-capture-guard.sh arm|restore|disarm|status" >&2
    exit 2
    ;;
esac
