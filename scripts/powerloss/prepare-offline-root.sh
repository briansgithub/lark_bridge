#!/usr/bin/env bash
# Seal an already-repartitioned, mounted recovery clone as a read-only appliance.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOT_MOUNT=""
EVIDENCE=""
CONFIRM_ROOT=""
APPLY=0

usage() {
    printf 'usage: sudo %s --root-mount /mnt/root --confirm-root /mnt/root --safety-evidence FILE --apply [--source-root PATH]\n' "$0"
}
die() { printf '[offline-root] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[offline-root] %s\n' "$*" >&2; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --root-mount) shift; ROOT_MOUNT="${1:?--root-mount requires a value}" ;;
        --confirm-root) shift; CONFIRM_ROOT="${1:?--confirm-root requires a value}" ;;
        --safety-evidence) shift; EVIDENCE="${1:?--safety-evidence requires a value}" ;;
        --source-root) shift; SOURCE_ROOT="${1:?--source-root requires a value}" ;;
        --apply) APPLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "must run as root on a Linux recovery host"
[ "$(uname -s)" = Linux ] || die "Linux is required"
[ "$APPLY" -eq 1 ] || die "pass --apply only after reviewing the mounted offline clone"
ROOT_MOUNT="$(readlink -f "$ROOT_MOUNT")"
CONFIRM_ROOT="$(readlink -f "$CONFIRM_ROOT")"
[ "$ROOT_MOUNT" = "$CONFIRM_ROOT" ] || die "--confirm-root must exactly match --root-mount"
[ "$ROOT_MOUNT" != / ] || die "the running root filesystem is never a valid target"
BOOT_MOUNT="$ROOT_MOUNT/boot/firmware"
DATA_MOUNT="$ROOT_MOUNT/var/lib/larkbridge-persist"

for command in blkid chroot cp findmnt install ln mount python3 readlink sha256sum systemctl umount; do
    command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done
python3 "$SCRIPT_DIR/safety-evidence.py" verify --evidence "$EVIDENCE" >/dev/null ||
    die "backup/recovery-card evidence failed verification"
[ "$(findmnt -nro TARGET --target "$ROOT_MOUNT")" = "$ROOT_MOUNT" ] || die "root path is not a distinct mount"
[ "$(findmnt -nro TARGET --target "$BOOT_MOUNT")" = "$BOOT_MOUNT" ] || die "boot firmware is not separately mounted"
[ "$(findmnt -nro TARGET --target "$DATA_MOUNT")" = "$DATA_MOUNT" ] || die "LARKDATA is not mounted at its final path"
[ "$(findmnt -nro FSTYPE --target "$ROOT_MOUNT")" = ext4 ] || die "offline root is not ext4"
case "$(findmnt -nro FSTYPE --target "$BOOT_MOUNT")" in vfat|fat) ;; *) die "offline boot is not FAT";; esac
[ "$(findmnt -nro FSTYPE --target "$DATA_MOUNT")" = ext4 ] || die "LARKDATA is not ext4"
for target in "$ROOT_MOUNT" "$BOOT_MOUNT" "$DATA_MOUNT"; do
    findmnt -nro OPTIONS --target "$target" | tr ',' '\n' | grep -qx rw ||
        die "image-build mount is not writable: $target"
done
for target in "$ROOT_MOUNT/dev" "$ROOT_MOUNT/proc" "$ROOT_MOUNT/sys"; do
    [ "$(findmnt -nro TARGET --target "$target")" != "$target" ] ||
        die "unexpected pre-existing chroot mount: $target"
done
data_source="$(findmnt -nro SOURCE --target "$DATA_MOUNT")"
[ "$(blkid -s LABEL -o value "$data_source")" = LARKDATA ] || die "persistent partition label is not LARKDATA"
[ -f "$ROOT_MOUNT/etc/os-release" ] || die "offline root does not resemble Linux"
[ -f "$BOOT_MOUNT/cmdline.txt" ] && [ -f "$BOOT_MOUNT/config.txt" ] || die "Raspberry Pi boot files are missing"
[ -f "$ROOT_MOUNT/home/admin/rpi-lark-bridge/config/bridge.toml" ] || die "active bridge.toml is missing"
[ -x "$ROOT_MOUNT/usr/sbin/update-initramfs" ] || die "install initramfs-tools in the disposable clone first"
[ -f "$ROOT_MOUNT/usr/share/initramfs-tools/scripts/init-bottom/overlayroot" ] ||
    die "install Debian's overlayroot package in the disposable clone first"
chroot "$ROOT_MOUNT" /usr/bin/dpkg --print-architecture >/dev/null ||
    die "the recovery host cannot execute programs from the offline image"

layout_marker="$DATA_MOUNT/.larkbridge-layout"
if [ ! -f "$layout_marker" ]; then
    unexpected="$(find "$DATA_MOUNT" -mindepth 1 -maxdepth 1 ! -name lost+found -print -quit)"
    [ -z "$unexpected" ] || die "LARKDATA is not empty: $unexpected"
    printf 'larkbridge-layout=1\n' > "$layout_marker"
    sync -f "$layout_marker"
elif [ "$(cat "$layout_marker")" != "larkbridge-layout=1" ]; then
    die "unsupported LARKDATA layout marker"
fi

log "initializing checksummed state on LARKDATA"
mkdir -p "$DATA_MOUNT"/{bluetooth,config,journal,recovery}
python3 "$SOURCE_ROOT/pi/powerloss/lark_state.py" --root "$DATA_MOUNT" config-write \
    --source "$ROOT_MOUNT/home/admin/rpi-lark-bridge/config/bridge.toml" >/dev/null
bluez_source="$ROOT_MOUNT/var/lib/bluetooth"
if [ -L "$bluez_source" ]; then
    [ "$(readlink "$bluez_source")" = /var/lib/larkbridge-persist/bluetooth/live ] ||
        die "offline BlueZ symlink has an unexpected target"
    bluez_source="$DATA_MOUNT/bluetooth/live"
fi
if [ ! -d "$bluez_source" ]; then
    mkdir -p "$bluez_source"
fi
python3 "$SOURCE_ROOT/pi/powerloss/lark_state.py" --root "$DATA_MOUNT" pairing-seal \
    --source "$bluez_source" >/dev/null
if [ "$bluez_source" = "$ROOT_MOUNT/var/lib/bluetooth" ]; then
    rm -rf -- "$DATA_MOUNT/bluetooth/live"
    cp -a -- "$bluez_source" "$DATA_MOUNT/bluetooth/live"
fi
if [ -f "$ROOT_MOUNT/var/lib/systemd/random-seed" ] && [ ! -L "$ROOT_MOUNT/var/lib/systemd/random-seed" ]; then
    install -o root -g root -m 0600 "$ROOT_MOUNT/var/lib/systemd/random-seed" "$DATA_MOUNT/random-seed"
else
    dd if=/dev/urandom of="$DATA_MOUNT/random-seed" bs=512 count=1 status=none
    chmod 0600 "$DATA_MOUNT/random-seed"
fi
if [ -d "$ROOT_MOUNT/var/log/journal" ] && [ ! -L "$ROOT_MOUNT/var/log/journal" ]; then
    cp -a -- "$ROOT_MOUNT/var/log/journal/." "$DATA_MOUNT/journal/"
fi

recovery="$ROOT_MOUNT/usr/share/rpi-lark-bridge/recovery"
rm -rf -- "$recovery/bluetooth"
install -d -o root -g root -m 0700 "$recovery/bluetooth"
install -o root -g root -m 0600 "$ROOT_MOUNT/home/admin/rpi-lark-bridge/config/bridge.toml" "$recovery/bridge.toml"
cp -a -- "$bluez_source/." "$recovery/bluetooth/"

log "installing guard, durability policy, and ordering"
install -d -m 0755 "$ROOT_MOUNT/usr/local/lib/rpi-lark-bridge/powerloss"
install -m 0755 "$SOURCE_ROOT/pi/powerloss/lark_state.py" "$ROOT_MOUNT/usr/local/lib/rpi-lark-bridge/powerloss/lark_state.py"
install -m 0755 "$SOURCE_ROOT/pi/powerloss/storage_guard.py" "$ROOT_MOUNT/usr/local/lib/rpi-lark-bridge/powerloss/storage_guard.py"
install -m 0755 "$SOURCE_ROOT/pi/powerloss/cut_activity.py" "$ROOT_MOUNT/usr/local/lib/rpi-lark-bridge/powerloss/cut_activity.py"
install -m 0755 "$SOURCE_ROOT/pi/powerloss/powerloss_verify.py" "$ROOT_MOUNT/usr/local/lib/rpi-lark-bridge/powerloss/powerloss_verify.py"
install -m 0644 "$SOURCE_ROOT/pi/systemd/system/bridge-storage-guard.service" "$ROOT_MOUNT/etc/systemd/system/bridge-storage-guard.service"
install -m 0644 "$SOURCE_ROOT/pi/systemd/system/bridge-pairing-seal.service" "$ROOT_MOUNT/etc/systemd/system/bridge-pairing-seal.service"
install -m 0644 "$SOURCE_ROOT/pi/systemd/system/bridge-pairing-seal.timer" "$ROOT_MOUNT/etc/systemd/system/bridge-pairing-seal.timer"
install -d -m 0755 "$ROOT_MOUNT/etc/systemd/system/bluetooth.service.d" "$ROOT_MOUNT/etc/systemd/system/user@1000.service.d"
install -m 0644 "$SOURCE_ROOT/pi/systemd/system/bluetooth.service.d/10-larkbridge-storage-guard.conf" \
    "$ROOT_MOUNT/etc/systemd/system/bluetooth.service.d/10-larkbridge-storage-guard.conf"
install -m 0644 "$SOURCE_ROOT/pi/systemd/system/user@1000.service.d/20-larkbridge-storage-guard.conf" \
    "$ROOT_MOUNT/etc/systemd/system/user@1000.service.d/20-larkbridge-storage-guard.conf"
install -d -m 0755 "$ROOT_MOUNT/etc/systemd/system/systemd-journal-flush.service.d" \
    "$ROOT_MOUNT/etc/systemd/system/systemd-random-seed.service.d"
install -m 0644 "$SOURCE_ROOT/pi/systemd/system/systemd-journal-flush.service.d/10-larkbridge-persist.conf" \
    "$ROOT_MOUNT/etc/systemd/system/systemd-journal-flush.service.d/10-larkbridge-persist.conf"
install -m 0644 "$SOURCE_ROOT/pi/systemd/system/systemd-random-seed.service.d/10-larkbridge-persist.conf" \
    "$ROOT_MOUNT/etc/systemd/system/systemd-random-seed.service.d/10-larkbridge-persist.conf"
install -d -m 0755 "$ROOT_MOUNT/etc/systemd/journald.conf.d" "$ROOT_MOUNT/etc/tmpfiles.d" "$ROOT_MOUNT/etc/apt/apt.conf.d"
install -m 0644 "$SOURCE_ROOT/pi/systemd/journald.conf.d/50-larkbridge-bounded.conf" \
    "$ROOT_MOUNT/etc/systemd/journald.conf.d/50-larkbridge-bounded.conf"
install -m 0644 "$SOURCE_ROOT/pi/systemd/tmpfiles.d/larkbridge-powerloss.conf" \
    "$ROOT_MOUNT/etc/tmpfiles.d/larkbridge-powerloss.conf"
install -m 0644 "$SOURCE_ROOT/pi/powerloss/99-larkbridge-read-only" \
    "$ROOT_MOUNT/etc/apt/apt.conf.d/99-larkbridge-read-only"
install -m 0644 "$SOURCE_ROOT/pi/powerloss/overlayroot.local.conf" "$ROOT_MOUNT/etc/overlayroot.local.conf"

rm -rf -- "$ROOT_MOUNT/var/lib/bluetooth" "$ROOT_MOUNT/var/log/journal"
rm -f -- "$ROOT_MOUNT/var/lib/systemd/random-seed"
# NOT a symlink. bluetoothd declares StateDirectory=bluetooth, and systemd refuses to
# set up a StateDirectory whose path is a symlink -- it exits 238/STATE_DIRECTORY and
# bluetooth.service never starts. Measured in E14: the first boot of a converted card had
# no Bluetooth at all, which for this product means no calls. Give systemd the real
# directory it expects and bind the LARKDATA copy over it instead.
mkdir -p "$ROOT_MOUNT/var/lib/bluetooth"
chmod 0700 "$ROOT_MOUNT/var/lib/bluetooth"
if ! grep -q "^/var/lib/larkbridge-persist/bluetooth/live" "$ROOT_MOUNT/etc/fstab"; then
    printf '%s %s none %s 0 0
'         /var/lib/larkbridge-persist/bluetooth/live /var/lib/bluetooth         bind,nofail,x-systemd.requires-mounts-for=/var/lib/larkbridge-persist         >> "$ROOT_MOUNT/etc/fstab"
fi
ln -s /var/lib/larkbridge-persist/journal "$ROOT_MOUNT/var/log/journal"
ln -s /var/lib/larkbridge-persist/random-seed "$ROOT_MOUNT/var/lib/systemd/random-seed"

install -d -m 0755 "$ROOT_MOUNT/etc/systemd/system/multi-user.target.wants"
ln -sfn ../bridge-storage-guard.service \
    "$ROOT_MOUNT/etc/systemd/system/multi-user.target.wants/bridge-storage-guard.service"
install -d -m 0755 "$ROOT_MOUNT/etc/systemd/system/timers.target.wants"
ln -sfn ../bridge-pairing-seal.timer \
    "$ROOT_MOUNT/etc/systemd/system/timers.target.wants/bridge-pairing-seal.timer"
for unit in apt-daily.service apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer; do
    ln -sfn /dev/null "$ROOT_MOUNT/etc/systemd/system/$unit"
done

python3 "$SCRIPT_DIR/configure-offline-boot.py" --root "$ROOT_MOUNT" --boot "$BOOT_MOUNT"
log "generating the overlay-capable initramfs inside the disposable clone"
initramfs_before="$(find "$BOOT_MOUNT" -maxdepth 1 -type f \( -name 'initrd*' -o -name 'initramfs*' \) \
    -exec sha256sum {} \; | sort)"
mkdir -p "$ROOT_MOUNT/run"
touch "$ROOT_MOUNT/run/larkbridge-image-build-mode"
cleanup_chroot() {
    umount "$ROOT_MOUNT/sys" 2>/dev/null || true
    umount "$ROOT_MOUNT/proc" 2>/dev/null || true
    umount "$ROOT_MOUNT/dev" 2>/dev/null || true
    rm -f -- "$ROOT_MOUNT/run/larkbridge-image-build-mode"
}
trap cleanup_chroot EXIT
mount --bind /dev "$ROOT_MOUNT/dev"
mount -t proc proc "$ROOT_MOUNT/proc"
mount -t sysfs sysfs "$ROOT_MOUNT/sys"
chroot "$ROOT_MOUNT" /usr/bin/systemd-analyze verify \
    bridge-storage-guard.service bridge-pairing-seal.service bridge-pairing-seal.timer \
    bluetooth.service user@1000.service systemd-journal-flush.service \
    systemd-random-seed.service
chroot "$ROOT_MOUNT" /usr/sbin/update-initramfs -u -k all
cleanup_chroot
trap - EXIT
initramfs_after="$(find "$BOOT_MOUNT" -maxdepth 1 -type f \( -name 'initrd*' -o -name 'initramfs*' \) \
    -exec sha256sum {} \; | sort)"
[ -n "$initramfs_after" ] ||
    die "no initramfs appeared on the boot partition"
[ "$initramfs_before" != "$initramfs_after" ] ||
    die "the boot-partition initramfs did not change"
sync
log "offline root prepared; unmount it and run read-only fsck before first boot"
