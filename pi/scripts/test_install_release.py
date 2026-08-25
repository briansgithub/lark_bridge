from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from install_release import InstallError, read_release


def make_archive(path: Path, *, corrupt: bool = False, unsafe: bool = False) -> str:
    payload = b"runtime\n"
    relative = "../escape" if unsafe else "runtime.py"
    manifest_hash = hashlib.sha256(payload).hexdigest()
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(f"rpi-lark-bridge/{relative}", payload)
        bundle.writestr(
            "rpi-lark-bridge/MANIFEST.sha256",
            f"{manifest_hash if not corrupt else '0' * 64}  {relative}\n",
        )
        bundle.writestr(
            "rpi-lark-bridge/RELEASE.json",
            json.dumps(
                {
                    "archive_schema": 1,
                    "commit": "a" * 40,
                    "profile": "pi3-usb-bt500-aux",
                }
            ),
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_valid_release_is_verified(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_archive(archive)

    release, manifest = read_release(archive, checksum)

    assert release["commit"] == "a" * 40
    assert manifest == {"runtime.py": hashlib.sha256(b"runtime\n").hexdigest()}


def test_manifest_corruption_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_archive(archive, corrupt=True)

    with pytest.raises(InstallError, match="manifest checksum"):
        read_release(archive, checksum)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_archive(archive, unsafe=True)

    with pytest.raises(InstallError, match="unsafe archive path"):
        read_release(archive, checksum)
