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

# Print "hciN ADDR" for every controller the kernel knows. `btmgmt info` is a read-only
# management command that an unprivileged user may run. The hci index is NOT stable
# across boots or replugs; the address is, so callers match on the address.
#   hci0:   Primary controller
#           addr B8:27:EB:12:34:56 version 8 manufacturer 305 class 0x000000
parse_btmgmt_info() {
  awk '
    /^hci[0-9]+:/ { idx = $1; sub(/:$/, "", idx); next }
    idx != "" && $1 == "addr" { print idx, toupper($2); idx = "" }
  '
}

bt_controllers() {
  command -v btmgmt >/dev/null 2>&1 || return 1
  # BlueZ 5.82 btmgmt exits before printing if stdin hits immediate EOF (e.g. /dev/null
  # under systemd), so keep stdin open briefly without blocking.
  (sleep 0.5) | timeout 10 btmgmt info 2>/dev/null | parse_btmgmt_info
}

# hci index of the pinned controller, or nothing.
bt_pinned_index() {
  local want="$1" idx addr
  while read -r idx addr; do
    [ "$addr" = "$want" ] && { printf '%s\n' "$idx"; return 0; }
  done < <(bt_controllers || true)
  return 1
}

# ---------------------------------------------------------------- product-card guard
#
# The rig image is a SEPARATE SD card. These print one reason per line when the system
# looks like the product LarkBridge card (or any overlayroot card), and print nothing on a
# fresh Raspberry Pi OS Lite. provision.sh refuses on any output; there is no override.

# Boot partition of a root: Bookworm/Trixie mount it at /boot/firmware.
boot_dir() {
  local root="$1"
  if [ -f "$root/boot/firmware/config.txt" ] || [ -f "$root/boot/firmware/cmdline.txt" ]; then
    printf '%s\n' "$root/boot/firmware"
  else
    printf '%s\n' "$root/boot"
  fi
}

# True when a kernel command line asks for an overlay root (raspi-config's boot=overlay,
# or Debian's overlayroot=<anything but disabled>).
cmdline_has_overlay() {
  local token tokens
  [ -r "$1" ] || return 1
  read -r -a tokens < "$1" || true
  for token in "${tokens[@]}"; do
    case "$token" in
      boot=overlay) return 0 ;;
      overlayroot=|overlayroot=disabled|overlayroot=\"disabled\") ;;
      overlayroot=*) return 0 ;;
    esac
  done
  return 1
}

# File evidence under ROOT ("" is the running system's /).
product_card_reasons() {
  local root="$1" path boot
  for path in \
      /etc/overlayroot.local.conf \
      /usr/share/initramfs-tools/scripts/init-bottom/overlayroot \
      /usr/local/lib/rpi-lark-bridge \
      /var/lib/rpi-lark-bridge \
      /var/lib/larkbridge-persist \
      /etc/bluetooth/main.conf.d/10-bridge.conf; do
    [ -e "$root$path" ] && printf 'product/overlayroot file present: %s\n' "$path"
  done
  if [ -f "$root/etc/overlayroot.conf" ] &&
     grep -Eq '^[[:space:]]*overlayroot[[:space:]]*=[[:space:]]*"?[^"[:space:]]' "$root/etc/overlayroot.conf" &&
     ! grep -Eq '^[[:space:]]*overlayroot[[:space:]]*=[[:space:]]*"?disabled' "$root/etc/overlayroot.conf"; then
    printf 'overlayroot is configured in /etc/overlayroot.conf\n'
  fi
  for path in "$root"/etc/systemd/system/bridge-*.service \
              "$root"/home/*/.config/systemd/user/bridge-supervisor.service \
              "$root"/home/*/.config/wireplumber/wireplumber.conf.d/50-bridge-bluez.conf; do
    [ -e "$path" ] && printf 'product LarkBridge install present: %s\n' "${path#"$root"}"
  done
  if [ -f "$root/etc/hostname" ] && [ "$(tr -d '[:space:]' < "$root/etc/hostname")" = larkbridge ]; then
    printf 'hostname is "larkbridge", the product Pi'"'"'s name\n'
  fi
  boot="$(boot_dir "$root")"
  if [ -f "$boot/cmdline.txt" ] && cmdline_has_overlay "$boot/cmdline.txt"; then
    printf 'cmdline.txt enables an overlay root\n'
  fi
  return 0
}

# Evidence from the RUNNING kernel and mounts (independent of any test root).
live_overlay_reasons() {
  local fstype
  fstype="$(findmnt -n -o FSTYPE / 2>/dev/null || true)"
  [ "$fstype" = overlay ] && printf 'the running root filesystem is an overlay\n'
  if mountpoint -q /media/root-ro 2>/dev/null; then
    printf '/media/root-ro is mounted (overlayroot)\n'
  fi
  if cmdline_has_overlay /proc/cmdline; then
    printf 'the running kernel was booted with an overlay root\n'
  fi
  return 0
}

# Escape a string for a JSON string literal (no surrounding quotes).
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"; s="${s//$'\r'/\\r}"; s="${s//$'\t'/\\t}"
  printf '%s' "$s"
}
