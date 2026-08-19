#!/usr/bin/env bash
# rig/lib/common.sh — shared library for the test rig CONTROL side.
#
# This runs on the Windows dev PC under Git Bash, not on the Pi. The Pi-side
# equivalent is scripts/lib/common.sh; keep the logging idiom identical so output
# from both reads the same in a transcript.
#
# Source it, never execute it.

[ -n "${_RIG_COMMON_SH:-}" ] && return 0
_RIG_COMMON_SH=1

set -euo pipefail

RIG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$RIG_ROOT/.." && pwd)"
RIG_ARTIFACTS="${RIG_ARTIFACTS:-$REPO_ROOT/artifacts}"
export RIG_ROOT REPO_ROOT RIG_ARTIFACTS

# ---------------------------------------------------------------- logging

if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
  _C_RED=$'\033[31m'; _C_YLW=$'\033[33m'; _C_GRN=$'\033[32m'
  _C_DIM=$'\033[2m';  _C_BLD=$'\033[1m';  _C_OFF=$'\033[0m'
else
  _C_RED=''; _C_YLW=''; _C_GRN=''; _C_DIM=''; _C_BLD=''; _C_OFF=''
fi

log()  { printf '%s[ .. ]%s %s\n' "$_C_DIM" "$_C_OFF" "$*" >&2; }
info() { printf '%s[info]%s %s\n' "$_C_BLD" "$_C_OFF" "$*" >&2; }
ok()   { printf '%s[ ok ]%s %s\n' "$_C_GRN" "$_C_OFF" "$*" >&2; }
warn() { printf '%s[warn]%s %s\n' "$_C_YLW" "$_C_OFF" "$*" >&2; }
err()  { printf '%s[FAIL]%s %s\n' "$_C_RED" "$_C_OFF" "$*" >&2; }
die()  { err "$*"; exit 1; }

timestamp() { date -u +%Y%m%dT%H%M%SZ; }

# Hardware absence is a PAUSE, not a failure. The working agreement with the operator
# is that a missing device means "stop and ask", never "improvise around it".
need_hardware() {
  local what="$1" hint="${2:-}"
  printf '\n%s========================================================%s\n' "$_C_BLD" "$_C_OFF" >&2
  printf '%s  PAUSED - HARDWARE NOT PRESENT%s\n' "$_C_YLW" "$_C_OFF" >&2
  printf '  Expected: %s\n' "$what" >&2
  [ -n "$hint" ] && printf '  %s\n' "$hint" >&2
  printf '  Connect it and re-run this test.\n' >&2
  printf '%s========================================================%s\n\n' "$_C_BLD" "$_C_OFF" >&2
  exit 78   # EX_CONFIG - distinguishable from a real test failure (1)
}

# ---------------------------------------------------------------- inventory

rig_inventory() {
  local f="$RIG_ROOT/inventory.toml"
  [ -f "$f" ] || die "missing $f - copy inventory.toml.example and fill it in"
  printf '%s\n' "$f"
}

# Minimal TOML scalar reader. The inventory is deliberately flat so this stays a small
# awk rather than a dependency.
#
# Do NOT assign to $1 here: awk rebuilds $0 using OFS when a field is modified, which
# replaces the "=" with a space and silently returns the whole line. That bug cost a
# debugging round; the code below only ever reads fields.
inv() {
  local key="$1" default="${2:-}" val
  val="$(awk -v k="$key" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/[[:space:]]+#.*$/, "", line)          # strip trailing comment
      if (line !~ /=/) next
      name = line; sub(/=.*$/, "", name)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
      if (name != k) next
      val = line; sub(/^[^=]*=[[:space:]]*/, "", val)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
      gsub(/^"|"$/, "", val)
      print val; exit
    }
  ' "$(rig_inventory)")"
  printf '%s\n' "${val:-$default}"
}

# ---------------------------------------------------------------- pi access

pi_host() { inv pi_host larkbridge; }

# Every invocation is a fresh SSH connection: Windows OpenSSH has no ControlMaster,
# and the calling agent's shell is stateless anyway. BatchMode means a missing key
# fails loudly instead of hanging on a password prompt.
#
# XDG_RUNTIME_DIR is exported for EVERY command, not just the ones that obviously need
# it. A non-login ssh session does not get it, and without it `systemctl --user` and
# `systemd-run --user` silently target the wrong (or no) manager. That cost a debugging
# round: a detached soak appeared to launch and then reported "unit could not be found".
pi() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$(pi_host)" \
    "export XDG_RUNTIME_DIR=/run/user/\$(id -u); $*"
}

pi_sudo() {
  pi "sudo $*"
}

require_pi() {
  pi true 2>/dev/null || die "cannot reach the Pi at $(pi_host).
  Check: it is powered, on Ethernet, and 'ssh $(pi_host) true' works.
  If the IP moved, update rig/inventory.toml and ~/.ssh/config."
}

# Copy the repo to the Pi. rsync is absent on Windows, so stream a tarball.
pi_sync() {
  # shellcheck disable=SC2088 # The remote shell, not this control PC, expands it.
  local dest; dest="$(inv pi_repo_path '~/rpi-lark-bridge')"
  info "syncing repo -> $(pi_host):$dest"
  pi "mkdir -p $dest"
  ( cd "$REPO_ROOT" && tar -cz \
      --exclude='.git' --exclude='artifacts' --exclude='.venv' \
      --exclude='.pio' --exclude='__pycache__' --exclude='rig/adb/platform-tools' \
      . ) | pi "tar -xz -C $dest"
  ok "synced"
}

# ---------------------------------------------------------------- phone access

adb_bin() {
  local local_adb="$RIG_ROOT/adb/platform-tools/adb.exe"
  if [ -x "$local_adb" ]; then printf '%s\n' "$local_adb"
  elif command -v adb >/dev/null 2>&1; then command -v adb
  else return 1; fi
}

phone() {
  local a; a="$(adb_bin)" || die "adb not found. Run: rig setup-adb"
  local serial; serial="$(inv phone_serial '')"
  if [ -n "$serial" ]; then "$a" -s "$serial" "$@"; else "$a" "$@"; fi
}

require_phone() {
  adb_bin >/dev/null 2>&1 || die "adb not installed - run: rig setup-adb"
  local n; n="$(phone devices 2>/dev/null | grep -cE '\sdevice$' || true)"
  [ "${n:-0}" -ge 1 ] || need_hardware "Pixel 7a over ADB" \
    "USB-C to this PC (Mode 1 testing) or wireless debugging on abridge-village_5G (Mode 2)."
}

# ---------------------------------------------------------------- artifacts + results

artifact_dir() {
  local name="$1" dir
  dir="$RIG_ARTIFACTS/${name}-$(timestamp)"
  mkdir -p "$dir"
  printf '%s\n' "$dir"
}

# Every unit test emits the same result shape so `rig unit all` can summarise
# without parsing prose.
emit_result() {
  local id="$1" verdict="$2" dir="$3"; shift 3
  {
    printf '{\n'
    printf '  "id": "%s",\n' "$id"
    printf '  "verdict": "%s",\n' "$verdict"
    printf '  "timestamp": "%s",\n' "$(timestamp)"
    printf '  "artifacts": "%s"' "$dir"
    while [ $# -ge 2 ]; do
      printf ',\n  "%s": "%s"' "$1" "$2"; shift 2
    done
    printf '\n}\n'
  } > "$dir/result.json"
}

# Standard header every unit test prints. Stating what a test does NOT prove is not
# decoration - it is what stops a green tick from being over-read later.
unit_header() {
  local id="$1" title="$2" proves="$3" not_proves="$4"
  printf '\n%s== %s - %s ==%s\n' "$_C_BLD" "$id" "$title" "$_C_OFF" >&2
  printf '   proves      : %s\n' "$proves" >&2
  printf '   does NOT    : %s\n' "$not_proves" >&2
  printf '\n' >&2
}
