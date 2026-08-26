#!/usr/bin/env python3
"""Time how long any configured microphone -> HFP raw link exists.

This exists because the obvious instrument cannot see the thing being measured.

E13 found that when the Lark appears while an HFP sink is present, the session manager
links it straight to the far end -- raw un-cancelled mic audio, plus a closed acoustic
loop through the speaker. Sampling the supervisor's status file showed a 6.4 s window
before the fix and apparently zero after, which is not credible: the supervisor polls
every 2 s, so some window must remain.

The reason is that the status-file sampler is gated on the supervisor's own view. The
tick that first reports `lark_present` is the same tick that now removes the link, so by
the time the instrument can see the Lark it is already safe. The exposure hides behind
the measurement.

This polls `pw-link` directly and is independent of the supervisor, so it sees the window
the other instrument structurally cannot.

    linkprobe.py 35        # poll for 35 seconds, print the window if one appears

Run it in the background and cycle either microphone underneath it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

MICROPHONE_COMPONENTS = {"USB3547:0407", "USB0C76:161E"}


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def microphone_names() -> set[str]:
    try:
        objects = json.loads(run(["pw-dump"]))
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for item in objects:
        if item.get("type") != "PipeWire:Interface:Node":
            continue
        props = (item.get("info") or {}).get("props") or {}
        component = str(props.get("alsa.components", "")).upper()
        if props.get("media.class") == "Audio/Source" and component in MICROPHONE_COMPONENTS:
            name = props.get("node.name")
            if name:
                names.add(str(name))
    return names


def hfp_sink() -> str | None:
    """Discover the sink rather than hardcoding a MAC."""
    for line in run(["pw-link", "-l"]).splitlines():
        stripped = line.strip()
        for token in (stripped, stripped.lstrip("|-><").strip()):
            name = token.split(":")[0]
            if name.startswith("bluez_output."):
                return name
    return None


def dangerous_present(microphones: set[str], sink: str) -> set[str]:
    dangerous: set[str] = set()
    current = None
    for raw in run(["pw-link", "-l"]).splitlines():
        if not raw.startswith((" ", "\t")):
            current = raw.strip().split(":")[0]
            continue
        value = raw.strip()
        if current in microphones and value.startswith("|->") and sink in value:
            dangerous.add(str(current))
    return dangerous


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 35.0
    microphones = microphone_names()
    sink = hfp_sink()
    first = last = None
    hits = polls = 0
    observed: set[str] = set()

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        microphones |= microphone_names()
        if not microphones:
            continue
        if sink is None:
            sink = hfp_sink()
            continue
        polls += 1
        exposed = dangerous_present(microphones, sink)
        if exposed:
            now = time.monotonic()
            if first is None:
                first = now
            last = now
            hits += 1
            observed.update(exposed)

    if first is None:
        print(f"  raw uplink NEVER observed ({polls} polls over {seconds:.0f}s)")
    else:
        print(
            f"  RAW UPLINK: {hits} of {polls} polls, window {last - first:.2f}s, "
            f"sources={sorted(observed)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
