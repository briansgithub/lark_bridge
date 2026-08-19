#!/usr/bin/env python3
"""Prepare verified LarkBridge state before Bluetooth and audio start."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from lark_state import (
    StateError,
    append_ledger,
    atomic_bytes,
    atomic_json,
    fsync_dir,
    repair_ledger,
    restore_pairing,
    select_config,
    select_pairing_snapshot,
    validate_bluez_tree,
    validate_toml,
)


def _unescape_mount(value: str) -> str:
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return value


def persistent_mount_state(
    path: Path, mountinfo: Path = Path("/proc/self/mountinfo")
) -> tuple[bool, str]:
    expected = str(path.resolve())
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return False, f"cannot read mount table: {error}"
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if len(fields) < separator + 4 or _unescape_mount(fields[4]) != expected:
            continue
        filesystem = fields[separator + 1]
        source = _unescape_mount(fields[separator + 2])
        options = set(fields[5].split(",")) | set(fields[separator + 3].split(","))
        if filesystem != "ext4":
            return False, f"unexpected filesystem {filesystem} on {expected}"
        if "rw" not in options:
            return False, f"persistent state is not writable ({source})"
        return True, f"{source} mounted ext4,rw"
    return False, f"{expected} is not a distinct mount"


def is_exact_mount(path: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    expected = str(path.resolve())
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(
        len(fields := line.split()) > 4 and _unescape_mount(fields[4]) == expected
        for line in lines
    )


def detach_unusable_mount(path: Path, mountinfo: Path) -> str:
    if not is_exact_mount(path, mountinfo):
        return ""
    unit = "var-lib-larkbridge\\x2dpersist.mount"
    try:
        result = subprocess.run(
            ["systemctl", "stop", unit],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StateError(
            f"could not detach unusable persistent mount: {error}"
        ) from error
    if result.returncode or is_exact_mount(path, mountinfo):
        detail = (
            result.stderr.strip() or result.stdout.strip() or "mount remained active"
        )
        raise StateError(f"could not detach unusable persistent mount: {detail}")
    return "unusable persistent mount detached; RAM fallback activated"


def _materialize_config(source: Path, destination: Path) -> None:
    validate_toml(source)
    # The root-owned guard publishes a read-only copy for the unprivileged
    # supervisor. Persistent slots remain root-only.
    atomic_bytes(destination, source.read_bytes(), 0o644)
    validate_toml(destination)


def _ensure_seed(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if 32 <= size <= 4096:
        os.chmod(path, 0o600)
        return "existing"
    atomic_bytes(path, os.urandom(512), 0o600)
    return "regenerated"


def verify_or_reset_journal(root: Path) -> str:
    journal = root / "journal"
    files = sorted(
        path
        for path in journal.rglob("*")
        if path.is_file()
        and (path.name.endswith(".journal") or ".journal~" in path.name)
    )
    if not files:
        return ""
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--verify",
                "--quiet",
                *(f"--file={path}" for path in files),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StateError(f"could not verify persistent journal: {error}") from error
    if result.returncode == 0:
        return ""
    quarantine = root / "journal-corrupt"
    if quarantine.exists():
        if quarantine.is_dir():
            shutil.rmtree(quarantine)
        else:
            quarantine.unlink()
    os.replace(journal, quarantine)
    journal.mkdir(mode=0o2755)
    os.chmod(journal, 0o2755)
    fsync_dir(root)
    return "persistent journal failed verification and was quarantined"


def _recover_pairing(
    root: Path, live: Path, immutable: Path, reasons: list[str]
) -> tuple[str, str | None]:
    try:
        validate_bluez_tree(live)
        return "live-valid", None
    except StateError as error:
        reasons.append(str(error))

    try:
        snapshot, slot, failures = select_pairing_snapshot(root)
        reasons.extend(failures)
        if failures:
            atomic_bytes(root / "bluetooth/current", f"{slot}\n".encode("ascii"), 0o600)
        restore_pairing(snapshot, live)
        return "snapshot-restored", slot
    except StateError as error:
        reasons.append(str(error))

    validate_bluez_tree(immutable)
    restore_pairing(immutable, live)
    reasons.append("pairing state restored from immutable fallback")
    return "immutable-restored", None


def guard(arguments: argparse.Namespace) -> dict[str, Any]:
    root: Path = arguments.root
    root.mkdir(parents=True, exist_ok=True)
    if arguments.assume_persistent:
        persistent, mount_detail = True, "test override"
    else:
        persistent, mount_detail = persistent_mount_state(root, arguments.mountinfo)

    reasons: list[str] = []
    if not persistent:
        reasons.append(mount_detail)
        detached = detach_unusable_mount(root, arguments.mountinfo)
        if detached:
            reasons.append(detached)

    ledger_repair = repair_ledger(root)
    if ledger_repair:
        reasons.append(ledger_repair)

    config_slot: str | None = None
    config_source = "immutable"
    if persistent:
        try:
            source, config_slot, failures = select_config(root)
            reasons.extend(failures)
            config_source = f"slot-{config_slot}"
            if failures:
                atomic_bytes(
                    root / "config/current",
                    f"{config_slot}\n".encode("ascii"),
                    0o600,
                )
        except StateError as error:
            reasons.append(str(error))
            source = arguments.immutable_config
    else:
        source = arguments.immutable_config
    _materialize_config(source, arguments.active_config)

    journal = root / "journal"
    journal.mkdir(parents=True, exist_ok=True, mode=0o2755)
    os.chmod(journal, 0o2755)
    journal_repair = verify_or_reset_journal(root)
    if journal_repair:
        reasons.append(journal_repair)
    pairing_action, pairing_slot = _recover_pairing(
        root, arguments.live_bluez, arguments.immutable_bluez, reasons
    )
    seed_action = _ensure_seed(root / "random-seed")

    state = "READY" if persistent and not reasons else "DEGRADED"
    status: dict[str, Any] = {
        "config_slot": config_slot,
        "config_source": config_source,
        "mount": mount_detail,
        "pairing_action": pairing_action,
        "pairing_slot": pairing_slot,
        "persistent": persistent,
        "reasons": reasons,
        "schema": 1,
        "seed_action": seed_action,
        "state": state,
    }
    atomic_json(arguments.status, status, 0o644)
    try:
        append_ledger(
            root,
            {
                "config_source": config_source,
                "event": "storage-guard",
                "pairing_action": pairing_action,
                "state": state,
            },
        )
    except OSError as error:
        status["reasons"].append(f"recovery ledger unavailable: {error}")
        status["state"] = "DEGRADED"
        atomic_json(arguments.status, status, 0o644)
    return status


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--root", type=Path, default=Path("/var/lib/larkbridge-persist")
    )
    result.add_argument(
        "--active-config",
        type=Path,
        default=Path("/home/admin/rpi-lark-bridge/config/bridge.toml"),
    )
    result.add_argument(
        "--immutable-config",
        type=Path,
        default=Path("/usr/share/rpi-lark-bridge/recovery/bridge.toml"),
    )
    result.add_argument("--live-bluez", type=Path, default=Path("/var/lib/bluetooth"))
    result.add_argument(
        "--immutable-bluez",
        type=Path,
        default=Path("/usr/share/rpi-lark-bridge/recovery/bluetooth"),
    )
    result.add_argument(
        "--status", type=Path, default=Path("/run/larkbridge/storage-health.json")
    )
    result.add_argument("--mountinfo", type=Path, default=Path("/proc/self/mountinfo"))
    result.add_argument(
        "--assume-persistent", action="store_true", help=argparse.SUPPRESS
    )
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        status = guard(arguments)
    except (OSError, StateError) as error:
        failure = {
            "persistent": False,
            "reasons": [str(error)],
            "schema": 1,
            "state": "FAILED",
        }
        try:
            atomic_json(arguments.status, failure, 0o644)
        except OSError:
            pass
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
