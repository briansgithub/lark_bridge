#!/usr/bin/env bash
# Shared shell library for rpi-lark-bridge.
# Source it, never execute it:   . "$(dirname "$0")/../scripts/lib/common.sh"
#
# Design rules for everything that sources this file:
#   - every mutation is idempotent and backs up what it touches
#   - every mutation is fenced so uninstall.sh can remove exactly our lines
#   - nothing is silently skipped; skipping is logged

[ -n "${_BRIDGE_COMMON_SH:-}" ] && return 0
_BRIDGE_COMMON_SH=1

set -euo pipefail

# ---------------------------------------------------------------- paths + identity

BRIDGE_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export BRIDGE_REPO_ROOT
BRIDGE_ARTIFACTS="${BRIDGE_ARTIFACTS:-$BRIDGE_REPO_ROOT/artifacts}"
export BRIDGE_ARTIFACTS
BRIDGE_USER="${BRIDGE_USER:-bridge}"
BRIDGE_FENCE_OPEN='# >>> rpi-lark-bridge >>>'
BRIDGE_FENCE_CLOSE='# <<< rpi-lark-bridge <<<'

bridge_version() { cat "$BRIDGE_REPO_ROOT/VERSION" 2>/dev/null || echo "unknown"; }
timestamp()      { date -u +%Y%m%dT%H%M%SZ; }

# ---------------------------------------------------------------- logging

if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
  _C_RED=$'\033[31m'; _C_YLW=$'\033[33m'; _C_GRN=$'\033[32m'
  _C_DIM=$'\033[2m';  _C_BLD=$'\033[1m';  _C_OFF=$'\033[0m'
else
  _C_RED=''; _C_YLW=''; _C_GRN=''; _C_DIM=''; _C_BLD=''; _C_OFF=''
fi

log()   { printf '%s[ .. ]%s %s\n'  "$_C_DIM" "$_C_OFF" "$*" >&2; }
info()  { printf '%s[info]%s %s\n'  "$_C_BLD" "$_C_OFF" "$*" >&2; }
ok()    { printf '%s[ ok ]%s %s\n'  "$_C_GRN" "$_C_OFF" "$*" >&2; }
warn()  { printf '%s[warn]%s %s\n'  "$_C_YLW" "$_C_OFF" "$*" >&2; }
err()   { printf '%s[FAIL]%s %s\n'  "$_C_RED" "$_C_OFF" "$*" >&2; }
die()   { err "$*"; exit 1; }

# A visually distinct banner for the manual steps in spike scripts, which are easy to miss
# when they scroll past between two walls of btmon output.
prompt_user() {
  printf '\n%s========================================================%s\n' "$_C_BLD" "$_C_OFF" >&2
  printf '%s  ACTION REQUIRED%s\n' "$_C_YLW" "$_C_OFF" >&2
  printf '%s\n' "$*" >&2
  printf '%s========================================================%s\n\n' "$_C_BLD" "$_C_OFF" >&2
}

# ---------------------------------------------------------------- guards

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root (try: sudo $0 $*)"
}

require_not_root() {
  [ "$(id -u)" -ne 0 ] || die "must NOT run as root — this touches the '$BRIDGE_USER' user session"
}

require_cmd() {
  local missing=0 c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || { err "missing required command: $c"; missing=1; }
  done
  [ "$missing" -eq 0 ] || die "install the missing tools and re-run"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

require_linux() {
  [ "$(uname -s)" = "Linux" ] || die "this script only runs on the Raspberry Pi (Linux), not $(uname -s)"
}

# Refuse to run on hardware this project has not been validated against, rather than
# producing confusing results. Override with BRIDGE_ALLOW_ANY_MODEL=1 at your own risk.
require_pi_model() {
  local want="${1:-Raspberry Pi 3 Model B}" model=""
  [ -r /proc/device-tree/model ] && model="$(tr -d '\0' </proc/device-tree/model)"
  if [ -z "$model" ]; then
    [ "${BRIDGE_ALLOW_ANY_MODEL:-0}" = "1" ] || die "cannot read /proc/device-tree/model — not a Raspberry Pi?"
    warn "unknown model, continuing because BRIDGE_ALLOW_ANY_MODEL=1"
    return 0
  fi
  info "detected: $model"
  case "$model" in
    *"$want"*) return 0 ;;
    *)
      [ "${BRIDGE_ALLOW_ANY_MODEL:-0}" = "1" ] \
        || die "expected '$want', found '$model'. Set BRIDGE_ALLOW_ANY_MODEL=1 to override."
      warn "model mismatch overridden by BRIDGE_ALLOW_ANY_MODEL=1"
      ;;
  esac
}

# ---------------------------------------------------------------- artifacts

# Every experiment writes into its own timestamped directory. Nothing overwrites
# anything, because the whole point of the spikes is comparing runs.
artifact_dir() {
  local name="$1" dir
  dir="$BRIDGE_ARTIFACTS/${name}-$(timestamp)"
  mkdir -p "$dir"
  printf '%s\n' "$dir"
}

# Capture a command's output into the artifact dir, tolerating absent tools so that
# one missing utility never aborts a 60-minute test run.
capture() {
  local dir="$1" name="$2"; shift 2
  if ! have_cmd "$1"; then
    printf '(command not available: %s)\n' "$1" > "$dir/$name.txt"
    warn "skipped capture '$name' — $1 not installed"
    return 0
  fi
  { printf '$ %s\n\n' "$*"; "$@" 2>&1 || printf '\n(exit status %d)\n' "$?"; } > "$dir/$name.txt"
  log "captured $name"
}

# ---------------------------------------------------------------- file mutation

backup_file() {
  local f="$1"
  [ -e "$f" ] || return 0
  [ -e "$f.bridge.bak" ] && { log "backup already exists for $f"; return 0; }
  cp -a "$f" "$f.bridge.bak"
  log "backed up $f -> $f.bridge.bak"
}

# Append a fenced block, replacing any previous block we own. Idempotent by construction:
# running install.sh twice produces a byte-identical file.
idempotent_block() {
  local file="$1" content="$2" tmp
  backup_file "$file"
  mkdir -p "$(dirname "$file")"
  touch "$file"
  tmp="$(mktemp)"
  awk -v o="$BRIDGE_FENCE_OPEN" -v c="$BRIDGE_FENCE_CLOSE" '
    $0 == o { skip = 1 } !skip { print } $0 == c { skip = 0 }
  ' "$file" > "$tmp"
  {
    printf '%s\n' "$BRIDGE_FENCE_OPEN"
    printf '%s\n' "$content"
    printf '%s\n' "$BRIDGE_FENCE_CLOSE"
  } >> "$tmp"
  if cmp -s "$tmp" "$file"; then
    log "unchanged: $file"
    rm -f "$tmp"
  else
    cat "$tmp" > "$file"
    rm -f "$tmp"
    ok "updated: $file"
  fi
}

remove_block() {
  local file="$1" tmp
  [ -e "$file" ] || return 0
  tmp="$(mktemp)"
  awk -v o="$BRIDGE_FENCE_OPEN" -v c="$BRIDGE_FENCE_CLOSE" '
    $0 == o { skip = 1 } !skip { print } $0 == c { skip = 0 }
  ' "$file" > "$tmp"
  cat "$tmp" > "$file"; rm -f "$tmp"
  ok "removed our block from: $file"
}

install_file() {
  local src="$1" dst="$2" mode="${3:-0644}"
  [ -e "$src" ] || die "source file missing: $src"
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ] && cmp -s "$src" "$dst"; then
    log "unchanged: $dst"
    return 0
  fi
  backup_file "$dst"
  install -m "$mode" "$src" "$dst"
  ok "installed: $dst"
}

# ---------------------------------------------------------------- bluetooth helpers

bt_adapter() { printf '%s\n' "${BRIDGE_HCI:-hci0}"; }

bt_adapter_present() {
  [ -d "/sys/class/bluetooth/$(bt_adapter)" ]
}

require_bt_adapter() {
  bt_adapter_present || die "no Bluetooth adapter at $(bt_adapter) — check 'rfkill list' and 'systemctl status bluetooth'"
}

# Read Local Supported Features and report whether the controller claims SCO/eSCO at all.
# This is the cheapest possible sanity check before spending an hour on spike S1.
bt_feature_summary() {
  local hci; hci="$(bt_adapter)"
  if have_cmd hciconfig; then
    hciconfig "$hci" features 2>/dev/null || true
  else
    warn "hciconfig not available; install 'bluez' with deprecated tools or read /sys directly"
  fi
}

# ---------------------------------------------------------------- misc

confirm() {
  local msg="${1:-Continue?}"
  [ "${BRIDGE_ASSUME_YES:-0}" = "1" ] && { log "auto-confirmed: $msg"; return 0; }
  printf '%s [y/N] ' "$msg" >&2
  local reply; read -r reply
  case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

countdown() {
  local secs="$1" msg="${2:-waiting}"
  local i
  for (( i=secs; i>0; i-- )); do
    printf '\r%s[ .. ]%s %s — %ds remaining   ' "$_C_DIM" "$_C_OFF" "$msg" "$i" >&2
    sleep 1
  done
  printf '\r%*s\r' 70 '' >&2
}

# Kill a background pid on exit without complaining if it already died.
_bridge_cleanup_pids=()
cleanup_pid_on_exit() { _bridge_cleanup_pids+=("$1"); }
_bridge_cleanup() {
  local p
  for p in "${_bridge_cleanup_pids[@]:-}"; do
    [ -n "$p" ] && kill "$p" 2>/dev/null || true
  done
}
trap _bridge_cleanup EXIT
