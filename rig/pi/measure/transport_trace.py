#!/usr/bin/env python3
"""Trace the phone's Bluetooth transport as it changes. Runs ON THE PI.

Writes one record per *change*, not per sample, so the log reads as a transition timeline
instead of a wall of identical lines. A 0.5 s poll over a five-minute run produces a handful
of records when nothing happens and a dense burst exactly where the interesting thing is.

WHY IT POLLS pw-dump/pw-link RATHER THAN THE SUPERVISOR STATUS FILE
-------------------------------------------------------------------
The same reason linkprobe.py does: the status file is gated on the supervisor's own view, so
the tick that first *reports* a node is the tick that has already acted on it. An instrument
reading that file structurally cannot see the exposure window between a node appearing and
the supervisor owning it. That window is precisely what E19 needs to measure, because
WirePlumber's default policy autoconnects an arriving a2dp-source stream within milliseconds
while the supervisor polls every 2 s.

WHAT IT WATCHES
---------------
  * every PipeWire node whose name carries the phone's MAC, with its bluez profile and state
  * the bluez card's active profile
  * every pw-link mentioning the phone
  * org.bluez MediaTransport1 State for each transport object, which is the signal
    a2dp-survival.sh uses to catch an AVDTP transport leaving `active`
  * the supervisor's own state, for correlation only -- never as the primary evidence

    python3 transport_trace.py --mac 5C:33:7B:CB:BF:C5 --seconds 300 --out /tmp/trace.log
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

STATUS_PATH = "/run/user/1000/bridge-status.json"


def run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def transport_states(mac_underscored: str) -> dict[str, str]:
    """MediaTransport1.State for every transport object belonging to this device."""
    out: dict[str, str] = {}
    tree = run(["busctl", "--system", "tree", "org.bluez", "--list"], timeout=10)
    for line in tree.splitlines():
        path = line.strip()
        if f"dev_{mac_underscored}" not in path or "/fd" not in path:
            continue
        raw = run(
            [
                "busctl",
                "--system",
                "get-property",
                "org.bluez",
                path,
                "org.bluez.MediaTransport1",
                "State",
            ]
        ).strip()
        if raw:
            out[path] = raw.replace('s "', "").rstrip('"')
    return out


def sample(mac: str) -> dict:
    mac_u = mac.replace(":", "_")
    nodes: dict[str, str] = {}
    card_profile = None
    try:
        for obj in json.loads(run(["pw-dump"], timeout=15) or "[]"):
            info = obj.get("info") or {}
            props = info.get("props") or {}
            kind = obj.get("type", "")
            if kind == "PipeWire:Interface:Node":
                name = str(props.get("node.name", ""))
                if mac_u in name:
                    nodes[name] = "{}/{}".format(props.get("api.bluez5.profile"), info.get("state"))
            elif kind == "PipeWire:Interface:Device":
                if mac_u in str(props.get("device.name", "")):
                    for entry in (info.get("params") or {}).get("Profile") or []:
                        card_profile = entry.get("name")
    except (json.JSONDecodeError, TypeError):
        pass

    links = sorted(
        line.strip() for line in (run(["pw-link", "-l"]) or "").splitlines() if mac_u in line
    )

    state = None
    try:
        state = json.loads(Path(STATUS_PATH).read_text(encoding="utf-8"))["state"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    return {
        "supervisor_state": state,
        "card_profile": card_profile,
        "nodes": nodes,
        "links": links,
        "transports": transport_states(mac_u),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mac", required=True)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--out", default="/tmp/e19-transport-trace.jsonl")
    args = parser.parse_args()

    deadline = time.monotonic() + args.seconds
    previous = None
    with open(args.out, "w", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            current = sample(args.mac)
            if current != previous:
                record = dict(current)
                record["t"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                previous = current
            time.sleep(args.interval)
        handle.write(json.dumps({"t": "END", "note": "trace window closed"}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
