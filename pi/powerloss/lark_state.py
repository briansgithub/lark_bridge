#!/usr/bin/env python3
"""Atomic, checksummed persistent-state transactions for LarkBridge."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import tomllib

SCHEMA = 1
CONFIG_SLOTS = ("a", "b")
PAIRING_SLOTS = ("a", "b")


class StateError(RuntimeError):
    """Persistent state is absent, malformed, or fails verification."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_dir(path: Path) -> None:
    # Windows does not permit opening a directory this way. The deployed appliance
    # is Linux, where syncing the containing directory is part of every commit.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        fsync_dir(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    atomic_bytes(path, payload, mode)


def validate_toml(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise StateError(f"invalid TOML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise StateError(f"configuration is not a TOML table: {path}")


def _read_pointer(path: Path, allowed: tuple[str, ...]) -> str | None:
    try:
        value = path.read_text(encoding="ascii").strip()
    except OSError:
        return None
    return value if value in allowed else None


def _slot_order(pointer: str | None, slots: tuple[str, ...]) -> tuple[str, ...]:
    if pointer in slots:
        return (pointer,) + tuple(slot for slot in slots if slot != pointer)
    return slots


def _config_paths(root: Path, slot: str) -> tuple[Path, Path]:
    directory = root / "config" / f"slot-{slot}"
    return directory / "bridge.toml", directory / "manifest.json"


def validate_config_slot(root: Path, slot: str) -> Path:
    config, manifest_path = _config_paths(root, slot)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(
            f"configuration slot {slot} manifest is invalid: {error}"
        ) from error
    if manifest.get("schema") != SCHEMA or manifest.get("slot") != slot:
        raise StateError(f"configuration slot {slot} manifest identity is invalid")
    try:
        size = config.stat().st_size
    except OSError as error:
        raise StateError(f"configuration slot {slot} is absent: {error}") from error
    if size != manifest.get("size") or sha256_file(config) != manifest.get("sha256"):
        raise StateError(f"configuration slot {slot} checksum mismatch")
    validate_toml(config)
    return config


def select_config(root: Path) -> tuple[Path, str, list[str]]:
    pointer = _read_pointer(root / "config" / "current", CONFIG_SLOTS)
    failures: list[str] = []
    for slot in _slot_order(pointer, CONFIG_SLOTS):
        try:
            return validate_config_slot(root, slot), slot, failures
        except StateError as error:
            failures.append(str(error))
    raise StateError("; ".join(failures) or "no configuration slots exist")


def write_config(root: Path, source: Path) -> str:
    validate_toml(source)
    payload = source.read_bytes()
    pointer_path = root / "config" / "current"
    current = _read_pointer(pointer_path, CONFIG_SLOTS)
    target = "b" if current == "a" else "a"
    config, manifest_path = _config_paths(root, target)
    atomic_bytes(config, payload, 0o600)
    manifest = {
        "committed_unix_ns": time.time_ns(),
        "schema": SCHEMA,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "slot": target,
    }
    atomic_json(manifest_path, manifest)
    validate_config_slot(root, target)
    atomic_bytes(pointer_path, f"{target}\n".encode("ascii"), 0o600)
    return target


def _pairing_entries(source: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not (
            stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)
        ):
            raise StateError(f"unsupported pairing-state entry: {relative}")
        entry: dict[str, Any] = {
            "mode": stat.S_IMODE(status.st_mode),
            "path": relative,
            "type": "directory" if path.is_dir() else "file",
        }
        if path.is_file():
            entry["sha256"] = sha256_file(path)
            entry["size"] = status.st_size
        entries.append(entry)
    return entries


def validate_bluez_tree(source: Path, *, allow_empty: bool = True) -> None:
    if not source.is_dir():
        raise StateError(f"BlueZ state directory is absent: {source}")
    entries = _pairing_entries(source)
    regular_files = [entry for entry in entries if entry["type"] == "file"]
    if not allow_empty and not regular_files:
        raise StateError(f"BlueZ state directory has no files: {source}")
    for entry in regular_files:
        path = source / entry["path"]
        if path.name not in {"settings", "info", "cache"}:
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        try:
            with path.open("r", encoding="utf-8") as handle:
                parser.read_file(handle)
        except (OSError, UnicodeError, configparser.Error) as error:
            raise StateError(
                f"invalid BlueZ state file {entry['path']}: {error}"
            ) from error


def _copy_tree_verified(source: Path, destination: Path) -> list[dict[str, Any]]:
    validate_bluez_tree(source)
    entries = _pairing_entries(source)
    destination.mkdir(mode=0o700, parents=True)
    for entry in entries:
        target = destination / entry["path"]
        if entry["type"] == "directory":
            target.mkdir(mode=entry["mode"], parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (source / entry["path"]).open("rb") as input_handle:
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    entry["mode"],
                )
                with os.fdopen(descriptor, "wb") as output_handle:
                    shutil.copyfileobj(input_handle, output_handle)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
    for directory, _, _ in os.walk(destination, topdown=False):
        fsync_dir(Path(directory))
    return entries


def _pairing_slot(root: Path, slot: str) -> Path:
    return root / "bluetooth" / f"snapshot-{slot}"


def validate_pairing_slot(root: Path, slot: str) -> Path:
    directory = _pairing_slot(root, slot)
    manifest_path = directory / ".larkbridge-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"pairing slot {slot} manifest is invalid: {error}") from error
    if manifest.get("schema") != SCHEMA or manifest.get("slot") != slot:
        raise StateError(f"pairing slot {slot} manifest identity is invalid")
    actual = _pairing_entries(directory)
    actual = [entry for entry in actual if entry["path"] != manifest_path.name]
    if actual != manifest.get("entries"):
        raise StateError(f"pairing slot {slot} checksum manifest mismatch")
    validate_bluez_tree(directory)
    return directory


def select_pairing_snapshot(root: Path) -> tuple[Path, str, list[str]]:
    pointer = _read_pointer(root / "bluetooth" / "current", PAIRING_SLOTS)
    failures: list[str] = []
    for slot in _slot_order(pointer, PAIRING_SLOTS):
        try:
            return validate_pairing_slot(root, slot), slot, failures
        except StateError as error:
            failures.append(str(error))
    raise StateError("; ".join(failures) or "no pairing snapshots exist")


def seal_pairing(root: Path, source: Path) -> str:
    validate_bluez_tree(source)
    source_entries = _pairing_entries(source)
    pointer_path = root / "bluetooth" / "current"
    current = _read_pointer(pointer_path, PAIRING_SLOTS)
    if current:
        try:
            current_snapshot = validate_pairing_slot(root, current)
            current_entries = _pairing_entries(current_snapshot)
            current_entries = [
                entry
                for entry in current_entries
                if entry["path"] != ".larkbridge-manifest.json"
            ]
            if current_entries == source_entries:
                return current
        except StateError:
            pass
    target = "b" if current == "a" else "a"
    parent = root / "bluetooth"
    parent.mkdir(parents=True, exist_ok=True)
    for orphan in parent.glob(".snapshot-*.new-*"):
        if orphan.is_dir():
            shutil.rmtree(orphan)
    temporary = parent / f".snapshot-{target}.new-{uuid.uuid4().hex}"
    destination = _pairing_slot(root, target)
    try:
        entries = _copy_tree_verified(source, temporary)
        if entries != source_entries or _pairing_entries(source) != source_entries:
            raise StateError("BlueZ state changed while the snapshot was being sealed")
        atomic_json(
            temporary / ".larkbridge-manifest.json",
            {
                "committed_unix_ns": time.time_ns(),
                "entries": entries,
                "schema": SCHEMA,
                "slot": target,
            },
        )
        fsync_dir(temporary)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        fsync_dir(parent)
        validate_pairing_slot(root, target)
        atomic_bytes(pointer_path, f"{target}\n".encode("ascii"), 0o600)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return target


def restore_pairing(snapshot: Path, live: Path) -> None:
    validate_bluez_tree(snapshot)
    if live.is_symlink():
        live = live.resolve(strict=False)
    parent = live.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f".{live.name}.new-{uuid.uuid4().hex}"
    retired = parent / f".{live.name}.retired-{uuid.uuid4().hex}"
    _copy_tree_verified(snapshot, temporary)
    if (temporary / ".larkbridge-manifest.json").exists():
        (temporary / ".larkbridge-manifest.json").unlink()
    fsync_dir(temporary)
    if live.exists():
        os.replace(live, retired)
    os.replace(temporary, live)
    fsync_dir(parent)
    if retired.exists():
        shutil.rmtree(retired)
    for orphan in (
        *parent.glob(f".{live.name}.new-*"),
        *parent.glob(f".{live.name}.retired-*"),
    ):
        if orphan.is_dir():
            shutil.rmtree(orphan)
    fsync_dir(parent)


def append_ledger(root: Path, event: dict[str, Any]) -> None:
    ledger = root / "recovery" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = ledger.stat().st_size
    except OSError:
        size = 0
    if size >= 1024 * 1024:
        previous = ledger.with_suffix(".jsonl.1")
        previous.unlink(missing_ok=True)
        os.replace(ledger, previous)
        fsync_dir(ledger.parent)
    record = dict(event)
    record.setdefault("schema", SCHEMA)
    record.setdefault("unix_ns", time.time_ns())
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    descriptor = os.open(ledger, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_dir(ledger.parent)


def repair_ledger(root: Path) -> str:
    ledger = root / "recovery" / "ledger.jsonl"
    try:
        payload = ledger.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as error:
        raise StateError(f"cannot read recovery ledger: {error}") from error
    offset = 0
    for line in payload.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        try:
            document = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            break
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            break
        offset += len(line)
    if offset == len(payload):
        return ""
    atomic_bytes(ledger, payload[:offset], 0o600)
    return f"discarded {len(payload) - offset} invalid trailing ledger bytes"


def _command_write_config(arguments: argparse.Namespace) -> None:
    slot = write_config(arguments.root, arguments.source)
    append_ledger(arguments.root, {"event": "config-committed", "slot": slot})
    print(slot)


def _command_seal_pairing(arguments: argparse.Namespace) -> None:
    slot = seal_pairing(arguments.root, arguments.source)
    append_ledger(arguments.root, {"event": "pairing-sealed", "slot": slot})
    print(slot)


def _command_status(arguments: argparse.Namespace) -> None:
    config, config_slot, config_failures = select_config(arguments.root)
    pairing, pairing_slot, pairing_failures = select_pairing_snapshot(arguments.root)
    print(
        json.dumps(
            {
                "config": str(config),
                "config_failures": config_failures,
                "config_slot": config_slot,
                "pairing": str(pairing),
                "pairing_failures": pairing_failures,
                "pairing_slot": pairing_slot,
                "schema": SCHEMA,
            },
            sort_keys=True,
        )
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--root", type=Path, default=Path("/var/lib/larkbridge-persist")
    )
    commands = result.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config-write")
    config.add_argument("--source", type=Path, required=True)
    config.set_defaults(function=_command_write_config)
    pairing = commands.add_parser("pairing-seal")
    pairing.add_argument("--source", type=Path, default=Path("/var/lib/bluetooth"))
    pairing.set_defaults(function=_command_seal_pairing)
    status = commands.add_parser("status")
    status.set_defaults(function=_command_status)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        arguments.function(arguments)
    except StateError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
