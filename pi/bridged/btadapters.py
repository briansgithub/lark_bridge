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

import importlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Linux production path; the fallback keeps host tests importable on Windows.
    fcntl: Any = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - exercised by the Windows checkout
    fcntl = None

SYS_BLUETOOTH = Path("/sys/class/bluetooth")
SYS_RFKILL = Path("/sys/class/rfkill")
BLUEZ = "org.bluez"

# Long enough for a page attempt to a device that is present but asleep; short enough that
# a watchdog poll loop is not blocked for a whole cycle by a device that is genuinely gone.
CONNECT_TIMEOUT = 45.0
QUERY_TIMEOUT = 15.0
POWER_SETTLE_SECONDS = 3.0
DISCOVERY_SECONDS = 12.0
DISCOVERY_START_TIMEOUT = 5.0
CHILD_EXIT_SECONDS = 2.0
PAIR_TIMEOUT = 45.0
SERVICE_RESOLUTION_TIMEOUT = 15.0
MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
DEVICE_PATH_RE = re.compile(
    r"(?P<path>/org/bluez/hci[^/\s\"']+/dev_(?P<mac>(?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2}))"
)
RSSI_RE = re.compile(r"(?:RSSI|rssi)[^\r\n-]*(-?\d{1,3})")


class BluetoothOperationError(RuntimeError):
    """A bounded controller operation could not finish safely."""


class BluetoothOperationCancelled(BluetoothOperationError):
    """The owner disappeared or shutdown was requested."""


def connect_in_progress(ok: bool, detail: str) -> bool:
    """Keep BlueZ's pending-operation signal distinct from ordinary failures."""
    if ok:
        return False
    normalized = detail.casefold()
    return (
        "org.bluez.error.inprogress" in normalized
        or "call failed: in progress" in normalized
        or "operation already in progress" in normalized
    )


@dataclass(frozen=True)
class Adapter:
    """One controller. `hci` is a lookup result, never an identity -- do not cache it."""

    hci: str
    address: str
    bus: str  # "UART" (onboard BCM43438) or "USB" (dongle)
    rfkill_index: int | None
    usb_vendor_id: str | None = None
    usb_product_id: str | None = None
    usb_parent: str | None = None
    usb_interface: str | None = None
    driver: str | None = None
    product: str | None = None
    manufacturer: str | None = None

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


@dataclass(frozen=True)
class DiscoveryRun:
    """Addresses observed inside one completed, controller-specific inquiry window."""

    observations: dict[str, int | None]
    started_monotonic: float
    completed_monotonic: float


@dataclass(frozen=True)
class PairResult:
    ok: bool
    pin_requested: bool = False
    detail: str = ""


def canonical_mac(value: object) -> str | None:
    """Return one canonical public Bluetooth address, rejecting loose/partial syntax."""
    candidate = str(value).strip().upper() if isinstance(value, str) else ""
    return candidate if MAC_RE.fullmatch(candidate) else None


class DiscoveryAccumulator:
    """Filter BlueZ monitor events to one adapter and one exact inquiry interval."""

    def __init__(self, adapter_path: str, started: float, deadline: float) -> None:
        self.adapter_path = adapter_path.rstrip("/")
        self.started = started
        self.deadline = deadline
        self.observations: dict[str, int | None] = {}

    def add(self, observed_at: float, line: str) -> None:
        # The contract says after discovery started and before its deadline. Events queued
        # before StartDiscovery must not become results merely because we read them later.
        if observed_at <= self.started or observed_at >= self.deadline:
            return
        parsed = parse_discovery_event(line, self.adapter_path)
        if parsed is None:
            return
        address, rssi = parsed
        previous = self.observations.get(address)
        if address not in self.observations or (
            rssi is not None and (previous is None or rssi > previous)
        ):
            self.observations[address] = rssi


def parse_discovery_event(line: str, adapter_path: str) -> tuple[str, int | None] | None:
    """Parse one busctl monitor event without accepting another controller's object."""
    match = DEVICE_PATH_RE.search(line)
    if match is None or not match.group("path").startswith(adapter_path.rstrip("/") + "/dev_"):
        return None
    # A remembered object's Connected/Trusted churn is not an inquiry observation. BlueZ
    # discovery either adds the object or changes discovery-fed identity/radio properties.
    if "InterfacesAdded" not in line and not re.search(
        r"\b(?:RSSI|Class|Name|Alias|UUIDs|AddressType|TxPower)\b", line
    ):
        return None
    address = match.group("mac").replace("_", ":").upper()
    if canonical_mac(address) is None:
        return None
    rssi_match = RSSI_RE.search(line)
    rssi = int(rssi_match.group(1)) if rssi_match else None
    if rssi is not None and not -127 <= rssi <= 20:
        rssi = None
    return address, rssi


_host_radio_lock = threading.Lock()


def default_radio_lock_path() -> Path:
    """The shared user-runtime lock used by output control and the root watchdog."""
    configured = os.environ.get("BRIDGE_OUTPUT_RADIO_LOCK")
    if configured:
        return Path(configured)
    default_uid = str(os.getuid()) if hasattr(os, "getuid") else "1000"
    uid = os.environ.get("BRIDGE_USER_UID", default_uid)
    return Path(f"/run/user/{uid}/bridge-output-radio.lock")


@contextmanager
def speaker_radio_lock(
    path: Path | None = None,
    *,
    blocking: bool = True,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[bool]:
    """Hold the cross-process speaker transaction lock, or yield False nonblocking.

    A cancellable blocking acquisition is used by the RFCOMM service.  A plain blocking
    ``flock`` cannot observe service shutdown while another process owns the radio.
    """
    target = path or default_radio_lock_path()
    if fcntl is None:
        if blocking and cancelled is not None:
            acquired = False
            while not acquired:
                if cancelled():
                    raise BluetoothOperationCancelled(
                        "operation cancelled while waiting for the speaker radio"
                    )
                acquired = _host_radio_lock.acquire(blocking=False)
                if not acquired:
                    time.sleep(0.05)
        else:
            acquired = _host_radio_lock.acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                _host_radio_lock.release()
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = target.open("a+", encoding="utf-8")
    except PermissionError:
        # The root watchdog may create the 0644 inode before the uid-1000 service starts.
        # flock only needs an open descriptor; read-only keeps both creation orders valid.
        handle = target.open("r", encoding="utf-8")
    acquired = False
    try:
        if blocking and cancelled is not None:
            while not acquired:
                if cancelled():
                    raise BluetoothOperationCancelled(
                        "operation cancelled while waiting for the speaker radio"
                    )
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    time.sleep(0.05)
        else:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle.fileno(), flags)
                acquired = True
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


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


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Bounded process-group cleanup shared by discovery, agents, and D-Bus calls."""
    if process.poll() is not None:
        process.wait()
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)  # type: ignore[attr-defined]
        else:  # pragma: no cover - production is Linux
            process.terminate()
        process.wait(timeout=CHILD_EXIT_SECONDS)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(  # type: ignore[attr-defined]
                os.getpgid(process.pid),  # type: ignore[attr-defined]
                signal.SIGKILL,  # type: ignore[attr-defined]
            )
        else:  # pragma: no cover - production is Linux
            process.kill()
        process.wait(timeout=CHILD_EXIT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait()


class _LineProcess:
    """A long-lived child whose output is timestamped as it arrives."""

    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=os.name == "posix",
        )
        self.lines: queue.Queue[tuple[float, str]] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put((time.monotonic(), line))

    def send(self, command: str) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise BluetoothOperationError(f"child exited before {command!r}")
        self.process.stdin.write(command.rstrip("\n") + "\n")
        self.process.stdin.flush()

    def get(self, timeout: float) -> tuple[float, str] | None:
        try:
            return self.lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def drain(self) -> list[tuple[float, str]]:
        found: list[tuple[float, str]] = []
        while True:
            try:
                found.append(self.lines.get_nowait())
            except queue.Empty:
                return found

    def stop(self, *commands: str) -> None:
        if self.process.poll() is None:
            for command in commands:
                try:
                    self.send(command)
                except (BluetoothOperationError, BrokenPipeError, OSError):
                    break
            try:
                self.process.wait(timeout=CHILD_EXIT_SECONDS)
            except subprocess.TimeoutExpired:
                _terminate_process(self.process)
        else:
            self.process.wait()
        self.reader.join(timeout=CHILD_EXIT_SECONDS)


def _run_cancellable(
    command: list[str],
    *,
    timeout: float,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[int, str, str]:
    """Run a short D-Bus helper with liveness callbacks and guaranteed group reap."""
    if cancelled is not None and cancelled():
        raise BluetoothOperationCancelled("operation owner disconnected")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        return 127, "", str(exc)
    deadline = time.monotonic() + timeout
    next_heartbeat = time.monotonic()
    try:
        while process.poll() is None:
            now = time.monotonic()
            if cancelled is not None and cancelled():
                raise BluetoothOperationCancelled("operation owner disconnected")
            if now >= deadline:
                _terminate_process(process)
                return 124, "", "timed out"
            if heartbeat is not None and now >= next_heartbeat:
                heartbeat()
                next_heartbeat = now + 1.0
            time.sleep(min(0.1, max(0.0, deadline - now)))
        stdout, stderr = process.communicate()
        return int(process.returncode or 0), stdout, stderr
    except BaseException:
        _terminate_process(process)
        raise


def _read_sysfs(path: Path, name: str) -> str | None:
    try:
        value = (path / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _uevent_driver(path: Path) -> str | None:
    try:
        lines = (path / "uevent").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("DRIVER=") and line[7:]:
            return line[7:]
    return None


def _bound_driver(path: Path) -> str | None:
    try:
        driver = (path / "driver").resolve(strict=True).name
    except OSError:
        driver = None
    return driver or _uevent_driver(path)


def _sysfs_identity(path: Path) -> dict[str, str | None]:
    """Derive physical diagnostics from ancestors without trusting their topology.

    USB parent/interface names are intentionally reported but never compared by the role
    resolver.  They change when a dongle moves ports; VID:PID and the permanent address do not.
    """
    ancestors = [path, *path.parents]
    usb_device: Path | None = None
    usb_interface: Path | None = None
    driver: str | None = None
    for candidate in ancestors:
        if usb_interface is None and (
            _read_sysfs(candidate, "bInterfaceClass") is not None
            or re.fullmatch(r"[^:]+:\d+\.\d+", candidate.name) is not None
        ):
            usb_interface = candidate
            driver = _bound_driver(candidate)
        if (
            usb_device is None
            and _read_sysfs(candidate, "idVendor") is not None
            and _read_sysfs(candidate, "idProduct") is not None
        ):
            usb_device = candidate
        if usb_device is not None and usb_interface is not None:
            break

    if usb_device is not None:
        return {
            "bus": "USB",
            "usb_vendor_id": (_read_sysfs(usb_device, "idVendor") or "").lower() or None,
            "usb_product_id": (_read_sysfs(usb_device, "idProduct") or "").lower() or None,
            "usb_parent": usb_device.name,
            "usb_interface": usb_interface.name if usb_interface is not None else None,
            "driver": driver or _bound_driver(usb_device),
            "product": _read_sysfs(usb_device, "product"),
            "manufacturer": _read_sysfs(usb_device, "manufacturer"),
        }

    serial = next(
        (
            candidate
            for candidate in ancestors
            if "serial" in candidate.as_posix().lower() or "uart" in candidate.name.lower()
        ),
        None,
    )
    for candidate in ancestors:
        driver = _bound_driver(candidate)
        if driver is not None:
            break
    return {
        "bus": "UART" if serial is not None else "unknown",
        "usb_vendor_id": None,
        "usb_product_id": None,
        "usb_parent": None,
        "usb_interface": None,
        "driver": driver,
        "product": None,
        "manufacturer": None,
    }


def _controller_sysfs_identity(hci: str) -> dict[str, str | None]:
    try:
        path = (SYS_BLUETOOTH / hci).resolve(strict=True)
    except OSError:
        return _sysfs_identity(SYS_BLUETOOTH / hci)
    return _sysfs_identity(path)


def _bus_type(hci: str) -> str:
    """UART vs USB, from where the device hangs in sysfs.

    This decides which recovery a wedged controller needs: the onboard part recovers only
    by unbinding its serdev driver (bt-reset.sh rung 6), a dongle by unbinding btusb.
    Applying the wrong one is a no-op at best.
    """
    return str(_controller_sysfs_identity(hci)["bus"])


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
        identity = _controller_sysfs_identity(entry.name)
        found.append(
            Adapter(
                hci=entry.name,
                address=address,
                bus=str(identity["bus"]),
                rfkill_index=_rfkill_index(entry.name),
                usb_vendor_id=identity["usb_vendor_id"],
                usb_product_id=identity["usb_product_id"],
                usb_parent=identity["usb_parent"],
                usb_interface=identity["usb_interface"],
                driver=identity["driver"],
                product=identity["product"],
                manufacturer=identity["manufacturer"],
            )
        )
    return sorted(found, key=lambda adapter: (adapter.address == "", adapter.address))


def adapter_by_address(address: str, objects: dict[str, dict] | None = None) -> Adapter | None:
    wanted = address.strip().upper()
    matches = [adapter for adapter in adapters(objects) if adapter.address == wanted]
    return matches[0] if len(matches) == 1 else None


def managed_objects(
    *,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, dict]:
    """BlueZ's object tree, or an empty dict if bluetoothd is not answering.

    Empty is deliberately indistinguishable from "no devices": callers must treat an absent
    device as "cannot act", never as "device is gone, take recovery action".
    """
    command = [
        "busctl",
        "--system",
        "--json=short",
        "call",
        BLUEZ,
        "/",
        "org.freedesktop.DBus.ObjectManager",
        "GetManagedObjects",
    ]
    if cancelled is not None or heartbeat is not None:
        code, out, _ = _run_cancellable(
            command,
            timeout=QUERY_TIMEOUT,
            cancelled=cancelled,
            heartbeat=heartbeat,
        )
    else:
        code, out, _ = _run(command)
    if code != 0:
        return {}
    try:
        return json.loads(out)["data"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {}


def _device_property(interfaces: dict, name: str):
    return ((interfaces.get("org.bluez.Device1") or {}).get(name) or {}).get("data")


def device_properties(
    adapter: Adapter, device_mac: str, objects: dict[str, dict] | None = None
) -> dict:
    """The explicit Device1 property map on one already-resolved controller."""
    tree = objects if objects is not None else managed_objects()
    return (tree.get(path_for(adapter, device_mac)) or {}).get("org.bluez.Device1") or {}


def device_property(
    adapter: Adapter,
    device_mac: str,
    name: str,
    objects: dict[str, dict] | None = None,
):
    return (device_properties(adapter, device_mac, objects).get(name) or {}).get("data")


def paired_on(adapter: Adapter, device_mac: str, objects: dict[str, dict] | None = None) -> bool:
    return bool(device_property(adapter, device_mac, "Paired", objects))


def connected_on(adapter: Adapter, device_mac: str, objects: dict[str, dict] | None = None) -> bool:
    return bool(device_property(adapter, device_mac, "Connected", objects))


def path_for(adapter: Adapter, device_mac: str) -> str:
    """The path a bond WOULD have on this adapter. Does not assert that it exists."""
    return f"{adapter.path}/dev_" + device_mac.strip().upper().replace(":", "_")


def discover_bredr(
    adapter: Adapter,
    *,
    duration: float = DISCOVERY_SECONDS,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> DiscoveryRun:
    """Own one explicit-controller BR/EDR inquiry and return only in-window events.

    ``bluetoothctl`` owns the BlueZ discovery client for the whole interval. A separate
    ``busctl monitor`` child supplies object paths, which is what lets us reject events from
    the call controller even when both controllers report the same public device address.
    """
    if duration != DISCOVERY_SECONDS:
        raise ValueError("speaker discovery duration is fixed at 12 seconds")
    if canonical_mac(adapter.address) != adapter.address:
        raise BluetoothOperationError("speaker controller has no canonical permanent address")

    owner: _LineProcess | None = None
    monitor: _LineProcess | None = None
    started: float | None = None
    accumulator: DiscoveryAccumulator | None = None
    try:
        # Monitoring the system bus is privileged on the deployed image.  The user
        # services do have a passwordless sudo grant, while an unprivileged monitor
        # exits immediately with AccessDenied and turns every otherwise-valid scan
        # into "discovery helper exited early".
        monitor = _LineProcess(
            ["sudo", "-n", "busctl", "--system", "--json=short", "monitor", BLUEZ]
        )
        owner = _LineProcess(["bluetoothctl"])
        owner.send(f"select {adapter.address}")
        select_deadline = time.monotonic() + DISCOVERY_START_TIMEOUT
        selected = False
        while time.monotonic() < select_deadline:
            if cancelled is not None and cancelled():
                raise BluetoothOperationCancelled("scan owner disconnected")
            item = owner.get(min(0.1, select_deadline - time.monotonic()))
            if item is None:
                if owner.process.poll() is not None:
                    break
                continue
            line = item[1]
            if adapter.address in line and "Controller" in line and "not available" not in line:
                selected = True
                break
            if "not available" in line or "Failed" in line:
                break
        if not selected:
            raise BluetoothOperationError("configured speaker controller could not be selected")

        owner.send("scan bredr")
        start_deadline = time.monotonic() + DISCOVERY_START_TIMEOUT
        while time.monotonic() < start_deadline:
            if cancelled is not None and cancelled():
                raise BluetoothOperationCancelled("scan owner disconnected")
            item = owner.get(min(0.1, start_deadline - time.monotonic()))
            if item is None:
                if owner.process.poll() is not None:
                    break
                continue
            line = item[1]
            if "Discovery started" in line or (
                adapter.address in line and "Discovering: yes" in line
            ):
                started = time.monotonic()
                break
            if "Failed to start discovery" in line or "not available" in line:
                break
        if started is None:
            raise BluetoothOperationError("BR/EDR discovery did not start")

        deadline = started + duration
        accumulator = DiscoveryAccumulator(adapter.path, started, deadline)
        if progress is not None:
            progress(0, int(duration * 1000))
        next_progress = started + 1.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if cancelled is not None and cancelled():
                raise BluetoothOperationCancelled("scan owner disconnected")
            if owner.process.poll() is not None or monitor.process.poll() is not None:
                raise BluetoothOperationError("discovery helper exited early")
            item = monitor.get(min(0.1, deadline - now))
            if item is not None:
                accumulator.add(item[0], item[1])
            now = time.monotonic()
            if progress is not None and now >= next_progress and now < deadline:
                progress(min(int((now - started) * 1000), 11_999), int(duration * 1000))
                next_progress += 1.0
        completed = time.monotonic()
        return DiscoveryRun(dict(accumulator.observations), started, completed)
    finally:
        # Stop only if this operation positively observed StartDiscovery success.
        if owner is not None:
            commands = ("scan off", "quit") if started is not None else ("quit",)
            owner.stop(*commands)
        if monitor is not None:
            monitor.stop()


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
    verb: str,
    device_mac: str,
    adapter: Adapter | None,
    timeout: float,
    *,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[bool, str]:
    path: str | None
    if adapter is not None:
        path = path_for(adapter, device_mac)
    else:
        path = device_path(device_mac)
        if path is None:
            return False, f"no bond for {device_mac} on any adapter"
    command = ["busctl", "--system", "call", BLUEZ, path, "org.bluez.Device1", verb]
    if cancelled is not None or heartbeat is not None:
        code, _, err = _run_cancellable(
            command, timeout=timeout, cancelled=cancelled, heartbeat=heartbeat
        )
    else:
        code, _, err = _run(command, timeout=timeout)
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


# A2DP Sink. Connecting this profile specifically, rather than everything a device offers,
# is what keeps a speaker from also becoming an HFP endpoint.
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"


def connect(
    device_mac: str,
    adapter: Adapter | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Connect a bond on an EXPLICIT adapter. Returns (ok, detail).

    This is the call `bluetoothctl connect` cannot make safely with two controllers.

    Prefer connect_profile() for speakers: this brings up everything the remote offers.
    """
    return _act("Connect", device_mac, adapter, CONNECT_TIMEOUT, cancelled=cancelled)


def connect_profile(
    device_mac: str,
    adapter: Adapter | None = None,
    uuid: str = A2DP_SINK_UUID,
    timeout: float | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
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
    command = [
        "busctl",
        "--system",
        "call",
        BLUEZ,
        path,
        "org.bluez.Device1",
        "ConnectProfile",
        "s",
        uuid,
    ]
    effective_timeout = timeout if timeout is not None else CONNECT_TIMEOUT
    if cancelled is not None or heartbeat is not None:
        code, _, err = _run_cancellable(
            command,
            timeout=effective_timeout,
            cancelled=cancelled,
            heartbeat=heartbeat,
        )
    else:
        code, _, err = _run(command, timeout=effective_timeout)
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


def cancel_pairing(device_mac: str, adapter: Adapter) -> None:
    """Best-effort cancellation on the one explicit object used by Pair."""
    _run(
        [
            "busctl",
            "--system",
            "call",
            BLUEZ,
            path_for(adapter, device_mac),
            "org.bluez.Device1",
            "CancelPairing",
        ],
        timeout=QUERY_TIMEOUT,
    )


def remove_device(device_mac: str, adapter: Adapter) -> tuple[bool, str]:
    """Remove exactly one adapter-owned object; never address a duplicate implicitly."""
    path = path_for(adapter, device_mac)
    code, _, err = _run(
        [
            "busctl",
            "--system",
            "call",
            BLUEZ,
            adapter.path,
            "org.bluez.Adapter1",
            "RemoveDevice",
            "o",
            path,
        ],
        timeout=QUERY_TIMEOUT,
    )
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


_PIN_MARKERS = (
    "request pin",
    "enter pin",
    "request passkey",
    "enter passkey",
    "confirm passkey",
    "confirmation request",
    "display passkey",
    "display pin",
)


def pair_device(
    device_mac: str,
    adapter: Adapter,
    *,
    timeout: float = PAIR_TIMEOUT,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> PairResult:
    """Pair one explicit Device1 with one temporary NoInputNoOutput agent."""
    address = canonical_mac(device_mac)
    if address is None:
        return PairResult(False, detail="invalid Bluetooth device address")
    agent: _LineProcess | None = None
    pin_requested = False
    started = time.monotonic()

    def inspect_agent() -> None:
        nonlocal pin_requested
        assert agent is not None
        for _observed, line in agent.drain():
            lowered = line.casefold()
            if any(marker in lowered for marker in _PIN_MARKERS):
                pin_requested = True

    def is_cancelled() -> bool:
        inspect_agent()
        return pin_requested or (cancelled is not None and cancelled())

    def pulse() -> None:
        inspect_agent()
        if heartbeat is not None:
            heartbeat()

    try:
        # BlueZ restricts temporary-agent registration on this appliance's system
        # bus.  The user service has a non-interactive sudo grant; use it only for
        # this short-lived NoInputNoOutput agent, which is torn down in finally.
        # This bluetoothctl build registers an agent at startup even without --agent.
        # Specify the capability there, wait for that one registration to finish, and
        # never issue the interactive `agent` toggle (which would unregister it).
        agent = _LineProcess(["sudo", "-n", "bluetoothctl", "--agent", "NoInputNoOutput"])
        register_deadline = min(started + 3.0, started + timeout)
        registered = False
        while time.monotonic() < register_deadline:
            if cancelled is not None and cancelled():
                raise BluetoothOperationCancelled("pairing owner disconnected")
            item = agent.get(min(0.1, register_deadline - time.monotonic()))
            if item is None:
                if agent.process.poll() is not None:
                    break
                continue
            lowered = item[1].casefold()
            if any(marker in lowered for marker in _PIN_MARKERS):
                pin_requested = True
                break
            if "agent registered" in lowered:
                registered = True
                break
            if "failed" in lowered:
                break
        if pin_requested:
            cancel_pairing(address, adapter)
            return PairResult(False, True, "pairing requested a PIN or passkey")
        if not registered:
            return PairResult(False, detail="temporary NoInputNoOutput agent did not register")

        agent.send("default-agent")
        default_deadline = min(time.monotonic() + 3.0, started + timeout)
        defaulted = False
        while time.monotonic() < default_deadline:
            if cancelled is not None and cancelled():
                raise BluetoothOperationCancelled("pairing owner disconnected")
            item = agent.get(min(0.1, default_deadline - time.monotonic()))
            if item is None:
                if agent.process.poll() is not None:
                    break
                continue
            lowered = item[1].casefold()
            if any(marker in lowered for marker in _PIN_MARKERS):
                pin_requested = True
                break
            if "default agent request successful" in lowered:
                defaulted = True
                break
            if "failed" in lowered:
                break
        if pin_requested:
            cancel_pairing(address, adapter)
            return PairResult(False, True, "pairing requested a PIN or passkey")
        if not defaulted:
            return PairResult(False, detail="temporary NoInputNoOutput agent was not made default")

        remaining = max(0.1, started + timeout - time.monotonic())
        path = path_for(adapter, address)
        try:
            code, _, err = _run_cancellable(
                ["busctl", "--system", "call", BLUEZ, path, "org.bluez.Device1", "Pair"],
                timeout=remaining,
                cancelled=is_cancelled,
                heartbeat=pulse,
            )
        except BluetoothOperationCancelled:
            inspect_agent()
            if pin_requested:
                cancel_pairing(address, adapter)
                return PairResult(False, True, "pairing requested a PIN or passkey")
            raise
        inspect_agent()
        if pin_requested:
            cancel_pairing(address, adapter)
            return PairResult(False, True, "pairing requested a PIN or passkey")
        if code != 0:
            cancel_pairing(address, adapter)
            return PairResult(False, detail=err.strip() or f"Pair exited {code}")

        deadline = started + timeout
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                raise BluetoothOperationCancelled("pairing owner disconnected")
            if paired_on(adapter, address):
                return PairResult(True, detail=path)
            if heartbeat is not None:
                heartbeat()
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        cancel_pairing(address, adapter)
        return PairResult(False, detail="Paired=true was not observed before the deadline")
    finally:
        if agent is not None:
            agent.stop("agent off", "quit")


def disconnect(
    device_mac: str,
    adapter: Adapter | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    return _act("Disconnect", device_mac, adapter, QUERY_TIMEOUT, cancelled=cancelled)


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
            "busctl",
            "--system",
            "set-property",
            BLUEZ,
            path,
            "org.bluez.Device1",
            "Alias",
            "s",
            alias,
        ]
    )
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


def set_trusted(
    device_mac: str,
    trusted: bool,
    adapter: Adapter | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
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
    command = [
        "busctl",
        "--system",
        "set-property",
        BLUEZ,
        path,
        "org.bluez.Device1",
        "Trusted",
        "b",
        "true" if trusted else "false",
    ]
    if cancelled is not None:
        code, _, err = _run_cancellable(command, timeout=QUERY_TIMEOUT, cancelled=cancelled)
    else:
        code, _, err = _run(command)
    if code == 0:
        return True, path
    return False, f"{path}: {err.strip() or 'exit ' + str(code)}"


def pin_to_adapter(
    device_mac: str,
    adapter: Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> TrustPinResult:
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
    tree = managed_objects(cancelled=cancelled)
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
        ok, detail = set_trusted(device_mac, True, adapter, cancelled=cancelled)
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
        ok, detail = set_trusted(device_mac, False, other, cancelled=cancelled)
        if ok:
            changed.append(f"{hci}:trusted=False")
        else:
            failures.append(detail)
    return TrustPinResult(
        ok=not failures,
        changed=tuple(changed),
        failures=tuple(failures),
    )


def unblock(
    adapter: Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
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
    if cancelled is not None and cancelled():
        raise BluetoothOperationCancelled("watchdog shutdown requested")
    if _run(["rfkill", "unblock", str(adapter.rfkill_index)])[0] == 0:
        return True
    # PATH on a non-login shell often lacks /usr/sbin, and writing the sysfs attribute
    # needs no binary at all.
    try:
        if cancelled is not None and cancelled():
            raise BluetoothOperationCancelled("watchdog shutdown requested")
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


def power_on(
    adapter: Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """Unblock and power one controller, addressed by its already-resolved identity.

    BlueZ restores adapter power across a daemon restart, but systemd-rfkill can restore a
    stale per-port soft block first. On the two-controller rig that left the USB speaker
    controller present in D-Bus but unusable until an operator manually unblocked it. The
    watchdog is root-scoped specifically so this recovery belongs there.

    Verify the property after the write even when busctl reports an error. The Pi's BlueZ
    stack has been observed to apply Powered=true and then return a failure while the adapter
    is registering; the resulting state, not that misleading reply, owns the outcome.
    """
    if is_blocked(adapter) is True and not unblock(adapter, cancelled=cancelled):
        return False, f"could not clear rfkill{adapter.rfkill_index} for {adapter.address}"
    if is_powered(adapter):
        return True, f"{adapter.address}: already powered"
    if cancelled is not None and cancelled():
        raise BluetoothOperationCancelled("watchdog shutdown requested")
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
        if cancelled is not None and cancelled():
            raise BluetoothOperationCancelled("watchdog shutdown requested")
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
                "usb_vendor_id": adapter.usb_vendor_id,
                "usb_product_id": adapter.usb_product_id,
                "usb_parent": adapter.usb_parent,
                "usb_interface": adapter.usb_interface,
                "driver": adapter.driver,
                "product": adapter.product,
                "manufacturer": adapter.manufacturer,
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
