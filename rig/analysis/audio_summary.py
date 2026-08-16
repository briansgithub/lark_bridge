#!/usr/bin/env python3
"""Render audio_state.py's JSON (on stdin) as a human-readable block.

Separate from audio_state.py so the machine-readable and human-readable forms cannot
drift apart: there is one parser, and this only formats its output.

    rig/adb/audio-state.sh | rig/analysis/audio_summary.py
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    d = json.load(sys.stdin)

    print(f"  audio mode (requested)   : {d.get('audio_mode_requested')}")
    print(f"  audio mode (actual)      : {d.get('audio_mode_actual')}")
    print(f"  mode owner               : {d.get('audio_mode_owner')}")
    print(f"  SCO state                : {d.get('sco_audio_state')}")
    print(f"  SCO mode                 : {d.get('sco_audio_mode')}")

    acd = d.get("active_communication_device")
    if acd:
        label = acd.get("name") or acd.get("addr") or ""
        print(f"  active comm device       : {acd.get('type')}  {label}")
    else:
        print("  active comm device       : none")

    devs = d.get("connected_devices") or []
    print(f"  connected devices        : {len(devs)}")
    for dev in devs:
        print(f"      {dev.get('type','?'):14s} {dev.get('name') or dev.get('addr') or ''}")

    for m in (d.get("recent_mode_changes") or [])[-5:]:
        print(f"      {m['ts']}  {m['mode']:24s} {m['pkg']}")

    if d.get("PARSE_WARNING"):
        print(f"  !! {d['PARSE_WARNING']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
