#!/usr/bin/env bash
# rig soak collect <id> -- pull artifacts and print the verdict.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
require_pi
ID="${1:?usage: rig soak collect <id>}"
DIR="$RIG_ARTIFACTS/soak-$ID"; mkdir -p "$DIR"
PY="$(command -v py 2>/dev/null || command -v python3)"
pi "cat /var/tmp/soak-$ID/meta.json"    > "$DIR/meta.json"    2>/dev/null || true
pi "cat /var/tmp/soak-$ID/samples.jsonl" > "$DIR/samples.jsonl" 2>/dev/null || die "no samples for $ID"
INT="$("$PY" "$RIG_ROOT/analysis/jsonget.py" interval_s < "$DIR/meta.json" 2>/dev/null || echo 5)"
"$PY" "$RIG_ROOT/analysis/soak_reduce.py" --interval "${INT:-5}" < "$DIR/samples.jsonl" \
  | tee "$DIR/verdict.json"
ok "artifacts: $DIR"
