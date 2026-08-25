#!/usr/bin/env python3
"""Capture synchronized E16 Bluetooth/output acceptance evidence from Windows.

The script is deliberately passive: it starts read-only Pi/Android monitors, records
before/after snapshots, and never sends a Bluetooth command or Android input event.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = REPO / "docs" / "experiments" / "results" / "E16"
ADB_CANDIDATES = (
    Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools" / "adb.exe",
    Path(os.environ.get("ANDROID_SDK_ROOT", "")) / "platform-tools" / "adb.exe",
    Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
)
WINDOWS_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class Capture:
    name: str
    process: subprocess.Popen[bytes]
    stdout_handle: object
    stderr_handle: object
    command: list[str]


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def locate_adb() -> Path:
    for candidate in ADB_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("adb")
    if found:
        return Path(found)
    raise SystemExit("adb was not found")


def run_snapshot(command: list[str], target: Path) -> dict[str, object]:
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        timeout=45,
        creationflags=WINDOWS_NO_WINDOW,
    )
    target.write_bytes(result.stdout)
    target.with_suffix(target.suffix + ".stderr").write_bytes(result.stderr)
    return {"command": command, "returncode": result.returncode}


def start_capture(name: str, command: list[str], directory: Path) -> Capture:
    stdout_handle = (directory / f"{name}.log").open("wb")
    stderr_handle = (directory / f"{name}.stderr.log").open("wb")
    process = subprocess.Popen(
        command,
        stdout=stdout_handle,
        stderr=stderr_handle,
        creationflags=WINDOWS_NO_WINDOW,
    )
    return Capture(name, process, stdout_handle, stderr_handle, command)


def stop_capture(capture: Capture) -> int:
    if capture.process.poll() is None:
        capture.process.terminate()
        try:
            capture.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            capture.process.kill()
            capture.process.wait(timeout=5)
    capture.stdout_handle.close()
    capture.stderr_handle.close()
    return int(capture.process.returncode or 0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="larkbridge")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--label", default="live")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 5 <= args.seconds <= 600:
        parser.error("--seconds must be between 5 and 600")

    ssh = shutil.which("ssh")
    if not ssh:
        raise SystemExit("ssh was not found")
    adb = locate_adb()
    directory = (args.output or DEFAULT_RESULTS / f"{utc_stamp()}-{args.label}").resolve()
    directory.mkdir(parents=True, exist_ok=False)

    remote_snapshot = r"""
set -u
date --iso-8601=ns
id
findmnt -no SOURCE,FSTYPE,OPTIONS /media/root-ro || true
systemctl is-active bluetooth.service bridge-btwatchdog.service bridge-storage-guard.service || true
systemctl --user is-active bridge-output-remote.service bridge-supervisor.service pipewire.service wireplumber.service || true
printf '%s\n' '--- bridge-status ---'
cat /run/user/1000/bridge-status.json
printf '%s\n' '--- storage-health ---'
cat /run/larkbridge/storage-health.json
printf '%s\n' '--- active-config-sha256 ---'
sha256sum /home/admin/rpi-lark-bridge/config/bridge.toml
printf '%s\n' '--- durable-configs ---'
sudo -n find /var/lib/larkbridge-persist/config -mindepth 2 -maxdepth 2 -name bridge.toml -exec sha256sum {} +
printf '%s\n' '--- a40-hci0 ---'
busctl --system introspect org.bluez /org/bluez/hci0/dev_98_47_44_CD_73_DE org.bluez.Device1 || true
printf '%s\n' '--- a40-hci1 ---'
busctl --system introspect org.bluez /org/bluez/hci1/dev_98_47_44_CD_73_DE org.bluez.Device1 || true
printf '%s\n' '--- bluez-tree ---'
busctl --system tree org.bluez
printf '%s\n' '--- pipewire-status ---'
XDG_RUNTIME_DIR=/run/user/1000 wpctl status --name || true
printf '%s\n' '--- pipewire-graph ---'
XDG_RUNTIME_DIR=/run/user/1000 pw-dump || true
""".strip()
    android_snapshot = (
        "shell",
        "sh",
        "-c",
        "date '+%Y-%m-%dT%H:%M:%S.%N%z'; "
        "dumpsys activity activities | grep -m1 mResumedActivity; "
        "dumpsys audio; dumpsys bluetooth_manager",
    )

    manifest: dict[str, object] = {
        "schema": 1,
        "label": args.label,
        "host": args.host,
        "planned_seconds": args.seconds,
        "started_utc": datetime.now(UTC).isoformat(),
        "directory": str(directory),
        "snapshots": [],
        "captures": [],
    }
    manifest["snapshots"].append(  # type: ignore[union-attr]
        run_snapshot([ssh, args.host, remote_snapshot], directory / "pi-before.log")
    )
    manifest["snapshots"].append(  # type: ignore[union-attr]
        run_snapshot([str(adb), *android_snapshot], directory / "android-before.log")
    )

    timeout_seconds = args.seconds + 10
    state_loop = r"""
set -u
while :; do
  date --iso-8601=ns
  jq -c '{state,call:.call.hfp_nodes_present,output:{desired:.output.desired_id,chosen:.output.chosen.id,reason:.output.reason,candidates:[.output.candidates[]|{id,present,connected,adapter,adapter_address,setup_state}]}}' /run/user/1000/bridge-status.json
  for path in /org/bluez/hci0/dev_98_47_44_CD_73_DE /org/bluez/hci1/dev_98_47_44_CD_73_DE; do
    printf '%s ' "$path"
    busctl --system get-property org.bluez "$path" org.bluez.Device1 Paired 2>/dev/null || printf 'absent\n'
  done
  sleep 1
done
""".strip()
    state_payload = base64.b64encode(state_loop.encode()).decode()
    commands = (
        (
            "bluez-monitor",
            [ssh, args.host, f"sudo -n timeout {timeout_seconds}s busctl --system --json=short monitor org.bluez"],
        ),
        (
            "btmon",
            [
                ssh,
                args.host,
                f"sudo -n timeout {timeout_seconds}s stdbuf -oL -eL btmon -T -c never",
            ],
        ),
        (
            "system-journal",
            [
                ssh,
                args.host,
                f"sudo -n timeout {timeout_seconds}s journalctl -f -n 0 -o short-precise "
                "-u bluetooth.service -u bridge-btwatchdog.service -u bridge-storage-guard.service",
            ],
        ),
        (
            "user-journal",
            [
                ssh,
                args.host,
                f"sudo -n timeout {timeout_seconds}s journalctl -f -n 0 -o short-precise "
                "_SYSTEMD_USER_UNIT=bridge-output-remote.service",
            ],
        ),
        (
            "state",
            [
                ssh,
                args.host,
                f"printf %s {state_payload} | base64 -d | timeout {timeout_seconds}s sh",
            ],
        ),
        (
            "pipewire-monitor",
            [
                ssh,
                args.host,
                f"XDG_RUNTIME_DIR=/run/user/1000 timeout {timeout_seconds}s "
                "stdbuf -oL -eL pw-mon",
            ],
        ),
        (
            "android-logcat",
            [
                str(adb),
                "logcat",
                "-T",
                "1",
                "-v",
                "threadtime",
                "BridgeOutputController:V",
                "AudioRoutingService:V",
                "*:S",
            ],
        ),
    )

    captures: list[Capture] = []
    try:
        captures = [start_capture(name, command, directory) for name, command in commands]
        for capture in captures:
            manifest["captures"].append(  # type: ignore[union-attr]
                {"name": capture.name, "command": capture.command, "pid": capture.process.pid}
            )
        (directory / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(directory, flush=True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    except KeyboardInterrupt:
        manifest["interrupted"] = True
    finally:
        returncodes = {capture.name: stop_capture(capture) for capture in captures}
        manifest["returncodes"] = returncodes
        manifest["snapshots"].append(  # type: ignore[union-attr]
            run_snapshot([ssh, args.host, remote_snapshot], directory / "pi-after.log")
        )
        manifest["snapshots"].append(  # type: ignore[union-attr]
            run_snapshot([str(adb), *android_snapshot], directory / "android-after.log")
        )
        manifest["completed_utc"] = datetime.now(UTC).isoformat()
        manifest["files"] = {
            path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in sorted(directory.iterdir())
            if path.is_file() and path.name != "run.json"
        }
        (directory / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
