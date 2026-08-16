#!/usr/bin/env python3
"""Read one dotted key out of a JSON document on stdin. A two-line stand-in for jq.

    jsonget.py sco_audio_state              < audio-state.json
    jsonget.py active_communication_device.type < audio-state.json
    jsonget.py --len connected_devices      < audio-state.json

Deliberately stdin-only: Git Bash rewrites POSIX paths to Windows form only when they
are separate ARGUMENTS to a native binary, never when embedded in a `-c` code string.
Taking the document on stdin removes that whole class of bug, because bash performs the
redirection itself. jq is not installed on the control PC and adding it would be a
dependency for something this small.

Missing keys print an empty line and exit 1, so callers can distinguish "absent" from
"present but null" (which prints "None").
"""

from __future__ import annotations

import json
import sys

MISSING = object()


def main() -> int:
    args = [a for a in sys.argv[1:]]
    want_len = False
    if args and args[0] == "--len":
        want_len = True
        args = args[1:]

    if len(args) != 1:
        sys.stderr.write("usage: jsonget.py [--len] <dotted.key>  < file.json\n")
        return 2

    try:
        doc = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"jsonget: input is not valid JSON: {exc}\n")
        return 2

    cur = doc
    for part in args[0].split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, MISSING)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else MISSING
        else:
            cur = MISSING
        if cur is MISSING:
            print("")
            return 1

    if want_len:
        print(len(cur) if hasattr(cur, "__len__") else 0)
    elif isinstance(cur, (dict, list)):
        print(json.dumps(cur))
    else:
        print(cur)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
