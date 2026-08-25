#!/usr/bin/env python3
"""Install one verified release into overlayroot's immutable lower filesystem.

Run this only through ``overlayroot-chroot``.  The explicit confirmation flag is a
second guard against accidentally replacing the disposable live overlay instead.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ARCHIVE_ROOT = PurePosixPath("rpi-lark-bridge")
MANIFEST_NAME = str(ARCHIVE_ROOT / "MANIFEST.sha256")
RELEASE_NAME = str(ARCHIVE_ROOT / "RELEASE.json")
HASH_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")
SYSTEM_UNITS = (
    "bridge-tuning.service",
    "bridge-btfw.service",
    "bridge-btwatchdog.service",
    "bridge-btwatchdog@.service",
    "bridge-boot-trial-rollback.service",
    "bridge-boot-trial-rollback.timer",
    "bridge-storage-guard.service",
    "bridge-pairing-seal.service",
    "bridge-pairing-seal.timer",
)
USER_UNITS = ("bridge-supervisor.service", "bridge-output-remote.service")
LIB_SCRIPTS = (
    "boot-transaction.sh",
    "boot-trial.sh",
    "onboard_bluetooth_config.py",
)
DISABLED_SYSTEM_UNITS = (
    "bridge-btfw.service",
    "bridge-btwatchdog.service",
    "bridge-btwatchdog@output.service",
)
PREIMAGE_SCHEMA = 1


class InstallError(RuntimeError):
    """Release identity, archive safety, or installation verification failed."""


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def safe_relative(name: str) -> str:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InstallError(f"unsafe archive path: {name!r}")
    return str(path)


def read_release(
    archive: Path, expected_archive_hash: str
) -> tuple[dict[str, Any], dict[str, str]]:
    payload_hash = digest(archive.read_bytes())
    if payload_hash != expected_archive_hash.lower():
        raise InstallError(
            f"archive checksum mismatch: expected {expected_archive_hash}, found {payload_hash}"
        )

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise InstallError("archive contains duplicate member names")
        for name in names:
            safe_relative(name)
        try:
            release = json.loads(bundle.read(RELEASE_NAME))
            manifest_text = bundle.read(MANIFEST_NAME).decode("utf-8")
        except (KeyError, UnicodeError, json.JSONDecodeError) as error:
            raise InstallError(f"release metadata is invalid: {error}") from error

        if not isinstance(release, dict) or release.get("archive_schema") != 1:
            raise InstallError("unsupported release metadata schema")
        if release.get("profile") != "pi3-usb-bt500-aux":
            raise InstallError("archive is not a Pi 3 USB-BT500+AUX release")
        if not re.fullmatch(r"[0-9a-f]{40}", str(release.get("commit", ""))):
            raise InstallError("release commit identity is invalid")

        manifest: dict[str, str] = {}
        for line in manifest_text.splitlines():
            match = HASH_LINE.fullmatch(line)
            if match is None:
                raise InstallError(f"invalid manifest line: {line!r}")
            relative = safe_relative(match.group(2))
            if relative in manifest:
                raise InstallError(f"duplicate manifest path: {relative}")
            manifest[relative] = match.group(1)
        if not manifest:
            raise InstallError("release manifest is empty")

        expected_members = {str(ARCHIVE_ROOT / relative) for relative in manifest} | {
            MANIFEST_NAME,
            RELEASE_NAME,
        }
        actual_files = {name for name in names if not name.endswith("/")}
        if actual_files != expected_members:
            extra = sorted(actual_files - expected_members)
            missing = sorted(expected_members - actual_files)
            raise InstallError(
                f"archive membership mismatch: extra={extra}, missing={missing}"
            )
        for relative, expected in manifest.items():
            actual = digest(bundle.read(str(ARCHIVE_ROOT / relative)))
            if actual != expected:
                raise InstallError(f"manifest checksum mismatch: {relative}")
    return release, manifest


def copy_managed(source: Path, target: Path, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.new-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def symlink(target: str, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.new-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)


def disable_unit(system_root: Path, unit: str) -> None:
    for wants in system_root.glob("etc/systemd/system/*.wants"):
        (wants / unit).unlink(missing_ok=True)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _relative_to_root(path: Path, system_root: Path) -> str:
    absolute_root = Path(os.path.abspath(system_root))
    absolute_path = Path(os.path.abspath(path))
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as error:
        raise InstallError(f"managed path is outside system root: {path}") from error
    if not relative.parts:
        raise InstallError("system root itself cannot be a managed path")
    return PurePosixPath(*relative.parts).as_posix()


def _path_from_manifest(system_root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or "\\" in relative:
        raise InstallError(f"invalid preimage path: {relative!r}")
    safe = safe_relative(relative)
    return system_root.joinpath(*PurePosixPath(safe).parts)


def managed_system_paths(release_root: Path, system_root: Path) -> list[Path]:
    """Return every system/user path provision_release or install may mutate."""

    paths: set[Path] = {
        *(system_root / "etc/systemd/system" / unit for unit in SYSTEM_UNITS),
        *(
            system_root / "home/admin/.config/systemd/user" / unit
            for unit in USER_UNITS
        ),
        system_root / "etc/bluetooth/main.conf.d/10-bridge.conf",
        *(
            system_root / "usr/local/lib/rpi-lark-bridge" / script
            for script in LIB_SCRIPTS
        ),
        system_root
        / "etc/systemd/system/multi-user.target.wants/bridge-tuning.service",
        system_root
        / "etc/systemd/system/multi-user.target.wants/bridge-btwatchdog@call.service",
        system_root
        / "home/admin/.config/systemd/user/default.target.wants/bridge-supervisor.service",
        system_root
        / "home/admin/.config/systemd/user/default.target.wants/bridge-output-remote.service",
        system_root
        / "etc/systemd/system/NetworkManager.service.d/10-larkbridge-netplan-startup.conf",
        system_root / "usr/local/lib/rpi-lark-bridge/boot-path/netplan",
        system_root / "etc/larkbridge/DEPLOYED.json",
    }
    user_root = system_root / "home/admin/.config"
    for source in sorted((release_root / "pi/pipewire/pipewire.conf.d").glob("*")):
        if source.is_file():
            paths.add(user_root / "pipewire/pipewire.conf.d" / source.name)
    for source in sorted(
        (release_root / "pi/wireplumber/wireplumber.conf.d").glob("*")
    ):
        if source.is_file():
            paths.add(user_root / "wireplumber/wireplumber.conf.d" / source.name)
    for wants in system_root.glob("etc/systemd/system/*.wants"):
        for unit in DISABLED_SYSTEM_UNITS:
            candidate = wants / unit
            if _lexists(candidate):
                paths.add(candidate)
    return sorted(paths, key=lambda path: os.fspath(path))


def snapshot_preimages(
    paths: list[Path], system_root: Path, preimage_root: Path
) -> Path:
    """Persist restorable preimages and a checksummed manifest before mutation."""

    if _lexists(preimage_root):
        raise InstallError(f"preimage path already exists: {preimage_root}")
    preimage_root.mkdir(parents=True, mode=0o700)
    payload_root = preimage_root / "files"
    entries: list[dict[str, Any]] = []
    created_directories: set[str] = set()
    unique_paths = sorted(set(paths), key=lambda path: os.fspath(path))
    absolute_root = Path(os.path.abspath(system_root))

    for path in unique_paths:
        relative = _relative_to_root(path, system_root)
        parent = Path(os.path.abspath(path)).parent
        while parent != absolute_root:
            if not _lexists(parent):
                created_directories.add(_relative_to_root(parent, system_root))
            parent = parent.parent

        try:
            metadata = path.lstat()
        except FileNotFoundError:
            entries.append({"path": relative, "kind": "missing"})
            continue
        common = {
            "path": relative,
            "mode": stat.S_IMODE(metadata.st_mode),
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
        }
        if stat.S_ISLNK(metadata.st_mode):
            entries.append({**common, "kind": "symlink", "target": os.readlink(path)})
        elif stat.S_ISREG(metadata.st_mode):
            payload_root.mkdir(exist_ok=True)
            payload_name = f"{len(entries):04d}.bin"
            payload = payload_root / payload_name
            shutil.copy2(path, payload)
            entries.append(
                {
                    **common,
                    "kind": "file",
                    "payload": payload_name,
                    "sha256": digest(payload.read_bytes()),
                }
            )
        else:
            raise InstallError(f"unsupported managed path type: {path}")

    manifest = {
        "schema": PREIMAGE_SCHEMA,
        "entries": entries,
        "created_directories": sorted(
            created_directories,
            key=lambda value: (len(PurePosixPath(value).parts), value),
        ),
    }
    manifest_path = preimage_root / "MANIFEST.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.new-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, manifest_path)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest_path


def _remove_managed_path(path: Path) -> None:
    if not _lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        raise InstallError(f"refusing to remove directory at managed file path: {path}")
    path.unlink()


def _restore_owner(path: Path, entry: dict[str, Any], *, symlink_path: bool) -> None:
    change_owner = getattr(os, "chown", None)
    get_effective_uid = getattr(os, "geteuid", lambda: 1)
    if change_owner is None or get_effective_uid() != 0:
        return
    change_owner(
        path,
        int(entry["uid"]),
        int(entry["gid"]),
        follow_symlinks=not symlink_path,
    )


def restore_preimages(system_root: Path, preimage_root: Path) -> None:
    """Restore a snapshot, verifying file payloads before changing managed paths."""

    try:
        manifest = json.loads((preimage_root / "MANIFEST.json").read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InstallError(f"preimage manifest is invalid: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != PREIMAGE_SCHEMA:
        raise InstallError("unsupported preimage manifest schema")
    entries = manifest.get("entries")
    directories = manifest.get("created_directories")
    if not isinstance(entries, list) or not isinstance(directories, list):
        raise InstallError("preimage manifest entries are invalid")

    validated: list[tuple[Path, dict[str, Any], Path | None]] = []
    seen: set[Path] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise InstallError("preimage entry is invalid")
        path = _path_from_manifest(system_root, raw_entry.get("path"))
        if path in seen:
            raise InstallError(f"duplicate preimage path: {path}")
        seen.add(path)
        kind = raw_entry.get("kind")
        payload: Path | None = None
        if kind == "file":
            for field in ("mode", "uid", "gid"):
                if not isinstance(raw_entry.get(field), int):
                    raise InstallError(f"invalid {field} in preimage for {path}")
            payload_name = raw_entry.get("payload")
            if (
                not isinstance(payload_name, str)
                or PurePosixPath(payload_name).name != payload_name
            ):
                raise InstallError(f"invalid preimage payload: {payload_name!r}")
            payload = preimage_root / "files" / payload_name
            try:
                actual = digest(payload.read_bytes())
            except OSError as error:
                raise InstallError(
                    f"preimage payload is unreadable: {payload}"
                ) from error
            if actual != raw_entry.get("sha256"):
                raise InstallError(f"preimage checksum mismatch: {path}")
        elif kind == "symlink":
            for field in ("uid", "gid"):
                if not isinstance(raw_entry.get(field), int):
                    raise InstallError(f"invalid {field} in preimage for {path}")
            if not isinstance(raw_entry.get("target"), str):
                raise InstallError(f"invalid symlink preimage: {path}")
        elif kind != "missing":
            raise InstallError(f"invalid preimage kind for {path}: {kind!r}")
        validated.append((path, raw_entry, payload))

    restored_directories: list[Path] = []
    seen_directories: set[Path] = set()
    for raw_directory in directories:
        directory = _path_from_manifest(system_root, raw_directory)
        if directory in seen_directories:
            raise InstallError(f"duplicate preimage directory: {directory}")
        seen_directories.add(directory)
        restored_directories.append(directory)

    for path, entry, payload in validated:
        kind = entry["kind"]
        if kind == "missing":
            _remove_managed_path(path)
            continue
        _remove_managed_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.restore-{os.getpid()}")
        temporary.unlink(missing_ok=True)
        try:
            if kind == "file":
                assert payload is not None
                shutil.copy2(payload, temporary)
                os.chmod(temporary, int(entry["mode"]))
                _restore_owner(temporary, entry, symlink_path=False)
            else:
                temporary.symlink_to(str(entry["target"]))
                _restore_owner(temporary, entry, symlink_path=True)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    for directory in sorted(
        restored_directories, key=lambda path: len(path.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError as error:
            # A newly-created parent may now contain unrelated data; never remove it.
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise


def provision_release(
    release_root: Path, system_root: Path, *, netplan_fastpath: bool
) -> None:
    for unit in SYSTEM_UNITS:
        copy_managed(
            release_root / "pi" / "systemd" / "system" / unit,
            system_root / "etc" / "systemd" / "system" / unit,
            0o644,
        )

    user_root = system_root / "home" / "admin" / ".config"
    for unit in USER_UNITS:
        copy_managed(
            release_root / "pi" / "systemd" / "user" / unit,
            user_root / "systemd" / "user" / unit,
            0o644,
        )
    for source in (release_root / "pi" / "pipewire" / "pipewire.conf.d").glob("*"):
        if source.is_file():
            copy_managed(
                source, user_root / "pipewire" / "pipewire.conf.d" / source.name, 0o644
            )
    for source in (release_root / "pi" / "wireplumber" / "wireplumber.conf.d").glob(
        "*"
    ):
        if source.is_file():
            copy_managed(
                source,
                user_root / "wireplumber" / "wireplumber.conf.d" / source.name,
                0o644,
            )
    copy_managed(
        release_root / "pi" / "bluez" / "main.conf.d" / "10-bridge.conf",
        system_root / "etc" / "bluetooth" / "main.conf.d" / "10-bridge.conf",
        0o644,
    )
    for script in LIB_SCRIPTS:
        copy_managed(
            release_root / "pi" / "scripts" / script,
            system_root / "usr" / "local" / "lib" / "rpi-lark-bridge" / script,
            0o755,
        )

    for unit in DISABLED_SYSTEM_UNITS:
        disable_unit(system_root, unit)
    symlink(
        "../bridge-tuning.service",
        system_root
        / "etc/systemd/system/multi-user.target.wants/bridge-tuning.service",
    )
    symlink(
        "../bridge-btwatchdog@.service",
        system_root
        / "etc/systemd/system/multi-user.target.wants/bridge-btwatchdog@call.service",
    )
    symlink(
        "../bridge-supervisor.service",
        user_root / "systemd/user/default.target.wants/bridge-supervisor.service",
    )
    (
        user_root / "systemd/user/default.target.wants/bridge-output-remote.service"
    ).unlink(missing_ok=True)

    fastpath = (
        system_root
        / "etc/systemd/system/NetworkManager.service.d/10-larkbridge-netplan-startup.conf"
    )
    fastpath_script = system_root / "usr/local/lib/rpi-lark-bridge/boot-path/netplan"
    if netplan_fastpath:
        copy_managed(
            release_root / "pi" / "scripts" / "netplan-startup-fastpath",
            fastpath_script,
            0o755,
        )
        copy_managed(
            release_root
            / "pi"
            / "systemd"
            / "system"
            / "NetworkManager-10-larkbridge-netplan-startup.conf",
            fastpath,
            0o644,
        )
    else:
        fastpath.unlink(missing_ok=True)
        fastpath_script.unlink(missing_ok=True)


def write_deployed(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.new-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_release_tree(path: Path) -> None:
    if not _lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def install(
    archive: Path,
    archive_hash: str,
    *,
    target: Path,
    system_root: Path,
    netplan_fastpath: bool,
) -> dict[str, Any]:
    release, manifest = read_release(archive, archive_hash)
    release_id = str(release["commit"])[:12]
    staging = target.parent / f".{target.name}.new-{release_id}-{os.getpid()}"
    retired = target.parent / f".{target.name}.retired-{release_id}-{os.getpid()}"
    if staging.exists() or retired.exists():
        raise InstallError("release staging path already exists")

    try:
        staging.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            for relative, expected in manifest.items():
                destination = staging / Path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = bundle.read(str(ARCHIVE_ROOT / relative))
                if digest(payload) != expected:
                    raise InstallError(
                        f"checksum changed during extraction: {relative}"
                    )
                destination.write_bytes(payload)
                mode = (
                    bundle.getinfo(str(ARCHIVE_ROOT / relative)).external_attr >> 16
                ) & 0o777
                os.chmod(destination, mode or 0o644)

        old_config = target / "config" / "bridge.toml"
        if old_config.is_file():
            destination = staging / "config" / "bridge.toml"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_config, destination)

        backup_root = system_root / "var/lib/rpi-lark-bridge/releases" / release_id
        transaction_root = backup_root / f"install-{time.time_ns()}-{os.getpid()}"
        transaction_root.mkdir(parents=True, mode=0o700)
        preimage_root = transaction_root / "system-preimage"
        snapshot_preimages(
            managed_system_paths(staging, system_root), system_root, preimage_root
        )

        target_retired = False
        target_installed = False
        try:
            if target.exists():
                with tarfile.open(
                    transaction_root / "preimage.tar.gz", "w:gz"
                ) as backup:
                    backup.add(target, arcname=target.name)
                os.replace(target, retired)
                target_retired = True
            os.replace(staging, target)
            target_installed = True
            provision_release(target, system_root, netplan_fastpath=netplan_fastpath)
            deployed = system_root / "etc/larkbridge/DEPLOYED.json"
            document = dict(release)
            document["archive_sha256"] = archive_hash.lower()
            document["manifest_entries"] = len(manifest)
            write_deployed(deployed, document)
            shutil.copy2(deployed, transaction_root / "DEPLOYED.json")
        except BaseException as error:
            rollback_errors: list[str] = []
            try:
                restore_preimages(system_root, preimage_root)
            except Exception as rollback_error:  # noqa: BLE001 - continue rollback
                rollback_errors.append(f"system preimages: {rollback_error}")
            try:
                if target_installed:
                    _remove_release_tree(target)
                if target_retired and _lexists(retired):
                    os.replace(retired, target)
            except Exception as rollback_error:  # noqa: BLE001 - continue rollback
                rollback_errors.append(f"release tree: {rollback_error}")
            if rollback_errors:
                details = "; ".join(rollback_errors)
                raise InstallError(
                    f"install failed ({error}); rollback also failed: {details}"
                ) from error
            raise
        if _lexists(retired):
            _remove_release_tree(retired)
        return document
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument(
        "--target", type=Path, default=Path("/home/admin/rpi-lark-bridge")
    )
    parser.add_argument("--netplan-fastpath", action="store_true")
    parser.add_argument("--confirm-lower-root", action="store_true")
    arguments = parser.parse_args()
    get_effective_uid = getattr(os, "geteuid", lambda: 1)
    if get_effective_uid() != 0:
        parser.error("must run as root")
    if not arguments.confirm_lower_root:
        parser.error("refusing without --confirm-lower-root through overlayroot-chroot")
    try:
        result = install(
            arguments.archive,
            arguments.archive_sha256,
            target=arguments.target,
            system_root=Path("/"),
            netplan_fastpath=arguments.netplan_fastpath,
        )
    except (InstallError, OSError, zipfile.BadZipFile) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
