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

STATUS = Path(f"/run/user/{getattr(os, 'getuid', lambda: 1000)()}/bridge-status.json")
POLL_SECONDS = 2.0
ORPHAN_PATTERNS = ("echo-cancel", "null-sink")
ORPHAN_PROCS = ("pw-loopback", "pw-cli")
AEC_CAPTURE = "echo-cancel-capture"
AEC_SOURCE = "bridge.aec.source"
MICROPHONE_INPUT = "input.bridge.mic"
MICROPHONE_OUTPUT = "output.bridge.mic"


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


def bluetooth_state() -> dict:
    """Controller, ACL link, and SCO voice link -- three separate things.

    Recorded per sample because a fault that takes Bluetooth down with it looks identical,
    from the supervisor's side, to a call simply ending. E13 spent a round trip guessing
    between those after the fact.

    `hcitool con` separates the layers that matter:
      ACL present, no eSCO  -> phone is connected but there is no call audio
      no ACL                -> the Bluetooth link itself dropped
      both                  -> voice link is up

    Note pactl does NOT list HFP nodes, so counting bluez entries there always returns 0
    even mid-call. pw-link sees them.
    """
    controller = "unknown"
    for line in run(["hciconfig", "hci0"]).splitlines():
        if "UP RUNNING" in line:
            controller = "up"
            break
        if "DOWN" in line:
            controller = "down"
            break
    connections = run(["hcitool", "con"])
    return {
        "controller": controller,
        "acl": "ACL" in connections,
        "sco": "SCO" in connections,  # matches both SCO and eSCO
        "bluez_ports": sum(1 for line in run(["pw-link", "-l"]).splitlines() if "bluez" in line),
    }


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


def microphone_inventory(status: dict) -> tuple[dict | None, set[str]]:
    """Return the authoritative selection and every observed candidate node."""

    endpoints = status.get("endpoints") or {}
    microphone = status.get("microphone")
    nodes: set[str] = set()
    if isinstance(microphone, dict):
        selected_value = microphone.get("selected")
        selected = selected_value if isinstance(selected_value, dict) else None
        candidates = microphone.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                node = candidate.get("node")
                if isinstance(node, str) and node:
                    nodes.add(node)
                matched = candidate.get("matched_nodes")
                if isinstance(matched, list):
                    nodes.update(
                        node for node in matched if isinstance(node, str) and node
                    )
        if selected is not None:
            node = selected.get("node")
            if isinstance(node, str) and node:
                nodes.add(node)
    else:
        legacy = endpoints.get("microphone") or endpoints.get("lark")
        selected = (
            {"id": "lark-a1", "node": legacy, "legacy": True}
            if isinstance(legacy, str) and legacy
            else None
        )

    # These aliases make the checker conservative when reading a transitional status.
    for key in ("microphone", "lark"):
        node = endpoints.get(key)
        if isinstance(node, str) and node:
            nodes.add(node)
    return selected, nodes


def check(status: dict, status_error: str | None) -> tuple[list[dict], dict]:
    """Return (violations, observations). A violation names the invariant it breaks."""
    violations: list[dict] = []
    links = pw_links()
    endpoints = status.get("endpoints") or {}
    aec = status.get("aec") or {}
    lark = endpoints.get("lark")
    selected_microphone, microphone_nodes = microphone_inventory(status)
    selected_node = (
        selected_microphone.get("node") if selected_microphone is not None else None
    )
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
    raw_uplinks = [
        (source, target)
        for source, target in links
        if source in microphone_nodes
        and target
        and (target == hfp_sink or "bluez_output" in target)
    ]
    if raw_uplinks:
        violations.append(
            {"id": "I1", "detail": f"raw microphone uplink: {raw_uplinks}"}
        )
    unselected_nodes = microphone_nodes - (
        {selected_node} if isinstance(selected_node, str) and selected_node else set()
    )
    unselected_routes = [
        (source, target)
        for source, target in links
        if source in unselected_nodes
        and target in {AEC_CAPTURE, MICROPHONE_INPUT, hfp_sink}
    ]
    if unselected_routes:
        violations.append(
            {
                "id": "I1",
                "detail": (
                    "inactive microphone linked into the managed graph: "
                    f"{unselected_routes}"
                ),
            }
        )

    # I2 -- fail_closed. An unverified AEC must not leave a raw uplink standing.
    if aec.get("enabled") and not aec.get("verified") and raw_uplinks:
        violations.append(
            {"id": "I2", "detail": f"raw uplink while unverified: {raw_uplinks}"}
        )

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
        if not isinstance(selected_node, str) or not selected_node:
            violations.append({"id": "I4", "detail": "ACTIVE without a selected microphone"})
        else:
            microphone_endpoint = endpoints.get("microphone")
            if (
                isinstance(status.get("microphone"), dict)
                and microphone_endpoint != selected_node
            ):
                violations.append(
                    {
                        "id": "I4",
                        "detail": (
                            "selected microphone does not match endpoints.microphone: "
                            f"{selected_node!r} != {microphone_endpoint!r}"
                        ),
                    }
                )
            physical_target = (
                AEC_CAPTURE if aec.get("enabled") else MICROPHONE_INPUT
            )
            physical_routes = {
                (source, target)
                for source, target in links
                if source in microphone_nodes
                and target in {AEC_CAPTURE, MICROPHONE_INPUT}
            }
            expected_route = (selected_node, physical_target)
            if physical_routes != {expected_route}:
                violations.append(
                    {
                        "id": "I4",
                        "detail": (
                            "physical microphone ownership mismatch: "
                            f"expected {[expected_route]}, found {sorted(physical_routes)}"
                        ),
                    }
                )
            if aec.get("enabled") and (AEC_SOURCE, MICROPHONE_INPUT) not in links:
                violations.append(
                    {
                        "id": "I4",
                        "detail": "verified AEC source does not feed bridge.mic",
                    }
                )
        if hfp_sink:
            hfp_inputs = {source for source, target in links if target == hfp_sink}
            if hfp_inputs != {MICROPHONE_OUTPUT}:
                violations.append(
                    {
                        "id": "I4",
                        "detail": (
                            "HFP uplink ownership mismatch: expected only "
                            f"{MICROPHONE_OUTPUT}, found {sorted(hfp_inputs)}"
                        ),
                    }
                )

    # I7 is reported as an OBSERVATION, not a violation.
    #
    # A single sample cannot tell a ratchet from a teardown still in progress: there is a
    # real window where the call is already down but the AEC has not finished unloading,
    # and the quantum has legitimately not returned yet. Firing on that produced a false
    # positive on restart-supervisor -- exactly the kind of noise that makes a checker
    # untrustworthy. Persistence is the caller's judgement, because only the caller has
    # the timeline.
    quantum = graph_quantum()
    configured = configured_quantum()
    quantum_low = bool(state == "CALL_DOWN" and quantum and configured and quantum < configured)

    observations = {
        "state": state,
        "aec_enabled": aec.get("enabled"),
        "aec_verified": aec.get("verified"),
        "aec_owner_pid": aec.get("owner_pid"),
        "attempts": status.get("attempts"),
        "last_failure": status.get("last_failure"),
        "lark_present": bool(lark),
        "microphone_present": bool(selected_node),
        "microphone_id": (
            selected_microphone.get("id") if selected_microphone is not None else None
        ),
        "microphone_node": selected_node,
        "microphone_candidate_nodes": sorted(microphone_nodes),
        "microphone_identity": (
            selected_microphone.get("identity")
            if selected_microphone is not None
            else None
        ),
        "microphone_format": (
            selected_microphone.get("format")
            if selected_microphone is not None
            else None
        ),
        "microphone_instance_token": (
            selected_microphone.get("instance_token")
            if selected_microphone is not None
            else None
        ),
        "graph_generation": status.get("generation"),
        "call_up": bool(endpoints.get("hfp_source")),
        "graph_quantum": quantum,
        "configured_quantum": configured,
        "quantum_below_configured": quantum_low,
        "link_count": len(links),
        "bluetooth": bluetooth_state(),
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
