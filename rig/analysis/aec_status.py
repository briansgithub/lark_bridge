#!/usr/bin/env python3
"""Validate a bridge status snapshot and emit a machine-readable verdict."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_EXPECTED_MICROPHONE = "lark-a1"


def selected_microphone(status: dict) -> tuple[dict | None, str | None]:
    endpoints = status.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        return None, "bridge endpoints are malformed"
    if "microphone" in status:
        microphone = status.get("microphone")
        if not isinstance(microphone, dict):
            return None, "microphone status is malformed"
        selected = microphone.get("selected")
        if not isinstance(selected, dict):
            return None, str(
                microphone.get("selection_reason")
                or "no microphone candidate is selected"
            )
        candidate_id = selected.get("id")
        node = selected.get("node")
        if not isinstance(candidate_id, str) or not candidate_id:
            return None, "selected microphone has no candidate id"
        if not isinstance(node, str) or not node:
            return None, "selected microphone has no node"
        if endpoints.get("microphone") != node:
            return None, "selected microphone does not match endpoints.microphone"
        return selected, None
    legacy_node = endpoints.get("lark")
    if isinstance(legacy_node, str) and legacy_node:
        return {
            "id": DEFAULT_EXPECTED_MICROPHONE,
            "node": legacy_node,
            "legacy": True,
        }, None
    return None, "no microphone endpoint is present"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("status", type=Path)
    parser.add_argument(
        "--expect", choices=("active", "safe", "baseline"), required=True
    )
    parser.add_argument(
        "--expected-microphone", default=DEFAULT_EXPECTED_MICROPHONE, metavar="ID"
    )
    args = parser.parse_args()

    failures: list[str] = []
    status: dict = {}
    status_loaded = False
    if args.status.exists() and args.status.stat().st_size:
        try:
            decoded = json.loads(args.status.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            failures.append(f"status JSON invalid: {exc}")
        else:
            if isinstance(decoded, dict):
                status = decoded
                status_loaded = True
            else:
                failures.append("bridge status must be a JSON object")
    else:
        failures.append("bridge status file missing")

    selected: dict | None = None
    microphone_error: str | None = None
    if status_loaded:
        selected, microphone_error = selected_microphone(status)
        if selected is not None and selected.get("id") != args.expected_microphone:
            failures.append(
                f"selected microphone is {selected.get('id')!r}, "
                f"expected {args.expected_microphone!r}"
            )
        unexpected = status.get("graph", {}).get("unexpected_links", [])
        if unexpected:
            failures.append(f"unexpected links: {unexpected}")
        if args.expect == "active":
            if microphone_error:
                failures.append(f"selected microphone unavailable: {microphone_error}")
            if status.get("state") != "ACTIVE":
                failures.append(f"state is {status.get('state')}, expected ACTIVE")
            aec = status.get("aec", {})
            if not aec.get("enabled") or not aec.get("verified"):
                failures.append("AEC is not enabled and verified")
            if not aec.get("owner_pid"):
                failures.append("AEC has no supervisor-owned module host")
        elif args.expect == "safe":
            if microphone_error:
                microphone = status.get("microphone")
                endpoints = status.get("endpoints")
                authoritative_absence = (
                    "microphone" in status
                    and isinstance(microphone, dict)
                    and microphone.get("selected") is None
                )
                legacy_absence = (
                    "microphone" not in status
                    and (endpoints is None or isinstance(endpoints, dict))
                    and not (endpoints or {}).get("lark")
                )
                if not (authoritative_absence or legacy_absence):
                    failures.append(
                        f"selected microphone status invalid: {microphone_error}"
                    )
            if status.get("state") == "FAILED":
                failures.append(f"supervisor failed: {status.get('last_failure')}")
        elif args.expect == "baseline" and microphone_error:
            failures.append(f"selected microphone unavailable: {microphone_error}")

    result = {
        "verdict": "PASS" if not failures else "FAIL",
        "expectation": args.expect,
        "state": status.get("state"),
        "generation": status.get("generation"),
        "expected_microphone": args.expected_microphone,
        "microphone_id": selected.get("id") if selected else None,
        "microphone_node": selected.get("node") if selected else None,
        "microphone": selected,
        "failures": failures,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
