#!/usr/bin/env bash
# provision.sh - turn a fresh Raspberry Pi OS Lite (64-bit) card into the HIL RIG Pi.
#
# FOR THE SEPARATE RIG SD CARD ONLY. It refuses to run when it sees overlayroot or the
# product LarkBridge install (see product_card_reasons in lib.sh), and has no override.
#
#   sudo ./provision.sh --bt-addr auto --ssh-key ~/rig.pub     # first run
#   sudo ./provision.sh                                        # re-run: idempotent
#   ./provision.sh --dry-run                                   # show what would change
#
# Options:
#   --dry-run          report every change, make none (no root needed)
#   --bt-addr ADDR     onboard controller address to pin, or "auto" to detect the one
#                      that is not on USB. Kept from rig.conf when omitted.
#   --ssh-key FILE     public key(s) to add to the rig user's authorized_keys
#   --user NAME        rig user (default: $SUDO_USER, else admin)
#   --hostname NAME    default larkbridge-rig (never "larkbridge", the product name)
#   --files-only       write files only: no apt, systemctl, usermod or hardware probes.
#                      Used by the unit tests together with RIG_SYSROOT=<temp root>.
#
# RIG_SYSROOT and --files-only must be used together with a separate test root.
# The overlay guard checks the running system as well as the test root.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=lib.sh
. "$HERE/lib.sh"

R="${RIG_SYSROOT:-}"
DRY_RUN=0
FILES_ONLY=0
BT_ADDR_ARG=""
SSH_KEY=""
RIG_USER_ARG=""
HOSTNAME_WANT="larkbridge-rig"

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --files-only) FILES_ONLY=1 ;;
    --bt-addr) shift; BT_ADDR_ARG="${1:?--bt-addr needs an address or auto}" ;;
    --ssh-key) shift; SSH_KEY="${1:?--ssh-key needs a file}" ;;
    --user) shift; RIG_USER_ARG="${1:?--user needs a name}" ;;
    --hostname) shift; HOSTNAME_WANT="${1:?--hostname needs a name}" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
  shift
done

LIVE=1
if [ "$FILES_ONLY" = 1 ]; then
  [ -n "$R" ] || die "--files-only requires a separate RIG_SYSROOT"
  R="$(realpath -e -- "$R")" || die "RIG_SYSROOT must exist"
  [ -d "$R" ] && [ "$R" != / ] || die "RIG_SYSROOT must be a separate directory"
  LIVE=0
elif [ -n "$R" ]; then
  die "RIG_SYSROOT requires --files-only"
fi
CHANGES=0
REBOOT_NEEDED=0

# ---------------------------------------------------------------- 1. refuse the product card

reasons="$(product_card_reasons "$R"; live_overlay_reasons)"
if [ -r "$R/etc/hostname" ] && [ "$(cat "$R/etc/hostname")" = larkbridge ]; then
  reasons+="${reasons:+$'\n'}product hostname present in /etc/hostname"
fi
if [ -n "$reasons" ]; then
  err "this looks like the PRODUCT LarkBridge card or an overlayroot system. Refusing."
  while IFS= read -r line; do err "  - $line"; done <<<"$reasons"
  err "provision.sh is only for a fresh Raspberry Pi OS Lite card used as the rig Pi."
  exit 4
fi
ok "not the product card (no overlayroot, no LarkBridge install)"

case "$HOSTNAME_WANT" in
  larkbridge) die "--hostname larkbridge is the product Pi's name; pick another" ;;
  *[!a-z0-9-]*|-*|'') die "invalid hostname: $HOSTNAME_WANT" ;;
esac

if [ "$DRY_RUN" = 0 ] && [ -z "$R" ] && [ "$(id -u)" != 0 ]; then
  die "run as root (sudo), or use --dry-run"
fi

model="$(tr -d '\0' 2>/dev/null < /proc/device-tree/model || true)"
if [ -z "$R" ] && [[ "$model" != "Raspberry Pi"* ]]; then
  [ "$DRY_RUN" = 1 ] || die "not a Raspberry Pi (model: ${model:-unknown})"
  warn "not a Raspberry Pi (model: ${model:-unknown}); dry run continues for review"
fi
[ -n "$model" ] && info "board: $model"

# ---------------------------------------------------------------- helpers

run() {
  if [ "$DRY_RUN" = 1 ]; then log "would run: $*"; return 0; fi
  log "run: $*"
  "$@"
}

# put_file SRC DEST MODE  - install SRC at $R/DEST when its content or mode differs.
put_file() {
  local src="$1" dest="$R$2" mode="$3"
  if [ -f "$dest" ] && cmp -s "$src" "$dest" && [ "$(stat -c %a "$dest")" = "${mode#0}" ]; then
    return 0
  fi
  CHANGES=$((CHANGES + 1))
  if [ "$DRY_RUN" = 1 ]; then log "would install $2 (mode $mode)"; return 0; fi
  install -D -m "$mode" "$src" "$dest"
  log "installed $2"
}

# put_content DEST MODE  - like put_file, content on stdin.
put_content() {
  local tmp rc=0
  tmp="$(mktemp)"
  cat > "$tmp"
  put_file "$tmp" "$1" "$2" || rc=$?
  rm -f "$tmp"
  return "$rc"
}

# put_link TARGET LINK  - symlink $R/LINK -> TARGET (TARGET is the path on the rig Pi).
put_link() {
  local target="$1" link="$R$2"
  [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ] && return 0
  CHANGES=$((CHANGES + 1))
  if [ "$DRY_RUN" = 1 ]; then log "would link $2 -> $target"; return 0; fi
  mkdir -p "$(dirname "$link")"
  ln -sfn "$target" "$link"
  log "linked $2 -> $target"
}

# edit FILE CMD...  - run conf_edit.py on $R/FILE; returns 0, sets EDITED=1 on change.
EDITED=0
edit() {
  local rel="$1" file="$R$1" cmd="$2" out rc=0; shift 2
  EDITED=0
  if [ "$DRY_RUN" = 1 ]; then
    out="$(python3 "$HERE/conf_edit.py" --check "$cmd" "$file" "$@")" || rc=$?
  else
    out="$(python3 "$HERE/conf_edit.py" "$cmd" "$file" "$@")" || rc=$?
  fi
  [ "$rc" = 0 ] || die "could not edit $rel (conf_edit.py $cmd exited $rc)"
  if [ "$out" = changed ]; then
    EDITED=1; CHANGES=$((CHANGES + 1))
    if [ "$DRY_RUN" = 1 ]; then log "would edit $rel"; else log "edited $rel"; fi
  fi
}

conf_get() {   # conf_get FILE KEY
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1 | sed 's/^"//; s/"$//'
}

user_exists() {
  if [ -n "$R" ]; then grep -q "^$1:" "$R/etc/passwd" 2>/dev/null; else getent passwd "$1" >/dev/null; fi
}

user_home() {
  if [ -n "$R" ]; then printf '/home/%s\n' "$1"; else getent passwd "$1" | cut -d: -f6; fi
}

# ---------------------------------------------------------------- 2. rig user

RIG_USER="${RIG_USER_ARG:-${SUDO_USER:-admin}}"
[ "$RIG_USER" = root ] && RIG_USER="admin"
user_exists "$RIG_USER" || die "rig user '$RIG_USER' does not exist (create it in Raspberry Pi Imager, or pass --user)"
RIG_HOME="$(user_home "$RIG_USER")"
info "rig user: $RIG_USER ($RIG_HOME)"

# ---------------------------------------------------------------- 3. packages

APT_PACKAGES=(
  bluez rfkill
  pipewire pipewire-bin wireplumber libspa-0.2-bluetooth dbus-user-session
  alsa-utils sox
  openocd gpiod
  python3 python3-serial python3-dbus python3-gi
  util-linux
)
if [ "$LIVE" = 1 ]; then
  missing=()
  for pkg in "${APT_PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed' || missing+=("$pkg")
  done
  # vcgencmd and pinctrl. Raspberry Pi OS ships them; install only if the repo has them.
  if ! command -v pinctrl >/dev/null 2>&1 && apt-cache show raspi-utils >/dev/null 2>&1; then
    missing+=(raspi-utils)
  fi
  if [ "${#missing[@]}" -gt 0 ]; then
    CHANGES=$((CHANGES + 1))
    run env DEBIAN_FRONTEND=noninteractive apt-get update -q
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "${missing[@]}"
  else
    ok "packages present"
  fi
  wp_version="$(wireplumber --version 2>/dev/null | sed -n 's/.*libwireplumber \([0-9.]*\).*/\1/p' | head -n 1)"
  case "$wp_version" in
    0.[0-4].*) warn "WirePlumber $wp_version is older than 0.5: the fake-car config will be ignored. Use the Trixie-based Raspberry Pi OS." ;;
  esac
fi

# ---------------------------------------------------------------- 4. boot: UART for the Pico

BOOT="$(boot_dir "$R")"
BOOT_REL="${BOOT#"$R"}"
[ -f "$BOOT/config.txt" ] || die "missing $BOOT_REL/config.txt"
[ -f "$BOOT/cmdline.txt" ] || die "missing $BOOT_REL/cmdline.txt"
edit "$BOOT_REL/config.txt" config-txt
[ "$EDITED" = 1 ] && REBOOT_NEEDED=1
edit "$BOOT_REL/cmdline.txt" cmdline
[ "$EDITED" = 1 ] && REBOOT_NEEDED=1
# No login prompt on the Pico's UART, whatever a generator decides.
put_link /dev/null /etc/systemd/system/serial-getty@ttyS0.service
put_link /dev/null /etc/systemd/system/serial-getty@serial0.service

# ---------------------------------------------------------------- 5. ssh: keys only

AUTH_KEYS="$RIG_HOME/.ssh/authorized_keys"
if [ -n "$SSH_KEY" ]; then
  [ -f "$SSH_KEY" ] || die "--ssh-key $SSH_KEY: no such file"
  grep -Eq '^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)' "$SSH_KEY" || die "--ssh-key $SSH_KEY does not look like a public key"
  merged="$(mktemp)"
  { [ -f "$R$AUTH_KEYS" ] && cat "$R$AUTH_KEYS"; true; } > "$merged"
  while IFS= read -r keyline; do
    [ -n "$keyline" ] || continue
    grep -qxF -- "$keyline" "$merged" || printf '%s\n' "$keyline" >> "$merged"
  done < "$SSH_KEY"
  put_file "$merged" "$AUTH_KEYS" 0600
  rm -f "$merged"
  if [ "$DRY_RUN" = 0 ] && [ -z "$R" ]; then
    chown "$RIG_USER:" "$RIG_HOME/.ssh" "$AUTH_KEYS"
    chmod 700 "$RIG_HOME/.ssh"
  fi
fi
key_count=0
[ -f "$R$AUTH_KEYS" ] && key_count="$(grep -Ec '^(ssh-|ecdsa-|sk-)' "$R$AUTH_KEYS" || true)"
if [ "$key_count" -gt 0 ] || { [ "$DRY_RUN" = 1 ] && [ -n "$SSH_KEY" ]; }; then
  put_file "$HERE/files/10-larkbridge-rig-ssh.conf" /etc/ssh/sshd_config.d/10-larkbridge-rig.conf 0644
elif [ "$DRY_RUN" = 1 ]; then
  warn "no key in $AUTH_KEYS and no --ssh-key: a real run would refuse to disable password login"
else
  die "refusing to disable SSH password login: $AUTH_KEYS has no key. Pass --ssh-key FILE."
fi

# ---------------------------------------------------------------- 6. Bluetooth: onboard only

CONF_DEST=/etc/larkbridge-rig/rig.conf
BT_ADDR="$(conf_get "$R$CONF_DEST" RIG_BT_ADDR)"

detect_onboard() {
  local idx addr dev found=()
  while read -r idx addr; do
    dev="$(readlink -f "/sys/class/bluetooth/$idx/device" 2>/dev/null || true)"
    case "$dev" in */usb*|'') ;; *) found+=("$addr") ;; esac
  done < <(bt_controllers || true)
  [ "${#found[@]}" = 1 ] || return 1
  printf '%s\n' "${found[0]}"
}

case "$BT_ADDR_ARG" in
  '') ;;
  auto)
    [ "$LIVE" = 1 ] || die "--bt-addr auto needs the live system; pass an address with --files-only"
    BT_ADDR="$(detect_onboard)" || die "could not detect exactly one non-USB controller; pass --bt-addr ADDR (see README read-only checklist)"
    info "detected onboard controller $BT_ADDR" ;;
  *)
    BT_ADDR="$(printf '%s' "$BT_ADDR_ARG" | tr '[:lower:]' '[:upper:]')"
    is_bdaddr "$BT_ADDR" || die "--bt-addr $BT_ADDR_ARG is not a Bluetooth address" ;;
esac
[ -n "$BT_ADDR" ] || warn "no onboard controller address yet: larkbridge-rig-bt-pin.service will fail until you re-run with --bt-addr"

put_file "$HERE/files/70-larkbridge-rig-no-usb-bt.rules" /etc/udev/rules.d/70-larkbridge-rig-no-usb-bt.rules 0644
# Class 0x000420: major Audio/Video, minor Car audio (BlueZ adds the service bits).
# BR/EDR only: the 32UE is a classic A2DP source, and LE would only add radio traffic.
[ -f "$R/etc/bluetooth/main.conf" ] || [ "$LIVE" = 0 ] || die "missing /etc/bluetooth/main.conf (is bluez installed?)"
edit /etc/bluetooth/main.conf ini-set General "Class=0x000420" "ControllerMode=bredr" "Name=LarkRig Fake Car"
bt_conf_changed=$EDITED

# ---------------------------------------------------------------- 7. audio: the fake car

put_file "$HERE/files/50-larkbridge-fake-car.wireplumber.conf" /etc/wireplumber/wireplumber.conf.d/50-larkbridge-fake-car.conf 0644
put_file "$HERE/files/50-larkbridge-fake-car.pipewire.conf" /etc/pipewire/pipewire.conf.d/50-larkbridge-fake-car.conf 0644
# Lingering keeps the rig user's PipeWire running with nobody logged in.
if [ "$LIVE" = 1 ]; then
  if [ ! -e "/var/lib/systemd/linger/$RIG_USER" ]; then
    CHANGES=$((CHANGES + 1)); run loginctl enable-linger "$RIG_USER"
  fi
else
  put_content "/var/lib/systemd/linger/$RIG_USER" 0644 < /dev/null
fi

# ---------------------------------------------------------------- 8. rig tools, lease, units

LIB=/usr/local/lib/larkbridge-rig
for f in lib.sh bt-pin.sh fake-car.sh fake_car_agent.py fake_car_status.py selftest.sh pico-ctl.sh rig_lease.py; do
  case "$f" in lib.sh) mode=0644 ;; *) mode=0755 ;; esac
  put_file "$HERE/$f" "$LIB/$f" "$mode"
done
put_link "$LIB/rig_lease.py" /usr/local/bin/rig-lease
put_link "$LIB/fake-car.sh" /usr/local/bin/fake-car
put_link "$LIB/selftest.sh" /usr/local/bin/rig-selftest
put_link "$LIB/pico-ctl.sh" /usr/local/bin/pico-ctl

# /run is root-only; tmpfiles.d recreates the lease file, owned by the rig user, each boot.
printf 'f /run/larkbridge-rig.lease 0664 %s %s -\n' "$RIG_USER" "$RIG_USER" |
  put_content /etc/tmpfiles.d/larkbridge-rig.conf 0644

for unit in larkbridge-rig-bt-pin.service larkbridge-rig-pico-idle.service; do
  put_file "$HERE/files/$unit" "/etc/systemd/system/$unit" 0644
  put_link "/etc/systemd/system/$unit" "/etc/systemd/system/multi-user.target.wants/$unit"
done

{
  printf '# larkbridge-rig configuration, written by provision.sh. Edit, then re-run it.\n'
  printf 'RIG_USER=%s\n' "$RIG_USER"
  printf 'RIG_BT_ADDR=%s\n' "$BT_ADDR"
  printf 'RIG_BT_ALIAS=LarkRig Fake Car\n'
  printf 'RIG_FAKE_CAR_SINK=larkbridge_fake_car\n'
  printf 'RIG_LEASE_PATH=/run/larkbridge-rig.lease\n'
  printf '# Pico fake Lark (hil-rig.md 3.4). The mini UART on GPIO14/15 is /dev/serial0.\n'
  printf 'RIG_UART=/dev/serial0\n'
  printf 'RIG_UART_BAUD=115200\n'
  printf 'PICO_SWCLK_GPIO=25\n'
  printf 'PICO_SWDIO_GPIO=24\n'
  printf 'PICO_RUN_GPIO=26\n'
  printf 'PICO_UART_TX_GPIO=14\n'
} | put_content "$CONF_DEST" 0644

# ---------------------------------------------------------------- 9. hostname

current_host="$(tr -d '[:space:]' < "$R/etc/hostname" 2>/dev/null || true)"
if [ "$current_host" != "$HOSTNAME_WANT" ]; then
  if [ "$LIVE" = 1 ]; then
    CHANGES=$((CHANGES + 1)); run hostnamectl set-hostname "$HOSTNAME_WANT"
  else
    printf '%s\n' "$HOSTNAME_WANT" | put_content /etc/hostname 0644
  fi
  if [ -f "$R/etc/hosts" ]; then
    sed -E "s/^(127\.0\.1\.1[[:space:]]+).*/\1$HOSTNAME_WANT/" "$R/etc/hosts" | put_content /etc/hosts 0644
  fi
fi

# ---------------------------------------------------------------- 10. apply (live only)

if [ "$LIVE" = 1 ]; then
  for group in bluetooth dialout gpio audio; do
    getent group "$group" >/dev/null || continue
    id -nG "$RIG_USER" | tr ' ' '\n' | grep -qx "$group" && continue
    CHANGES=$((CHANGES + 1)); run usermod -aG "$group" "$RIG_USER"
  done
  run systemctl daemon-reload
  run systemd-tmpfiles --create /etc/tmpfiles.d/larkbridge-rig.conf
  run udevadm control --reload
  if [ -f /etc/ssh/sshd_config.d/10-larkbridge-rig.conf ] || [ "$DRY_RUN" = 1 ]; then
    run sshd -t
    run systemctl enable ssh
    run systemctl reload-or-restart ssh
  fi
  [ "${bt_conf_changed:-0}" = 1 ] && run systemctl restart bluetooth
  if [ -n "$BT_ADDR" ]; then
    run systemctl restart larkbridge-rig-bt-pin.service || warn "bt-pin failed: see journalctl -u larkbridge-rig-bt-pin"
  fi
  run systemctl start larkbridge-rig-pico-idle.service || warn "pico-idle failed: see journalctl -u larkbridge-rig-pico-idle"
fi

# ---------------------------------------------------------------- summary

if [ "$DRY_RUN" = 1 ]; then
  info "dry run: $CHANGES change(s) would be made"
else
  ok "provisioned: $CHANGES change(s)"
fi
if [ "$REBOOT_NEEDED" = 1 ]; then
  warn "boot files changed: reboot, then run: rig-selftest"
else
  info "verify with: rig-selftest"
fi
