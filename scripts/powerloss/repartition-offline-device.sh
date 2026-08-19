#!/usr/bin/env bash
# Shrink partition 2 and add a 1 GiB LARKDATA partition on an offline clone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVICE=""
CONFIRM_DEVICE=""
EVIDENCE=""
APPLY=0
DATA_MIB=1024

usage() {
    printf 'usage: sudo %s --device /dev/DEVICE --confirm-device /dev/DEVICE --safety-evidence FILE --apply\n' "$0"
}
die() { printf '[repartition] ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[repartition] %s\n' "$*" >&2; }
repair_ext4() {
    local target="$1" status=0
    e2fsck -f -y "$target" || status=$?
    [ "$status" -le 1 ] || die "e2fsck failed for $target with status $status"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --device) shift; DEVICE="${1:?--device requires a value}" ;;
        --confirm-device) shift; CONFIRM_DEVICE="${1:?--confirm-device requires a value}" ;;
        --safety-evidence) shift; EVIDENCE="${1:?--safety-evidence requires a value}" ;;
        --data-mib) shift; DATA_MIB="${1:?--data-mib requires a value}" ;;
        --apply) APPLY=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "must run as root on a Linux recovery host"
[ "$(uname -s)" = Linux ] || die "Linux is required"
[ "$APPLY" -eq 1 ] || die "dry-run only: pass --apply after reviewing the selected device"
[ -b "$DEVICE" ] || die "not a block device: $DEVICE"
[ "$DEVICE" = "$CONFIRM_DEVICE" ] || die "--confirm-device must exactly match --device"
case "$DEVICE" in /dev/*) ;; *) die "device must be an explicit /dev path";; esac
case "$DATA_MIB" in *[!0-9]*|'') die "--data-mib must be an integer";; esac
[ "$DATA_MIB" -ge 1024 ] || die "LARKDATA must be at least 1024 MiB"

for command in blockdev dumpe2fs e2fsck findmnt lsblk mkfs.ext4 parted partprobe python3 resize2fs sfdisk udevadm; do
    command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done
python3 "$SCRIPT_DIR/safety-evidence.py" verify --evidence "$EVIDENCE" >/dev/null ||
    die "backup/recovery-card evidence failed verification"

[ "$(lsblk -dnro TYPE "$DEVICE")" = disk ] || die "target must be a whole disk"
root_source="$(findmnt -nro SOURCE /)"
root_parent="$(lsblk -no PKNAME "$root_source" 2>/dev/null | head -1 || true)"
[ -z "$root_parent" ] || [ "/dev/$root_parent" != "$DEVICE" ] || die "target contains the running root filesystem"
if lsblk -nrpo NAME,MOUNTPOINT "$DEVICE" | awk 'NF >= 2 && $2 != "" { found=1 } END { exit !found }'; then
    die "target or one of its partitions is mounted"
fi
[ "$(lsblk -nrpo PARTN "$DEVICE" | sed '/^$/d' | tr '\n' ' ')" = "1 2 " ] ||
    die "expected exactly the audited two-partition source layout"

partition_two="$(lsblk -nrpo NAME,PARTN "$DEVICE" | awk '$2 == 2 {print $1}')"
[ "$(lsblk -dnro FSTYPE "$partition_two")" = ext4 ] || die "partition 2 is not ext4"
[ "$(parted -sm "$DEVICE" print | sed -n '2p' | cut -d: -f6)" = msdos ] ||
    die "only the audited MBR/msdos layout is supported"

sector_size="$(blockdev --getss "$DEVICE")"
disk_sectors="$(blockdev --getsz "$DEVICE")"
partition_start="$(lsblk -bnro START "$partition_two")"
data_sectors=$(( DATA_MIB * 1024 * 1024 / sector_size ))
# Align the LARKDATA start to a one-MiB boundary.
alignment=$(( 1024 * 1024 / sector_size ))
data_start=$(( (disk_sectors - data_sectors) / alignment * alignment ))
root_end=$(( data_start - 1 ))
new_root_bytes=$(( (root_end - partition_start + 1) * sector_size ))
log "checking and recording the original clone layout"
partition_backup="${EVIDENCE}.partition-table-before.sfdisk"
[ ! -e "$partition_backup" ] || die "partition-table evidence already exists: $partition_backup"
sfdisk --dump "$DEVICE" > "$partition_backup"
sync -f "$partition_backup"
repair_ext4 "$partition_two"
minimum_blocks="$(resize2fs -P "$partition_two" 2>&1 | awk -F: '/minimum size/{gsub(/ /,"",$2); print $2}')"
block_size="$(dumpe2fs -h "$partition_two" 2>/dev/null | awk -F: '/Block size/{gsub(/ /,"",$2); print $2}')"
[ -n "$minimum_blocks" ] && [ -n "$block_size" ] || die "could not determine ext4 minimum size"
minimum_with_margin=$(( minimum_blocks * block_size + 512 * 1024 * 1024 ))
[ "$new_root_bytes" -gt "$minimum_with_margin" ] || die "root filesystem cannot safely shrink by ${DATA_MIB} MiB"
new_root_kib=$(( new_root_bytes / 1024 ))

log "verified offline target $DEVICE"
log "shrinking $partition_two to ${new_root_kib} KiB; LARKDATA begins at sector $data_start"
resize2fs "$partition_two" "${new_root_kib}K"
parted -s "$DEVICE" unit s resizepart 2 "${root_end}s"
parted -s "$DEVICE" unit s mkpart primary ext4 "${data_start}s" 100%
partprobe "$DEVICE"
udevadm settle
partition_three="$(lsblk -nrpo NAME,PARTN "$DEVICE" | awk '$2 == 3 {print $1}')"
[ -b "$partition_three" ] || die "new partition 3 did not appear"
mkfs.ext4 -F -L LARKDATA -m 1 "$partition_three"
repair_ext4 "$partition_two"
repair_ext4 "$partition_three"
log "offline repartition complete: $partition_three is LARKDATA"
