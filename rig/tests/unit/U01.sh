#!/usr/bin/env bash
# U01 — SD card / OS image identity
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U01" "OS image identity" \
  "the Pi booted the intended image on the intended hardware" \
  "prove any peripheral works, or that package versions are installed"

require_pi
DIR="$(artifact_dir U01-os)"

pi 'cat /etc/os-release; echo "---"; uname -a; echo "---"; tr -d "\0" </proc/device-tree/model' > "$DIR/identity.txt" 2>&1

CODENAME="$(pi 'awk -F= "/^VERSION_CODENAME=/{print \$2}" /etc/os-release')"
VERSION_ID="$(pi 'awk -F\" "/^VERSION_ID=/{print \$2}" /etc/os-release')"
KERNEL="$(pi 'uname -r')"
ARCH="$(pi 'uname -m')"
MODEL="$(pi 'tr -d "\0" </proc/device-tree/model')"

printf '  os        : Debian %s (%s)\n' "$VERSION_ID" "$CODENAME" >&2
printf '  kernel    : %s\n' "$KERNEL" >&2
printf '  arch      : %s\n' "$ARCH" >&2
printf '  model     : %s\n' "$MODEL" >&2
echo >&2

fail=0

# PLAN.md is written against Trixie: PipeWire 1.4.x / WirePlumber 0.5.x. Bookworm
# ships older versions and several config keys differ, so this is a hard gate.
[ "$CODENAME" = "trixie" ] || { err "expected Debian 13 trixie, got '$CODENAME'"; fail=1; }
[ "$ARCH" = "aarch64" ]    || { err "expected aarch64 (64-bit), got '$ARCH'"; fail=1; }

case "$MODEL" in
  *"Raspberry Pi 3 Model B Rev 1.2"*) ok "exact target hardware: Pi 3B v1.2" ;;
  *"Raspberry Pi 3 Model B"*) warn "Pi 3B but not Rev 1.2 — Bluetooth findings may not transfer" ;;
  *) err "unexpected model: $MODEL"; fail=1 ;;
esac

# The kernel version is recorded rather than asserted: it is an input to E01, because
# it determines how the Bluetooth chip is attached (serdev vs userspace hciattach).
case "$KERNEL" in
  6.1[2-9]*|6.[2-9][0-9]*|[7-9].*) ok "kernel $KERNEL (>= 6.12 baseline)" ;;
  *) warn "kernel $KERNEL is older than the 6.12 baseline PLAN.md assumes" ;;
esac

if [ "$fail" -eq 0 ]; then
  ok "U01 PASS"
  emit_result U01 PASS "$DIR" os "$CODENAME" kernel "$KERNEL" arch "$ARCH" model "$MODEL"
else
  err "U01 FAIL — re-flash with Raspberry Pi OS Lite (64-bit), Trixie"
  emit_result U01 FAIL "$DIR" os "$CODENAME" kernel "$KERNEL" arch "$ARCH" model "$MODEL"
fi
exit "$fail"
