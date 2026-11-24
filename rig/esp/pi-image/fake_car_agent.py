#!/usr/bin/env python3
"""Pairing window for the fake car: a NoInputNoOutput BlueZ agent that accepts everything.

Used by ``fake-car.sh pair-accept``. For ``--seconds`` it makes the pinned adapter
discoverable and pairable, accepts any pairing or service authorization, marks each newly
paired device Trusted (so later reconnects need no agent), then turns discoverable and
pairable off again and prints one JSON object::

    {"ok": true, "adapter": "/org/bluez/hci0", "paired": ["AA:BB:..."], "seconds": 120}

Only the rig Pi runs this, and only during a pairing gate (the plan's rule: no bond
changes outside pairing gates). Needs python3-dbus and python3-gi.
"""

from __future__ import annotations

import argparse
import json
import sys

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

BUS = "org.bluez"
AGENT_PATH = "/org/larkbridge/rig/agent"


class Agent(dbus.service.Object):
    """Accept-all agent. BlueZ calls these; any exception would reject the request."""

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--adapter", required=True, help="D-Bus path, e.g. /org/bluez/hci0")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--until-paired", action="store_true",
                        help="end the window as soon as one device pairs")
    args = parser.parse_args(argv)

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter = dbus.Interface(bus.get_object(BUS, args.adapter), "org.freedesktop.DBus.Properties")
    manager = dbus.Interface(bus.get_object(BUS, "/org/bluez"), "org.bluez.AgentManager1")
    loop = GLib.MainLoop()
    paired: list[str] = []

    def on_paired(path: str) -> None:
        if not path.startswith(args.adapter + "/dev_"):
            return
        props = dbus.Interface(bus.get_object(BUS, path), "org.freedesktop.DBus.Properties")
        address = str(props.Get("org.bluez.Device1", "Address"))
        if address not in paired:
            paired.append(address)
            props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
            if args.until_paired:
                loop.quit()

    def on_props(interface, changed, _invalidated, path=None):
        if interface == "org.bluez.Device1" and changed.get("Paired"):
            on_paired(str(path))

    bus.add_signal_receiver(on_props, dbus_interface="org.freedesktop.DBus.Properties",
                            signal_name="PropertiesChanged", path_keyword="path")

    Agent(bus, AGENT_PATH)
    manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    manager.RequestDefaultAgent(AGENT_PATH)
    try:
        adapter.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
        adapter.Set("org.bluez.Adapter1", "PairableTimeout", dbus.UInt32(args.seconds))
        adapter.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(args.seconds))
        adapter.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
        adapter.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
        GLib.timeout_add_seconds(args.seconds, loop.quit)
        loop.run()
    finally:
        for prop in ("Discoverable", "Pairable"):
            try:
                adapter.Set("org.bluez.Adapter1", prop, dbus.Boolean(False))
            except dbus.DBusException:
                pass
        try:
            manager.UnregisterAgent(AGENT_PATH)
        except dbus.DBusException:
            pass
    print(json.dumps({"ok": True, "adapter": args.adapter, "paired": paired,
                      "seconds": args.seconds}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
