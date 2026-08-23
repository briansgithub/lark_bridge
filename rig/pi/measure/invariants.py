#!/usr/bin/env python3
"""Answer "is the bridge healthy right now" as structured data.

The point of a fault-injection campaign is not the fault list, it is having something
trustworthy to assert afterwards. This is that something. It runs on the Pi, strict
stdlib, and is cheap enough to sample once a second so a caller can build a timeline
and see not just whether the system recovered but how long it took.

    invariants.py                 # human-readable
    invariants.py --json          # for the harness

Invariants, and why each one is here:

  I1  no acoustic feedback loop   The whole reason the supervisor exists rather than
                                  static config. A persistent loopback aimed at a
                                  transient target gets re-linked to the DEFAULT device
                                  and closes Lark -> speaker -> Lark.
  I2  fail_closed honoured        With AEC enabled but unverified, the far end must never
                                  be given un-cancelled audio as a silent fallback.
  I3  no orphans                  CALL_DOWN must leave no pw-cli, pw-loopback, echo-cancel
                                  module or null sink behind.
  I4  state is coherent           The reported state must match the graph it describes.
                                  Recovery timing is the caller's job; this reports the
                                  ingredients.
  I5  status file sane            Valid JSON and fresh. A stale status file silently
                                  invalidates every other check that reads it.
  I6  resource counts             fds, RSS, modules, processes. Not pass/fail on their own
                                  -- the caller trends them across cycles.
  I7  quantum not ratcheted       Loading the AEC drags the graph quantum down (E11/E12).
                                  With the AEC down it must return to the configured
                                  default and must not ratchet lower across cycles.

DELIBERATELY NOT AN INVARIANT: "audio is flowing". E12 reported a supervisor defect on
that basis and it was wrong -- the far-end microphone was simply unplugged, and a bridge
carrying silence because nobody is talking is not broken. Such a check would fire on a
healthy unit every time the far end went quiet. Whether audio RESUMES after a fault is a
per-fault assertion against a known injected signal, and belongs in the harness that
controls that signal, not here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

STATUS = Path(f"/run/user/{os.getuid()}/bridge-status.json")
POLL_SECONDS = 2.0
ORPHAN_PATTERNS = ("echo-cancel", "null-sink")
ORPHAN_PROCS = ("pw-loopback", "pw-cli")


def run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def read_status() -> tuple[dict, str | None]:
    try:
        raw = STATUS.read_text()
    except OSError as exc:
        return {}, f"unreadable: {exc}"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON: {exc}"


def pw_links() -> list[tuple[str, str]]:
    """(source, target) pairs, node names only. Mirrors the supervisor's own parser."""
    out = run(["pw-link", "-l"])
    links: list[tuple[str, str]] = []
    current: str | None = None
    for raw in out.splitlines():
        if not raw.startswith((" ", "\t")):
            current = raw.strip().split(":")[0]
            continue
        value = raw.strip()
        if current is None or not value:
            continue
        if value.startswith("|->"):
            links.append((current, value[3:].strip().split(":")[0]))
        elif value.startswith("|<-"):
            links.append((value[3:].strip().split(":")[0], current))
    return links


def graph_quantum() -> int:
    """Largest running quantum in the graph. Followers report 0; the driver carries it."""
    text = run(["timeout", "6", "pw-top", "-b", "-n", "2"])
    best = 0
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 10 or not cols[0].startswith(("R", "I")):
            continue
        try:
            best = max(best, int(cols[2]))
        except ValueError:
            continue
    return best


def configured_quantum() -> int:
    for line in run(["pw-metadata", "-n", "settings"]).splitlines():
        if "clock.quantum" in line and "force" not in line:
            match = re.search(r"value:'(\d+)'", line)
            if match:
                return int(match.group(1))
    return 0


def resource_counts(status: dict) -> dict:
    modules = run(["pactl", "list", "short", "modules"]).splitlines()
    procs = run(["pgrep", "-c", "-f", "pw-loopback"]).strip()
    pid = (status.get("aec") or {}).get("owner_pid")
    fds = rss = None
    if pid:
        try:
            fds = len(os.listdir(f"/proc/{pid}/fd"))
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
        except OSError:
            pass
    return {
        "modules_total": len(modules),
        "loopback_procs": int(procs) if procs.isdigit() else 0,
        "aec_owner_fds": fds,
        "aec_owner_rss_kib": rss,
    }


def check(status: dict, status_error: str | None) -> tuple[list[dict], dict]:
    """Return (violations, observations). A violation names the invariant it breaks."""
    violations: list[dict] = []
    links = pw_links()
    endpoints = status.get("endpoints") or {}
    aec = status.get("aec") or {}
    lark = endpoints.get("lark")
    hfp_sink = endpoints.get("hfp_sink")
    state = status.get("state")

    # I5 first: everything else reads the status file, so its validity gates the rest.
    if status_error:
        violations.append({"id": "I5", "detail": f"status file {status_error}"})
    else:
        age = time.time() - float(status.get("timestamp", 0))
        if age > POLL_SECONDS * 3:
            violations.append({"id": "I5", "detail": f"status file stale by {age:.1f}s"})

    # I1 -- the feedback loop this whole component exists to prevent.
    if lark and hfp_sink and (lark, hfp_sink) in links:
        violations.append({"id": "I1", "detail": f"FEEDBACK LOOP: {lark} -> {hfp_sink}"})

    # I2 -- fail_closed. An unverified AEC must not leave a raw uplink standing.
    if aec.get("enabled") and not aec.get("verified"):
        raw_uplinks = [(s, t) for s, t in links if s == lark and t and "bluez_output" in t]
        if raw_uplinks:
            violations.append({"id": "I2", "detail": f"raw uplink while unverified: {raw_uplinks}"})

    # I3 -- orphans. Only meaningful once the call is down and teardown should be complete.
    orphans: list[str] = []
    if state == "CALL_DOWN":
        for line in run(["pactl", "list", "short", "modules"]).splitlines():
            if any(p in line for p in ORPHAN_PATTERNS):
                orphans.append(line.split("\t")[1] if "\t" in line else line)
        for proc in ORPHAN_PROCS:
            if run(["pgrep", "-f", proc]).strip():
                orphans.append(f"process:{proc}")
        if orphans:
            violations.append({"id": "I3", "detail": f"orphans while CALL_DOWN: {orphans}"})

    # I4 -- the reported state must match the graph it claims.
    graph = status.get("graph") or {}
    if state == "ACTIVE":
        if graph.get("missing_links"):
            violations.append({"id": "I4", "detail": f"ACTIVE with missing links: {graph['missing_links']}"})
        if graph.get("unexpected_links"):
            violations.append({"id": "I4", "detail": f"ACTIVE with unexpected links: {graph['unexpected_links']}"})
        if aec.get("enabled") and not aec.get("verified"):
            violations.append({"id": "I4", "detail": "ACTIVE but AEC never verified"})

    # I7 -- with the AEC down the graph must be back at its configured quantum.
    quantum = graph_quantum()
    configured = configured_quantum()
    if state == "CALL_DOWN" and quantum and configured and quantum < configured:
        violations.append(
            {"id": "I7", "detail": f"quantum {quantum} below configured {configured} with no call"}
        )

    observations = {
        "state": state,
        "aec_enabled": aec.get("enabled"),
        "aec_verified": aec.get("verified"),
        "aec_owner_pid": aec.get("owner_pid"),
        "attempts": status.get("attempts"),
        "last_failure": status.get("last_failure"),
        "lark_present": bool(lark),
        "call_up": bool(endpoints.get("hfp_source")),
        "graph_quantum": quantum,
        "configured_quantum": configured,
        "link_count": len(links),
        "resources": resource_counts(status),
    }
    return violations, observations


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    status, status_error = read_status()
    violations, observations = check(status, status_error)
    report = {
        "t": time.time(),
        "healthy": not violations,
        "violations": violations,
        "observations": observations,
    }

    if args.json:
        print(json.dumps(report, separators=(",", ":")))
        return 0

    print(f"healthy: {report['healthy']}")
    for key, value in observations.items():
        print(f"  {key}: {value}")
    for violation in violations:
        print(f"  VIOLATION {violation['id']}: {violation['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
