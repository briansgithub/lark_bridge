#!/usr/bin/env python3
"""Validate a bridge status snapshot and emit a machine-readable verdict."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("status", type=Path)
    parser.add_argument(
        "--expect", choices=("active", "safe", "baseline"), required=True
    )
    args = parser.parse_args()

    failures: list[str] = []
    status: dict = {}
    if args.status.exists() and args.status.stat().st_size:
        try:
            status = json.loads(args.status.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"status JSON invalid: {exc}")
    elif args.expect != "baseline":
        failures.append("bridge status file missing")

    if status:
        unexpected = status.get("graph", {}).get("unexpected_links", [])
        if unexpected:
            failures.append(f"unexpected links: {unexpected}")
        if args.expect == "active":
            if status.get("state") != "ACTIVE":
                failures.append(f"state is {status.get('state')}, expected ACTIVE")
            aec = status.get("aec", {})
            if not aec.get("enabled") or not aec.get("verified"):
                failures.append("AEC is not enabled and verified")
            if not aec.get("owner_pid"):
                failures.append("AEC has no supervisor-owned module host")
        elif args.expect == "safe" and status.get("state") == "FAILED":
            failures.append(f"supervisor failed: {status.get('last_failure')}")

    result = {
        "verdict": "PASS" if not failures else "FAIL",
        "expectation": args.expect,
        "state": status.get("state"),
        "generation": status.get("generation"),
        "failures": failures,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
