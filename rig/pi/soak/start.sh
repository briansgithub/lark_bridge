#!/usr/bin/env bash
# rig soak start <label> [--minutes N] -- launch a detached soak on the Pi.
#
# Detached because the controlling agent's shell times out at 10 minutes; a 30- or
# 60-minute test cannot be held open over ssh.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
require_pi

LABEL="${1:?usage: rig soak start <label> [--minutes N]}"; shift || true
MINUTES=30
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes) MINUTES="$2"; shift 2 ;;
    *) die "unknown arg: $1" ;;
  esac
done

ID="${LABEL}-$(timestamp)"
MAC="$(inv pixel_bt_mac '')"
REPO_REMOTE="$(inv pi_repo_path '~/rpi-lark-bridge')"

# ABSOLUTE path on purpose. systemd-run does NOT inherit the invoking shell's working
# directory -- it starts the unit from the user's home. A relative path fails with
# status 127, and --collect then deletes the unit before the error can be read. Both
# of those cost a debugging round here.
pi "systemd-run --user --unit=soak-$ID --collect \
      bash $REPO_REMOTE/rig/pi/soak/soak-monitor.sh \
      --minutes $MINUTES --label '$LABEL' --mac '$MAC' --out /var/tmp/soak-$ID" >/dev/null

sleep 3
if [ "$(pi "systemctl --user is-active soak-$ID 2>/dev/null || echo dead")" != "active" ]; then
  err "soak unit did not stay running"
  pi "systemctl --user status soak-$ID --no-pager 2>&1 | tail -8" >&2 || true
  die "see above; re-run without --collect in start.sh to keep the failed unit for inspection"
fi

ok "soak started: $ID (${MINUTES} min, detached)"
info "poll with : rig soak status $ID"
info "collect   : rig soak collect $ID"
printf '%s\n' "$ID"
