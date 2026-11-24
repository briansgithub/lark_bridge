#!/usr/bin/env bash
# rig/esp/pi-image/lib.sh - shared helpers for the RIG Pi (the second SD card).
#
# This runs on the rig Pi, never on the product Pi and never on the control PC. The
# control-side twin is rig/lib/common.sh; the logging idiom is kept identical so a
# transcript reads the same from both ends.
#
# Source it, never execute it. In the repo it sits beside the scripts; provision.sh
# installs it to /usr/local/lib/larkbridge-rig/lib.sh.

[ -n "${_RIG_PI_LIB_SH:-}" ] && return 0
_RIG_PI_LIB_SH=1

set -euo pipefail

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

# Rig configuration written by provision.sh. Plain KEY=VALUE shell, so every script
# (and a person) can read it. Environment variables win over the file, so a caller can
# override one value for one run.
RIG_CONF="${RIG_CONF:-/etc/larkbridge-rig/rig.conf}"

rig_load_conf() {
  local key val line
  [ -r "$RIG_CONF" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    case "$key" in *[!A-Z0-9_]*|'') continue ;; esac
    val="${val%\"}"; val="${val#\"}"
    # Environment first: only fill keys that are unset.
    if [ -z "${!key+x}" ]; then
      printf -v "$key" '%s' "$val"
      export "${key?}"
    fi
  done < "$RIG_CONF"
}

# A Bluetooth address, upper case, colon separated.
is_bdaddr() { [[ "$1" =~ ^[0-9A-F]{2}(:[0-9A-F]{2}){5}$ ]]; }

# Escape a string for a JSON string literal (no surrounding quotes).
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"; s="${s//$'\r'/\\r}"; s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}
