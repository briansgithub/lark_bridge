#!/usr/bin/env bash
# U03 — Non-interactive SSH key authentication from the control PC
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

unit_header "U03" "SSH key auth (non-interactive)" \
  "the agent can drive the Pi unattended, from a fresh process, with no prompt" \
  "prove the Pi is secure — password auth may still be enabled (U03b hardens it)"

DIR="$(artifact_dir U03-ssh)"
HOST="$(pi_host)"
fail=0

# BatchMode is the whole test: it disables every interactive prompt, so if key auth
# is not working this fails rather than hanging waiting for a password. That property
# is what makes unattended soak tests possible at all.
if ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" 'echo ok' >/dev/null 2>&1; then
  ok "key auth works non-interactively (BatchMode)"
else
  err "BatchMode SSH failed — the agent cannot drive this Pi unattended"
  fail=1
fi

# A fresh process each time is how the agent actually invokes ssh; an agent-forwarded
# or cached credential would give a false pass here.
for i in 1 2 3; do
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true >/dev/null 2>&1 \
    || { err "repeat connection $i failed"; fail=1; }
done
[ "$fail" -eq 0 ] && ok "3 consecutive fresh connections succeeded"

KEYTYPE="$(ssh -G "$HOST" 2>/dev/null | awk '/^identityfile/{print $2; exit}')"
printf '  identity  : %s\n' "${KEYTYPE:-<default>}" >&2

PWAUTH="$(pi 'sudo sshd -T 2>/dev/null | awk "/^passwordauthentication/{print \$2}"' 2>/dev/null || echo unknown)"
printf '  passwordauth on Pi : %s\n' "$PWAUTH" >&2
if [ "$PWAUTH" = "yes" ]; then
  warn "password auth still enabled — fine for bring-up, harden before leaving this on the LAN"
  warn "  (the flashed password is weak and was shared in plaintext; keys make it irrelevant)"
fi

pi 'sudo sshd -T 2>/dev/null | grep -E "^(passwordauthentication|pubkeyauthentication|permitrootlogin)"' \
  > "$DIR/sshd-effective.txt" 2>&1 || true

if [ "$fail" -eq 0 ]; then ok "U03 PASS"; emit_result U03 PASS "$DIR" password_auth "$PWAUTH"
else err "U03 FAIL"; emit_result U03 FAIL "$DIR" password_auth "$PWAUTH"; fi
exit "$fail"
