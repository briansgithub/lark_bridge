#!/usr/bin/env bash
# BRIDGE_BTFW_VERIFY_ONLY_V2
#
# Device Tree configures BCM43438 SCO-over-HCI. This script only verifies the
# live property and controller readback; it never modifies controller state.
set -euo pipefail

HCI="${BRIDGE_HCI:-hci0}"
DT_PROP="/sys/firmware/devicetree/base/soc/serial@7e201000/bluetooth/brcm,bt-pcm-int-params"
WANT_DT_HEX="0102000101"
WANT_PARAMS="01 02 00 01 01"
MAX_ATTEMPTS="${BRIDGE_BT_VERIFY_ATTEMPTS:-30}"
RETRY_DELAY="${BRIDGE_BT_VERIFY_DELAY:-0.10}"

log() { printf '[bridge-btfw] %s\n' "$*"; }
die() { printf '[bridge-btfw] ERROR: %s\n' "$*" >&2; exit 1; }

command -v hcitool >/dev/null 2>&1 || die "hcitool not found"
[ -f "$DT_PROP" ] || die "DT-native SCO property is missing: $DT_PROP"

dt_hex="$(od -An -tx1 -v "$DT_PROP" | tr -d ' \n')"
[ "$dt_hex" = "$WANT_DT_HEX" ] ||
    die "DT SCO property is '$dt_hex', expected '$WANT_DT_HEX'"

read_params() {
    hcitool -i "$HCI" cmd 0x3f 0x1d 2>/dev/null |
        awk '
            /^[[:space:]]*01 1D FC / {
                if ($4 == "00") {
                    print $5, $6, $7, $8, $9
                    exit
                }
            }
        '
}

for ((attempt=1; attempt<=MAX_ATTEMPTS; attempt++)); do
    params="$(read_params || true)"
    if [ -n "$params" ]; then
        log "SCO PCM params: $params"
        [ "$params" = "$WANT_PARAMS" ] ||
            die "controller SCO params are '$params', expected '$WANT_PARAMS'; refusing to modify controller state"
        log "verified: DT-native SCO routing is active; no userspace write performed"
        exit 0
    fi

    case "$attempt" in
        1|10|20|"$MAX_ATTEMPTS")
            log "controller not yet readable for SCO verification (attempt ${attempt}/${MAX_ATTEMPTS})"
            ;;
    esac
    sleep "$RETRY_DELAY"
done

die "controller never became readable for SCO verification after ${MAX_ATTEMPTS} attempts"
