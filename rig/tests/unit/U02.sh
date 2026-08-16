#!/usr/bin/env bash
# U02 — Ethernet link, addressing, DNS and package-archive reachability
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U02" "Ethernet + name resolution + apt" \
  "the Pi has a working wired network path and can install packages" \
  "prove Wi-Fi is disabled, or that mDNS works from every host on the LAN"

require_pi
DIR="$(artifact_dir U02-net)"

pi 'ip -br addr; echo "--- routes ---"; ip route; echo "--- resolv ---"; cat /etc/resolv.conf' \
  > "$DIR/network.txt" 2>&1

IPV4="$(pi "ip -4 -br addr show eth0 | awk '{print \$3}'")"
GW="$(pi "ip route | awk '/^default/{print \$3; exit}'")"

printf '  eth0      : %s\n' "${IPV4:-<none>}" >&2
printf '  gateway   : %s\n' "${GW:-<none>}" >&2

fail=0
[ -n "$IPV4" ] || { err "eth0 has no IPv4 address — is the cable in?"; fail=1; }
[ -n "$GW" ]   || { err "no default route"; fail=1; }

if pi 'ping -c1 -W2 "$(ip route | awk "/^default/{print \$3; exit}")" >/dev/null 2>&1'; then
  ok "gateway reachable"
else
  err "cannot ping the gateway"; fail=1
fi

if pi 'getent hosts deb.debian.org >/dev/null 2>&1'; then ok "DNS resolves"
else err "DNS resolution failed"; fail=1; fi

if pi 'sudo apt-get update -qq >/dev/null 2>&1'; then ok "apt-get update succeeds"
else err "apt-get update failed — no usable path to the archives"; fail=1; fi

# Informational, not a gate. Imager's cloud-init runs `rfkill unblock wifi`, so the
# radio is live out of the box. It shares a die with the Bluetooth controller under
# test and must be off before U20 — flagged here so it is not forgotten.
WIFI="$(pi 'rfkill list wifi 2>/dev/null | grep -c "Soft blocked: no" || true')"
if [ "${WIFI:-0}" -gt 0 ]; then
  warn "onboard Wi-Fi radio is UNBLOCKED — must be disabled before U20 (BT coexistence)"
else
  ok "onboard Wi-Fi is blocked/absent"
fi

if [ "$fail" -eq 0 ]; then ok "U02 PASS"; emit_result U02 PASS "$DIR" ipv4 "$IPV4" gateway "$GW"
else err "U02 FAIL"; emit_result U02 FAIL "$DIR" ipv4 "$IPV4" gateway "$GW"; fi
exit "$fail"
