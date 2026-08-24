#!/usr/bin/env python3
"""Resolve Bluetooth adapters and devices by stable identity, never by hciX index.

WHY THIS EXISTS
---------------
Until 2026-08-23 this appliance had exactly one Bluetooth controller, so `hci0` was a safe
literal and `bluetoothctl connect <mac>` was a safe command. Mode 1 adds a second
controller and both of those stop being true:

  * `hciX` numbering follows probe order and is not stable across boots or replugs. The
    onboard controller happens to win the race today because it is a platform serdev and
    the dongle is USB, but nothing guarantees it.

  * **`bluetoothctl` follows its own notion of a "default" adapter, and the default is the
    LAST one registered.** Measured on the unit the moment the dongle was plugged in:

        Controller A0:AD:9F:73:6C:24 larkbridge #2 [default]   <- the dongle, no bonds
        Controller B8:27:EB:43:8D:51 larkbridge-v2             <- onboard, all three bonds

    Every bond -- phone, iWorld, Soundcore -- lives on the onboard controller. So
    `bluetoothctl connect 5C:33:7B:CB:BF:C5` began targeting an adapter that has never
    heard of the phone. That silently broke bt-watchdog's unattended reconnect on the
    PROVEN Mode 1W configuration, with no code change and no config change: plugging in a
    USB dongle was enough.

So nothing here addresses an adapter by index or by "default". Adapters are identified by
BD address; devices are addressed through the D-Bus object path that already encodes which
adapter owns the bond (`/org/bluez/hci0/dev_5C_33_7B_CB_BF_C5`).

WHY D-BUS AND NOT bluetoothctl
------------------------------
`busctl` ships with systemd, is already used by `rig/pi/measure/a2dp-survival.sh`, needs no
python-dbus, and takes an explicit object path -- which is exactly the property
`bluetoothctl` lacks. `GetManagedObjects` is readable unprivileged, so the user-scoped
supervisor and the root-scoped watchdog can share this module.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SYS_BLUETOOTH = Path("/sys/class/bluetooth")
SYS_RFKILL = Path("/sys/class/rfkill")
BLUEZ = "org.bluez"

# Long enough for a page attempt to a device that is present but asleep; short enough that
# a watchdog poll loop is not blocked for a whole cycle by a device that is genuinely gone.
CONNECT_TIMEOUT = 45.0
QUERY_TIMEOUT = 15.0
POWER_SETTLE_SECONDS = 3.0


@dataclass(frozen=True)
class Adapter:
    """One controller. `hci` is a lookup result, never an identity -- do not cache it."""

    hci: str
    address: str
    bus: str  # "UART" (onboard BCM43438) or "USB" (dongle)
    rfkill_index: int | None

    @property
    def path(self) -> str:
        return f"/org/bluez/{self.hci}"

    @property
    def is_usb(self) -> bool:
        return self.bus == "USB"


@dataclass(frozen=True)
class TrustPinResult:
    """Outcome of making exactly one adapter authoritative for a device."""

    ok: bool
    changed: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


def _run(command: list[str], timeout: float = QUERY_TIMEOUT) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout, result.stderr


def _bus_type(hci: str) -> str:
    """UART vs USB, from where the device hangs in sysfs.

    This decides which recovery a wedged controller needs: the onboard part recovers only
    by unbinding its serdev driver (bt-reset.sh rung 6), a dongle by unbinding btusb.
    Applying the wrong one is a no-op at best.
    """
    try:
        target = (SYS_BLUETOOTH / hci).resolve().as_posix()
    except OSError:
        return "unknown"
    if "/usb" in target:
        return "USB"
    if "serial" in target:
        return "UART"
    return "unknown"


def _rfkill_index(hci: str) -> int | None:
    try:
        entries = sorted(SYS_RFKILL.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if (entry / "name").read_text(encoding="utf-8").strip() == hci:
                return int(entry.name.removeprefix("rfkill"))
        except (OSError, ValueError):
            continue
    return None


def adapters(objects: dict[str, dict] | None = None) -> list[Adapter]:
    """Every controller, named by the kernel and addressed by BlueZ.

    The split matters and is not arbitrary:

      * **Which adapters exist** comes from sysfs, because that is kernel truth. An adapter
        BlueZ has not registered -- rfkilled at boot, or bluetoothd not yet up -- must still
        be visible and repairable, not silently missing from a recovery path.
      * **The BD address** comes from D-Bus, because `/sys/class/bluetooth/hciX/address`
        DOES NOT EXIST on this kernel (checked 2026-08-23: the class directory holds only
        `device`, `power`, `reset`, `rfkill*`, `uevent`). An earlier version of this function
        read that file and returned an empty list on real hardware.

    An adapter BlueZ does not know about gets an empty address, which `adapter_by_address`
    can never match -- deliberate, since acting on an adapter we cannot address is worse
    than reporting it and stopping.
    """
    tree = objects if objects is not None else managed_objects()
    found: list[Adapter] = []
    try:
        entries = sorted(SYS_BLUETOOTH.iterdir())
    except OSError:
        return found
    for entry in entries:
        # /sys/class/bluetooth also carries connection objects such as `hci0:12`; only the
        # bare controller names are adapters.
        if ":" in entry.name:
            continue
        interface = (tree.get(f"/org/bluez/{entry.name}") or {}).get("org.bluez.Adapter1") or {}
        address = str((interface.get("Address") or {}).get("data") or "").upper()
        found.append(
            Adapter(
                hci=entry.name,
                address=address,
                bus=_bus_type(entry.name),
                rfkill_index=_rfkill_index(entry.name),
            )
        )
    return sorted(found, key=lambda adapter: (adapter.address == "", adapter.address))


def adapter_by_address(address: str) -> Adapter | None:
    wanted = address.strip().upper()
    for adapter in adapters():
        if adapter.address == wanted:
            return adapter
    return None


def managed_objects() -> dict[str, dict]:
    """BlueZ's object tree, or an empty dict if bluetoothd is not answering.

    Empty is deliberately indistinguishable from "no devices": callers must treat an absent
    device as "cannot act", never as "device is gone, take recovery action".
    """
    code, out, _ = _run(
        [
            "busctl", "--system", "--json=short", "call", BLUEZ, "/",
            "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
        ]
    )
    if code != 0:
        return {}
    try:
        return json.loads(out)["data"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {}


def _device_property(interfaces: dict, name: str):
    return ((interfaces.get("org.bluez.Device1") or {}).get(name) or {}).get("data")


def path_for(adapter: Adapter, device_mac: str) -> str:
    """The path a bond WOULD have on this adapter. Does not assert that it exists."""
    return f"{adapter.path}/dev_" + device_mac.strip().upper().replace(":", "_")


def device_path(device_mac: str, objects: dict[str, dict] | None = None) -> str | None:
    """The D-Bus path of a bond, which encodes the adapter that owns it.

    A device paired on two adapters has two paths. The lowest by sorted order is returned
    so the choice is at least deterministic; callers that care must pass an explicit
    adapter to `path_for`.
    """
    suffix = "/dev_" + device_mac.strip().upper().replace(":", "_")
    tree = objects if objects is not None else managed_objects()
    matches = [
        path
        for path, interfaces in tree.items()
        if path.endswith(suffix) and "org.bluez.Device1" in interfaces
    ]
    if not matches:
        return None
    # A device bonded on BOTH adapters has two paths, and the one that matters is the one
    # it is actually connected on. Measured 2026-08-23: after the iWorld was paired on the
    # dongle it stayed bonded on the onboard radio too, so a plain lowest-path rule named
    # hci0 while the A2DP stream was live on hci1 -- which would have pointed the survival
    # poller and the SCO counters at the wrong controller and quietly measured nothing.
    connected = [p for p in matches if _device_property(tree[p], "Connected")]
    return min(connected) if connected else min(matches)


def adapter_for_device(device_mac: str) -> Adapter | None:
    """Which controller holds this bond."""
    path = device_path(device_mac)
    if path is None:
        return None
    hci = path.split("/")[3]
    for adapter in adapters():
        if adapter.hci == hci:
            return adapter
    return None


def is_connected(device_mac: str, objects: dict[str, dict] | None = None) -> bool:
    tree = objects if objects is not None else managed_objects()
    path = device_path(device_mac, tree)
    return bool(_device_property(tree[path], "Connected")) if path else False


def is_paired(device_mac: str, objects: dict[str, dict] | None = None) -> bool:
    tree = objects if objects is not None else managed_objects()
    path = device_path(device_mac, tree)
    return bool(_device_property(tree[path], "Paired")) if path else False


def _act(
    verb: str, device_mac: str, adapter: Adapter | None, timeout: float
) -> tuple[bool, str]:
    path: str | None
    if adapter is not None:
        path = path_for(adapter, device_mac)
    else:
        path = device_path(device_mac)
        if path is None:
            return False, f"no bond for {device_mac} on any adapter"
    code, _, err = _run(
        ["busctl", "--system", "call", BLUEZ, path, "org.bluez.Device1", verb],
        timeout=timeout,
    )
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


# A2DP Sink. Connecting this profile specifically, rather than everything a device offers,
# is what keeps a speaker from also becoming an HFP endpoint.
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"


def connect(device_mac: str, adapter: Adapter | None = None) -> tuple[bool, str]:
    """Connect a bond on an EXPLICIT adapter. Returns (ok, detail).

    This is the call `bluetoothctl connect` cannot make safely with two controllers.

    Prefer connect_profile() for speakers: this brings up everything the remote offers.
    """
    return _act("Connect", device_mac, adapter, CONNECT_TIMEOUT)


def connect_profile(
    device_mac: str,
    adapter: Adapter | None = None,
    uuid: str = A2DP_SINK_UUID,
    timeout: float | None = None,
) -> tuple[bool, str]:
    """Bring up ONE profile, defaulting to A2DP Sink.

    Why not plain Connect(): measured on the unit, the Monoprice Boombox advertises
    0000111e (Handsfree) alongside 0000110b (A2DP Sink). Device1.Connect() brings up
    everything on offer, so it can establish HFP *to the speaker* while the Pixel is already
    the audio gateway on the other radio. Two HFP relationships is not a state this design
    has any handling for, and by the time it collides the call is already broken.

    It negotiated A2DP-only in the one observed run, but that was WirePlumber's role config
    declining the rest, not us declining to ask. Asking for one profile makes it structural.
    """
    if adapter is not None:
        path = path_for(adapter, device_mac)
    else:
        resolved = device_path(device_mac)
        if resolved is None:
            return False, f"no bond for {device_mac} on any adapter"
        path = resolved
    code, _, err = _run(
        [
            "busctl", "--system", "call", BLUEZ, path,
            "org.bluez.Device1", "ConnectProfile", "s", uuid,
        ],
        timeout=timeout if timeout is not None else CONNECT_TIMEOUT,
    )
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


def disconnect(device_mac: str, adapter: Adapter | None = None) -> tuple[bool, str]:
    return _act("Disconnect", device_mac, adapter, QUERY_TIMEOUT)


def set_alias(device_mac: str, alias: str, adapter: Adapter | None = None) -> tuple[bool, str]:
    """Rename a bonded device, so a human can find it in a list.

    Devices name themselves badly. The Monoprice Boombox reports itself as "MP43247", which
    is a model number and not something an owner would ever type or recognise. BlueZ stores
    Alias per bond, so this is the correct place for a friendly name -- not a parallel
    nickname table in our own config that could disagree with what every other tool shows.

    This is a durable write (BlueZ persists it into the bond's info file on LARKDATA), but it
    is a deliberate one-off user action, not a per-connect one, so it does not threaten the
    idle-write budget.
    """
    if adapter is not None:
        path = path_for(adapter, device_mac)
    else:
        resolved = device_path(device_mac)
        if resolved is None:
            return False, f"no bond for {device_mac} on any adapter"
        path = resolved
    code, _, err = _run(
        [
            "busctl", "--system", "set-property", BLUEZ, path,
            "org.bluez.Device1", "Alias", "s", alias,
        ]
    )
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


def set_trusted(device_mac: str, trusted: bool, adapter: Adapter | None = None) -> tuple[bool, str]:
    """Set Trusted on one bond. Reads first, so an unchanged value costs no write.

    BlueZ persists Trusted into the bond's info file on LARKDATA, and E14 set an idle-write
    bar of ~65 KB/120 s, so this must not be called unconditionally in a poll loop.
    """
    if adapter is not None:
        path = path_for(adapter, device_mac)
    else:
        resolved = device_path(device_mac)
        if resolved is None:
            return False, f"no bond for {device_mac} on any adapter"
        path = resolved
    code, out, _ = _run(
        ["busctl", "--system", "get-property", BLUEZ, path, "org.bluez.Device1", "Trusted"]
    )
    if code == 0 and out.strip().split()[-1:] == ["true" if trusted else "false"]:
        return True, f"{path}: already {trusted}"
    code, _, err = _run(
        [
            "busctl", "--system", "set-property", BLUEZ, path,
            "org.bluez.Device1", "Trusted", "b", "true" if trusted else "false",
        ]
    )
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


def pin_to_adapter(device_mac: str, adapter: Adapter) -> TrustPinResult:
    """Trust a device on ONE adapter and untrust it everywhere else.

    Both halves are necessary and each was learned from a failure on the unit:

      * TRUSTED on the owning adapter, because BlueZ refuses an untrusted device's incoming
        connection -- logged as `a2dp.c:auth_cb() Access denied: org.bluez.Error.Rejected`.
        Having untrusted the Boombox by hand while freeing an adapter, the speaker then
        churned: it kept trying to connect and kept being rejected, which read as a flaky
        speaker and silently voided three switch measurements.
      * UNTRUSTED elsewhere, because a device bonded and trusted on both adapters bounces
        between them -- measured on the iWorld -- and a speaker that bounces onto the radio
        carrying the call is the exact contention E03 spent days on.

    Idempotent, so it is safe to call whenever a speaker is selected.
    """
    tree = managed_objects()
    suffix = "/dev_" + device_mac.strip().upper().replace(":", "_")
    wanted = path_for(adapter, device_mac)
    matches = [
        (path, interfaces)
        for path, interfaces in sorted(tree.items())
        if path.endswith(suffix) and "org.bluez.Device1" in interfaces
    ]
    if wanted not in {path for path, _interfaces in matches}:
        return TrustPinResult(
            ok=False,
            failures=(f"no bond for {device_mac} on {adapter.hci}",),
        )

    changed: list[str] = []
    wanted_interfaces = next(interfaces for path, interfaces in matches if path == wanted)
    if not bool(_device_property(wanted_interfaces, "Trusted")):
        ok, detail = set_trusted(device_mac, True, adapter)
        if not ok:
            # Keep any trusted duplicate intact until the intended adapter is trustworthy.
            # Otherwise one failed D-Bus write can leave the speaker trusted nowhere.
            return TrustPinResult(ok=False, failures=(detail,))
        changed.append(f"{adapter.hci}:trusted=True")

    failures: list[str] = []
    for path, interfaces in matches:
        if path == wanted or not bool(_device_property(interfaces, "Trusted")):
            continue
        hci = path.split("/")[3]
        other = Adapter(hci=hci, address="", bus="unknown", rfkill_index=None)
        ok, detail = set_trusted(device_mac, False, other)
        if ok:
            changed.append(f"{hci}:trusted=False")
        else:
            failures.append(detail)
    return TrustPinResult(
        ok=not failures,
        changed=tuple(changed),
        failures=tuple(failures),
    )


def unblock(adapter: Adapter) -> bool:
    """Clear a soft rfkill on ONE adapter. Root only.

    Never `rfkill unblock bluetooth`: that is class-wide, and its matching `rfkill block
    bluetooth` -- which scripts/bt-reset.sh rung 4 issues -- would take BOTH radios down.
    During a call the second radio is the speaker link this mode depends on, so a recovery
    aimed at one controller must not touch the other.

    Measured 2026-08-23: the dongle came up soft-blocked and therefore never ran controller
    setup ("Operation not possible due to RF-kill (132)"), which presented as a dead adapter
    rather than as a blocked one. systemd-rfkill persists that verdict keyed on the USB PORT
    PATH (`platform-3f980000.usb-usb-0:1.4:1.0:bluetooth`), so it does not follow the dongle
    to another port. Unblocking explicitly at start is cheaper than reasoning about which
    port it was last in.
    """
    if adapter.rfkill_index is None:
        return False
    if _run(["rfkill", "unblock", str(adapter.rfkill_index)])[0] == 0:
        return True
    # PATH on a non-login shell often lacks /usr/sbin, and writing the sysfs attribute
    # needs no binary at all.
    try:
        (SYS_RFKILL / f"rfkill{adapter.rfkill_index}" / "soft").write_text("0")
        return True
    except OSError:
        return False


def is_blocked(adapter: Adapter) -> bool | None:
    if adapter.rfkill_index is None:
        return None
    try:
        raw = (SYS_RFKILL / f"rfkill{adapter.rfkill_index}" / "soft").read_text()
    except OSError:
        return None
    return raw.strip() == "1"


def is_powered(adapter: Adapter, objects: dict[str, dict] | None = None) -> bool:
    tree = objects if objects is not None else managed_objects()
    entry = (tree.get(adapter.path) or {}).get("org.bluez.Adapter1") or {}
    return bool((entry.get("Powered") or {}).get("data"))


def power_on(adapter: Adapter) -> tuple[bool, str]:
    """Unblock and power one controller, addressed by its already-resolved identity.

    BlueZ restores adapter power across a daemon restart, but systemd-rfkill can restore a
    stale per-port soft block first. On the two-controller rig that left the USB speaker
    controller present in D-Bus but unusable until an operator manually unblocked it. The
    watchdog is root-scoped specifically so this recovery belongs there.

    Verify the property after the write even when busctl reports an error. The Pi's BlueZ
    stack has been observed to apply Powered=true and then return a failure while the adapter
    is registering; the resulting state, not that misleading reply, owns the outcome.
    """
    if is_blocked(adapter) is True and not unblock(adapter):
        return False, f"could not clear rfkill{adapter.rfkill_index} for {adapter.address}"
    if is_powered(adapter):
        return True, f"{adapter.address}: already powered"
    code, _, err = _run(
        [
            "busctl",
            "--system",
            "set-property",
            BLUEZ,
            adapter.path,
            "org.bluez.Adapter1",
            "Powered",
            "b",
            "true",
        ]
    )
    deadline = time.monotonic() + POWER_SETTLE_SECONDS
    while time.monotonic() < deadline:
        if is_powered(adapter):
            return True, adapter.path
        time.sleep(0.1)
    return False, f"{adapter.path}: {err.strip() or 'exit ' + str(code)}"


def describe() -> dict:
    """A snapshot for status files, invariants and logs."""
    tree = managed_objects()
    return {
        "adapters": [
            {
                "hci": adapter.hci,
                "address": adapter.address,
                "bus": adapter.bus,
                "rfkill_index": adapter.rfkill_index,
                "blocked": is_blocked(adapter),
                "powered": is_powered(adapter, tree),
            }
            for adapter in adapters(tree)
        ],
        "devices": [
            {
                "path": path,
                "adapter": path.split("/")[3],
                "address": path.split("dev_")[-1].replace("_", ":"),
                "alias": _device_property(interfaces, "Alias"),
                "connected": bool(_device_property(interfaces, "Connected")),
                "paired": bool(_device_property(interfaces, "Paired")),
            }
            for path, interfaces in sorted(tree.items())
            if "org.bluez.Device1" in interfaces
        ],
    }


if __name__ == "__main__":
    print(json.dumps(describe(), indent=2))
