#!/usr/bin/env python3
"""Reduce BlueZ and PipeWire state to the fake car's status JSON.

``fake-car.sh status`` feeds this two dumps, both read-only:

* ``--objects``: ``busctl --json=short call org.bluez / org.freedesktop.DBus.ObjectManager
  GetManagedObjects``. busctl wraps every variant as ``{"type": ..., "data": ...}``.
* ``--pwdump``: ``pw-dump`` output, a JSON array of PipeWire objects (optional).

It prints one JSON object. ``ok`` is true when the pinned controller is present and
powered and the recording tap exists. A connected 32UE is reported, not required.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"type", "data"}:
        return _unwrap(value["data"])
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    if isinstance(value, dict):
        return {key: _unwrap(item) for key, item in value.items()}
    return value


def managed_objects(raw: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Accept busctl's {"type", "data": [objects]} envelope or a bare mapping."""
    if isinstance(raw, dict) and "data" in raw and "type" in raw:
        raw = raw["data"]
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
    return _unwrap(raw) if isinstance(raw, dict) else {}


def reduce(objects: dict, adapter_path: str, pw_nodes: list[dict] | None,
           sink_name: str) -> dict[str, Any]:
    adapter = objects.get(adapter_path, {}).get("org.bluez.Adapter1")
    controller = None
    if adapter is not None:
        controller = {
            "path": adapter_path,
            "address": adapter.get("Address"),
            "alias": adapter.get("Alias"),
            "class": f"0x{int(adapter.get('Class', 0)):06x}",
            "powered": bool(adapter.get("Powered")),
            "discoverable": bool(adapter.get("Discoverable")),
            "pairable": bool(adapter.get("Pairable")),
        }
    players: dict[str, str] = {}
    for path, interfaces in objects.items():
        if "org.bluez.MediaPlayer1" in interfaces:
            players[path.rsplit("/", 1)[0]] = path
    devices = []
    for path, interfaces in sorted(objects.items()):
        device = interfaces.get("org.bluez.Device1")
        if device is None or not path.startswith(adapter_path + "/"):
            continue
        uuids = [str(u).lower() for u in device.get("UUIDs", [])]
        devices.append({
            "address": device.get("Address"),
            "name": device.get("Alias") or device.get("Name"),
            "paired": bool(device.get("Paired")),
            "bonded": bool(device.get("Bonded", device.get("Paired"))),
            "trusted": bool(device.get("Trusted")),
            "connected": bool(device.get("Connected")),
            "a2dp_source": A2DP_SOURCE_UUID in uuids,
            "avrcp_player": players.get(path),
        })
    audio: dict[str, Any] = {"pipewire": pw_nodes is not None}
    if pw_nodes is not None:
        names = [((node.get("info") or {}).get("props") or {}).get("node.name", "")
                 for node in pw_nodes if node.get("type") == "PipeWire:Interface:Node"]
        audio["sink_present"] = sink_name in names
        audio["a2dp_streams"] = sorted(n for n in names if n.startswith("bluez_input."))
    ok = bool(controller and controller["powered"] and audio.get("sink_present"))
    return {
        "ok": ok,
        "controller": controller,
        "devices": devices,
        "connected": [d["address"] for d in devices if d["connected"]],
        "audio": audio,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--objects", required=True)
    parser.add_argument("--pwdump")
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--sink", default="larkbridge_fake_car")
    args = parser.parse_args(argv)

    def load(path):
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            return json.loads(text) if text.strip() else None
        except (OSError, ValueError):
            return None

    objects = managed_objects(load(args.objects) or {})
    pw = load(args.pwdump) if args.pwdump else None
    out = reduce(objects, args.adapter, pw if isinstance(pw, list) else None, args.sink)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
