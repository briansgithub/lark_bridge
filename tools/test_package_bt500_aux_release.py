from __future__ import annotations

import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from package_bt500_aux_release import ReleaseError, build_release


def command(directory: Path, *arguments: str) -> None:
    subprocess.run(arguments, cwd=directory, check=True, capture_output=True)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    command(tmp_path, "git", "init", "-q")
    command(tmp_path, "git", "config", "user.email", "release-test@example.invalid")
    command(tmp_path, "git", "config", "user.name", "Release Test")
    command(tmp_path, "git", "config", "core.autocrlf", "false")
    (tmp_path / "runtime.py").write_bytes(b"print('ready')\n")
    command(tmp_path, "git", "add", "runtime.py")
    command(tmp_path, "git", "commit", "-qm", "fixture")
    monkeypatch.setattr("package_bt500_aux_release.BASE_COMMIT", "HEAD")
    return tmp_path


def test_release_is_commit_exact(repository: Path, tmp_path: Path) -> None:
    archive, sidecar = build_release(
        repository,
        tmp_path / "out",
        timestamp=datetime(2026, 8, 25, tzinfo=UTC),
    )

    assert archive.is_file()
    assert sidecar.is_file()
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.read("rpi-lark-bridge/runtime.py") == b"print('ready')\n"
        manifest = bundle.read("rpi-lark-bridge/MANIFEST.sha256").decode()
        assert "  runtime.py\n" in manifest
        assert b'"profile": "pi3-usb-bt500-aux"' in bundle.read(
            "rpi-lark-bridge/RELEASE.json"
        )


def test_release_refuses_dirty_tree(repository: Path, tmp_path: Path) -> None:
    (repository / "runtime.py").write_bytes(b"changed\n")

    with pytest.raises(ReleaseError, match="not clean"):
        build_release(repository, tmp_path / "out")
