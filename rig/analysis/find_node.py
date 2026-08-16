#!/usr/bin/env python3
"""Find a PipeWire node name in `pw-dump` output (stdin). Prints the name, or nothing.

    pw-dump | find_node.py --prefix bluez_output
    pw-dump | find_node.py --prefix alsa_input --contains Wireless
    pw-dump | find_node.py --media-class Audio/Sink --contains iWorld

Exists so shell callers never have to embed Python inside ssh inside bash. That nesting
needs three levels of quote escaping and breaks in ways that look like hardware faults.
Exit 1 if nothing matched, so `[ -n "$X" ]` and `||` both work.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default=None, help="node.name starts with this")
    ap.add_argument("--contains", default=None, help="node.name or description contains this")
    ap.add_argument("--media-class", default=None, help="exact media.class match")
    ap.add_argument("--all", action="store_true", help="print every match, not just the first")
    a = ap.parse_args()

    try:
        objs = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 1

    found = []
    for o in objs:
        props = (o.get("info") or {}).get("props") or {}
        name = str(props.get("node.name", ""))
        if not name:
            continue
        desc = str(props.get("node.description", ""))
        if a.prefix and not name.startswith(a.prefix):
            continue
        if a.media_class and props.get("media.class") != a.media_class:
            continue
        if a.contains and a.contains.lower() not in (name + " " + desc).lower():
            continue
        found.append(name)
        if not a.all:
            break

    for f in found:
        print(f)
    return 0 if found else 1


if __name__ == "__main__":
    raise SystemExit(main())
