#!/usr/bin/env python3
"""Passive Pixel <-> S31 Bluetooth soak.

Observes, never drives:

* the S31 USB console, when ``--port`` is given. The logger requests deasserted DTR
  and RTS before opening the port (``arrival._quiet_serial_factory``);
* the Pixel's ``dumpsys bluetooth_manager`` over adb, every ``--interval`` seconds.

It sends nothing to either device. Output (all under ``--out``):

* ``s31-serial.log``  every console line, UTC-stamped, flushed per line;
* ``samples.jsonl``   one record per adb poll;
* ``events.jsonl``    state changes, Pixel ACL history lines for the S31, S31 reboot/BT lines;
* ``summary.json``    rewritten after every poll, so a killed run still leaves one.

Run it under Windows Python (``py.exe``) so COM ports and ``adb.exe`` are reachable.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Lines on the S31 console worth an event record.
S31_EVENT_RE = re.compile(
    r"rst:|boot:|ESP-ROM|Guru Meditation|abort\(\)|Brownout|"
    r"A2DP connection state|HFP service state|HFP audio|AVRCP controller|"
    r"authentication|reconnect request|gateway ready|bound upstream|s31_uac|uac-host|LARK",
    re.IGNORECASE,
)
ACL_RE = re.compile(
    r"^\s*(?P<ts>\d\d-\d\d \d\d:\d\d:\d\d\.\d+) \[(?P<addr>[0-9A-FX:]+)\] "
    r"aclStateChangeCallback: State:(?P<state>\w+).*?reason:(?P<reason>-?\d+)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_bluetooth_manager(text: str, mac_tail: str) -> dict[str, Any]:
    """Reduce ``dumpsys bluetooth_manager`` to the fields the soak tracks.

    Android masks the first four octets, so the device is matched on ``mac_tail``
    (for example ``61:5F``).
    """

    tail = mac_tail.upper()
    result: dict[str, Any] = {
        "adapter_state": None,
        "connection_state": None,
        "bonded": False,
        "acl_bredr": None,
        "a2dp_connected": False,
        "a2dp_active": False,
        "hfp_connected": False,
        "hfp_active": False,
        "acl_history": [],
    }
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if result["adapter_state"] is None and stripped.startswith("State:"):
            result["adapter_state"] = stripped.split(":", 1)[1].strip()
        elif result["connection_state"] is None and stripped.startswith("ConnectionState:"):
            result["connection_state"] = stripped.split(":", 1)[1].strip()
        if "=>" in stripped and stripped.upper().startswith(f"XX:XX:XX:XX:{tail}"):
            result["bonded"] = "[BR/EDR]" in stripped or "BREDR" in stripped
            match = re.search(r"ACL BR/EDR:(\w)", stripped)
            if match:
                result["acl_bredr"] = match.group(1) == "Y"
        if stripped in ("A2DP:", "HFP:", "HA:", "LE Audio:", "LE HA:"):
            section = stripped[:-1]
        elif section in ("A2DP", "HFP") and stripped.upper().startswith(f"XX:XX:XX:XX:{tail}"):
            key = section.lower()
            result[f"{key}_connected"] = True
            if "ACTIVE" in stripped:
                result[f"{key}_active"] = True
        acl = ACL_RE.match(line)
        if acl and acl.group("addr").upper().endswith(tail):
            result["acl_history"].append(
                {
                    "pixel_time": acl.group("ts"),
                    "state": acl.group("state"),
                    "reason": int(acl.group("reason")),
                }
            )
    result["link_up"] = bool(
        result["adapter_state"] == "ON"
        and result["acl_bredr"]
        and result["a2dp_connected"]
        and result["hfp_connected"]
    )
    return result


STATE_KEYS = ("adapter_state", "acl_bredr", "a2dp_connected", "hfp_connected", "link_up")


def state_changes(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[str]:
    if previous is None:
        return []
    return [key for key in STATE_KEYS if previous.get(key) != current.get(key)]


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out: Path = args.out
        self.out.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.events = (self.out / "events.jsonl").open("a", encoding="utf-8")
        self.samples = (self.out / "samples.jsonl").open("a", encoding="utf-8")
        self.summary: dict[str, Any] = {
            "started_utc": utc_now(),
            "port": args.port,
            "mac_tail": args.mac_tail,
            "interval_s": args.interval,
            "polls": 0,
            "polls_link_up": 0,
            "poll_errors": 0,
            "link_drops": [],
            "pixel_acl_events": 0,
            "serial_enabled": args.port is not None and not args.no_serial,
            "s31_lines": 0,
            "s31_event_lines": 0,
            "s31_reboot_markers": 0,
            "serial_errors": [],
            "finished_utc": None,
            "completed": False,
        }
        self.seen_acl: set[tuple[str, str, int]] = set()

    def event(self, kind: str, **fields: Any) -> None:
        record = {"utc": utc_now(), "kind": kind, **fields}
        with self.lock:
            self.events.write(json.dumps(record, sort_keys=True) + "\n")
            self.events.flush()

    def write_summary(self) -> None:
        with self.lock:
            path = self.out / "summary.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.summary, indent=2, sort_keys=True) + "\n")
            tmp.replace(path)

    # --- serial ---------------------------------------------------------------------------
    def serial_loop(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "firmware" / "tools"))
        try:
            import serial  # type: ignore
            from arrival import _quiet_serial_factory  # noqa: PLC0415
        except ImportError as error:
            self.summary["serial_errors"].append({"utc": utc_now(), "error": str(error)})
            self.event("serial_unavailable", error=str(error))
            return

        factory = _quiet_serial_factory(serial.Serial)
        log = (self.out / "s31-serial.log").open("a", encoding="utf-8", newline="\n")
        while not self.stop.is_set():
            try:
                connection = factory(port=self.args.port, baudrate=115200, timeout=0.5)
            except Exception as error:  # port busy or gone; never toggle anything, just wait
                self.summary["serial_errors"].append({"utc": utc_now(), "error": str(error)})
                self.event("serial_open_failed", error=str(error))
                self.stop.wait(60)
                continue
            log.write(f"[{utc_now()}] CAPTURE_OPEN port={self.args.port}\n")
            log.flush()
            try:
                while not self.stop.is_set():
                    raw = connection.readline()
                    if not raw:
                        continue
                    text = bytes(raw).decode("utf-8", errors="replace").rstrip("\r\n")
                    log.write(f"[{utc_now()}] {text}\n")
                    log.flush()
                    self.summary["s31_lines"] += 1
                    if S31_EVENT_RE.search(text):
                        self.summary["s31_event_lines"] += 1
                        if re.search(r"rst:|ESP-ROM|Guru Meditation|Brownout", text):
                            self.summary["s31_reboot_markers"] += 1
                        self.event("s31_line", line=text)
            except Exception as error:
                self.summary["serial_errors"].append({"utc": utc_now(), "error": str(error)})
                self.event("serial_lost", error=str(error))
            finally:
                try:
                    connection.close()
                except Exception:
                    pass
            log.write(f"[{utc_now()}] CAPTURE_CLOSED\n")
            log.flush()
            self.stop.wait(30)
        log.close()

    # --- adb ------------------------------------------------------------------------------
    def poll_once(self, previous: dict[str, Any] | None) -> dict[str, Any] | None:
        try:
            completed = subprocess.run(
                [self.args.adb, "shell", "dumpsys", "bluetooth_manager"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.summary["poll_errors"] += 1
            self.event("adb_error", error=str(error))
            return previous
        if completed.returncode != 0 or "State:" not in completed.stdout:
            self.summary["poll_errors"] += 1
            self.event("adb_error", returncode=completed.returncode, stderr=completed.stderr[-300:])
            return previous
        state = parse_bluetooth_manager(completed.stdout, self.args.mac_tail)
        history = state.pop("acl_history")
        for item in history:
            key = (item["pixel_time"], item["state"], item["reason"])
            if key not in self.seen_acl:
                self.seen_acl.add(key)
                self.summary["pixel_acl_events"] += 1
                self.event("pixel_acl", **item)
        self.summary["polls"] += 1
        if state["link_up"]:
            self.summary["polls_link_up"] += 1
        sample = {"utc": utc_now(), **state}
        with self.lock:
            self.samples.write(json.dumps(sample, sort_keys=True) + "\n")
            self.samples.flush()
        changed = state_changes(previous, state)
        if changed:
            self.event("state_change", changed=changed, state=state)
            if previous and previous.get("link_up") and not state["link_up"]:
                self.summary["link_drops"].append({"utc": sample["utc"], "state": state})
        return state

    def run(self) -> int:
        deadline = time.monotonic() + self.args.seconds
        deadline_utc = datetime.fromtimestamp(time.time() + self.args.seconds, timezone.utc)
        self.event("soak_start", deadline_utc=deadline_utc.isoformat())
        reader = threading.Thread(target=self.serial_loop, daemon=True)
        if self.summary["serial_enabled"]:
            reader.start()
        previous: dict[str, Any] | None = None
        try:
            while time.monotonic() < deadline:
                previous = self.poll_once(previous)
                self.write_summary()
                self.stop.wait(min(self.args.interval, max(0.0, deadline - time.monotonic())))
            self.summary["completed"] = True
        except KeyboardInterrupt:
            self.event("interrupted")
        finally:
            self.stop.set()
            if reader.is_alive():
                reader.join(timeout=5)
            self.summary["finished_utc"] = utc_now()
            self.event("soak_end")
            self.write_summary()
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="explicitly enable S31 serial capture on this port")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--mac-tail", default="61:5F")
    parser.add_argument("--interval", type=float, default=180.0)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-serial", action="store_true", help="adb polling only")
    return parser


def main(argv: list[str] | None = None) -> int:
    return Soak(build_parser().parse_args(argv)).run()


if __name__ == "__main__":
    raise SystemExit(main())
