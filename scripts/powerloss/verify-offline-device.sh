#!/usr/bin/env bash
# Read-only final filesystem acceptance check for an unmounted sealed clone.
set -euo pipefail

DEVICE=""
OUTPUT=""

usage() { printf 'usage: sudo %s --device /dev/DEVICE --output FILE\n' "$0"; }
die() { printf '[offline-verify] ERROR: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --device) shift; DEVICE="${1:?--device requires a value}" ;;
        --output) shift; OUTPUT="${1:?--output requires a value}" ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || die "must run as root on Linux"
[ "$(uname -s)" = Linux ] || die "Linux is required"
[ -b "$DEVICE" ] || die "not a block device: $DEVICE"
[ -n "$OUTPUT" ] || die "--output is required"
[ ! -e "$OUTPUT" ] || die "output already exists: $OUTPUT"
for command in blkid e2fsck fsck.vfat lsblk; do
    command -v "$command" >/dev/null 2>&1 || die "missing required command: $command"
done
if lsblk -nrpo NAME,MOUNTPOINT "$DEVICE" | awk 'NF >= 2 && $2 != "" { found=1 } END { exit !found }'; then
    die "target or one of its partitions is mounted"
fi
partition_one="$(lsblk -nrpo NAME,PARTN "$DEVICE" | awk '$2 == 1 {print $1}')"
partition_two="$(lsblk -nrpo NAME,PARTN "$DEVICE" | awk '$2 == 2 {print $1}')"
partition_three="$(lsblk -nrpo NAME,PARTN "$DEVICE" | awk '$2 == 3 {print $1}')"
[ -b "$partition_one" ] && [ -b "$partition_two" ] && [ -b "$partition_three" ] ||
    die "expected partitions 1, 2, and 3"
[ "$(blkid -s LABEL -o value "$partition_three")" = LARKDATA ] ||
    die "partition 3 is not labeled LARKDATA"

temporary="${OUTPUT}.tmp.$$"
trap 'rm -f -- "$temporary"' EXIT
boot_rc=0
root_rc=0
data_rc=0
{
    printf 'device=%s\nchecked_utc=%s\n' "$DEVICE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '\n== boot: %s ==\n' "$partition_one"
    fsck.vfat -n -v "$partition_one" || boot_rc=$?
    printf '\n== root: %s ==\n' "$partition_two"
    e2fsck -f -n -v "$partition_two" || root_rc=$?
    printf '\n== LARKDATA: %s ==\n' "$partition_three"
    e2fsck -f -n -v "$partition_three" || data_rc=$?
    printf '\nreturn_codes: boot=%s root=%s data=%s\n' "$boot_rc" "$root_rc" "$data_rc"
} > "$temporary" 2>&1
mv -- "$temporary" "$OUTPUT"
trap - EXIT
[ "$boot_rc" -eq 0 ] && [ "$root_rc" -eq 0 ] && [ "$data_rc" -eq 0 ] || {
    printf 'FAIL: filesystem check reported errors; evidence: %s\n' "$OUTPUT" >&2
    exit 1
}
printf 'PASS: read-only filesystem checks completed; evidence: %s\n' "$OUTPUT"
