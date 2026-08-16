#!/usr/bin/env python3
"""Normalise `adb shell dumpsys audio` into stable JSON.

Reads dumpsys output on stdin, writes JSON on stdout.

Why this exists: `dumpsys audio` is ~700 lines of loosely-structured text whose format
is not an API and will drift across Android releases. Every rig assertion about Android's
routing goes through this one parser, so when the format changes there is exactly one
place to fix — and `raw_lines_matched` makes a silent parse failure visible instead of
looking like "Android routed nowhere".

The load-bearing field is `active_communication_device`. That is what actually answers
"is Android using our bridge for calls", as opposed to merely having it connected.

Stdlib only: this must run before any virtualenv exists.
"""

from __future__ import annotations

import json
import re
import sys

# "AudioDeviceAttributes: role:output type:bt_a2dp addr:98:47:.. name:Soundcore .. profiles:[..]"
_ATTR = re.compile(
    r"role:(?P<role>\w+)\s+type:(?P<type>\w+)(?:\s+addr:(?P<addr>\S+))?(?:\s+name:(?P<name>.*?))?"
    r"(?:\s+profiles:|\s+descriptors:|$)"
)

# "08-15 22:03:19:234 setMode(MODE_IN_COMMUNICATION) from package=com.discord pid=6953"
_SETMODE = re.compile(
    r"^(?P<ts>\d\d-\d\d \d\d:\d\d:\d\d:\d+)\s+setMode\((?P<mode>\w+)\)\s+from package=(?P<pkg>\S+)"
)


def parse_device(text: str) -> dict | None:
    text = text.strip()
    if not text or text == "null":
        return None
    m = _ATTR.search(text)
    if not m:
        return {"raw": text}
    d = {k: v for k, v in m.groupdict().items() if v}
    if "name" in d:
        d["name"] = d["name"].strip()
    return d


def main() -> int:
    raw = sys.stdin.read()
    lines = raw.splitlines()

    out: dict = {
        "audio_mode_requested": None,
        "audio_mode_actual": None,
        "audio_mode_owner": None,
        "sco_audio_state": None,
        "sco_audio_mode": None,
        "active_communication_device": None,
        "computed_preferred_communication_device": None,
        "applied_preferred_communication_device": None,
        "connected_devices": [],
        "recent_mode_changes": [],
        "bluetooth_events": [],
        "raw_lines_matched": 0,
    }
    matched = 0

    for i, line in enumerate(lines):
        s = line.strip()

        # The "Audio mode:" header carries no value; requested/actual/owner follow it.
        if s.startswith("- Requested mode ="):
            out["audio_mode_requested"] = s.split("=", 1)[1].strip()
            matched += 1

        elif s.startswith("- Actual mode ="):
            out["audio_mode_actual"] = s.split("=", 1)[1].strip()
            matched += 1

        elif s.startswith("- Mode owner:"):
            val = s.split(":", 1)[1].strip()
            if not val and i + 1 < len(lines):
                val = lines[i + 1].strip()
            out["audio_mode_owner"] = None if val in ("", "None") else val
            matched += 1

        # SCO transport state — the direct read on whether an HFP voice channel is live.
        elif s.startswith("mScoAudioState:"):
            out["sco_audio_state"] = s.split(":", 1)[1].strip()
            matched += 1

        elif s.startswith("mScoAudioMode:"):
            out["sco_audio_mode"] = s.split(":", 1)[1].strip()
            matched += 1

        elif s.startswith("Active communication device:"):
            out["active_communication_device"] = parse_device(s.split(":", 1)[1])
            matched += 1

        elif s.startswith("Computed Preferred communication device:"):
            out["computed_preferred_communication_device"] = parse_device(s.split(":", 1)[1])
            matched += 1

        elif s.startswith("Applied Preferred communication device:"):
            out["applied_preferred_communication_device"] = parse_device(s.split(":", 1)[1])
            matched += 1

        # Two device-listing sections exist: the generic "Connected devices:" and the
        # narrower "APM Connected device (A2DP sink only):". Both are frequently EMPTY —
        # an empty list here means "nothing connected", which is a real measurement, not
        # a parse failure. raw_lines_matched is what distinguishes the two cases.
        elif s.startswith("Connected devices:") or "APM Connected device" in s:
            if s.startswith("Connected devices:"):
                label = "connected"
            else:
                label = s.split("APM Connected device", 1)[1].strip(" :()") or "apm"
            for nxt in lines[i + 1 : i + 16]:
                t = nxt.strip()
                if not t or t.endswith(":"):
                    break
                dev = parse_device(t)
                if dev:
                    dev["group"] = label
                    out["connected_devices"].append(dev)
            matched += 1

        else:
            m = _SETMODE.match(s)
            if m:
                out["recent_mode_changes"].append(m.groupdict())
                matched += 1
            elif "BlutoothActiveDeviceChanged" in s or "now available" in s or "profile service" in s:
                out["bluetooth_events"].append(s)
                matched += 1

    out["raw_lines_matched"] = matched
    out["recent_mode_changes"] = out["recent_mode_changes"][-10:]
    out["bluetooth_events"] = out["bluetooth_events"][-10:]

    # A parse that matched nothing means the format moved; say so loudly rather than
    # emitting a tidy all-null object that reads like a real measurement.
    if matched == 0:
        out["PARSE_WARNING"] = (
            "matched zero known fields - dumpsys audio format may have changed; "
            "do not treat the nulls below as measurements"
        )

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
