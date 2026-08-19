#!/usr/bin/env python3
"""Generate a bounded, fsync-heavy state/journal workload for one cut test."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from lark_state import append_ledger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument(
        "--root", type=Path, default=Path("/var/lib/larkbridge-persist")
    )
    arguments = parser.parse_args()
    if not 1 <= arguments.duration <= 600 or not 0.05 <= arguments.interval <= 5:
        parser.error("duration or interval is outside the safety bounds")
    deadline = time.monotonic() + arguments.duration
    sequence = 0
    while time.monotonic() < deadline:
        record = {"event": "cut-write-probe", "sequence": sequence}
        append_ledger(arguments.root, record)
        print(json.dumps(record, sort_keys=True), flush=True)
        sequence += 1
        time.sleep(arguments.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
