#!/usr/bin/env bash
# rig soak status [id] -- progress of a running or finished soak.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
require_pi
ID="${1:-}"
if [ -z "$ID" ]; then
  pi 'ls -1t /var/tmp/ 2>/dev/null | grep "^soak-" | head -5' | sed 's/^soak-/  /'
  exit 0
fi
STATE="$(pi "systemctl --user is-active soak-$ID 2>/dev/null || echo finished")"
printf '  unit    : soak-%s (%s)\n' "$ID" "$STATE"
pi "wc -l < /var/tmp/soak-$ID/samples.jsonl 2>/dev/null || echo 0" | sed 's/^/  samples : /'
pi "tail -3 /var/tmp/soak-$ID/samples.jsonl 2>/dev/null" | sed 's/^/  /'
