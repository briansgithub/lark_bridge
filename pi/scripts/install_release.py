#!/usr/bin/env python3
"""Install one verified release into overlayroot's immutable lower filesystem.

Run this only through ``overlayroot-chroot``.  The explicit confirmation flag is a
second guard against accidentally replacing the disposable live overlay instead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

ARCHIVE_ROOT = PurePosixPath("rpi-lark-bridge")
MANIFEST_NAME = str(ARCHIVE_ROOT / "MANIFEST.sha256")
RELEASE_NAME = str(ARCHIVE_ROOT / "RELEASE.json")
HASH_LINE = re.compile(r"^([0-9a-f]{64})  (.+)$")


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
    shutil.copyfile(source, temporary)
    os.chmod(temporary, mode)
    os.replace(temporary, target)


def symlink(target: str, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.new-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def disable_unit(system_root: Path, unit: str) -> None:
    for wants in system_root.glob("etc/systemd/system/*.wants"):
        (wants / unit).unlink(missing_ok=True)


def provision_release(
    release_root: Path, system_root: Path, *, netplan_fastpath: bool
) -> None:
    system_units = (
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
    for unit in system_units:
        copy_managed(
            release_root / "pi" / "systemd" / "system" / unit,
            system_root / "etc" / "systemd" / "system" / unit,
            0o644,
        )

    user_root = system_root / "home" / "admin" / ".config"
    for unit in ("bridge-supervisor.service", "bridge-output-remote.service"):
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
    for script in (
        "boot-transaction.sh",
        "boot-trial.sh",
        "onboard_bluetooth_config.py",
    ):
        copy_managed(
            release_root / "pi" / "scripts" / script,
            system_root / "usr" / "local" / "lib" / "rpi-lark-bridge" / script,
            0o755,
        )

    disable_unit(system_root, "bridge-btfw.service")
    disable_unit(system_root, "bridge-btwatchdog.service")
    disable_unit(system_root, "bridge-btwatchdog@output.service")
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
        backup_root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with tarfile.open(backup_root / "preimage.tar.gz", "w:gz") as backup:
                backup.add(target, arcname=target.name)
            os.replace(target, retired)
        os.replace(staging, target)
        try:
            provision_release(target, system_root, netplan_fastpath=netplan_fastpath)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if retired.exists():
                os.replace(retired, target)
            raise
        if retired.exists():
            shutil.rmtree(retired)

        deployed = system_root / "etc/larkbridge/DEPLOYED.json"
        deployed.parent.mkdir(parents=True, exist_ok=True)
        document = dict(release)
        document["archive_sha256"] = archive_hash.lower()
        document["manifest_entries"] = len(manifest)
        copy = deployed.with_name(f".{deployed.name}.new-{os.getpid()}")
        copy.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(copy, 0o644)
        os.replace(copy, deployed)
        shutil.copy2(deployed, backup_root / "DEPLOYED.json")
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
