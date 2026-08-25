#!/usr/bin/env python3
"""Build a checksummed, commit-exact BT500+AUX release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

BASE_COMMIT = "4dba442a95166a60630a7f68e0248f8cc4f488fa"
ARCHIVE_ROOT = PurePosixPath("rpi-lark-bridge")


class ReleaseError(RuntimeError):
    """A release cannot be proven to match one clean Git commit."""


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ReleaseError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_release(
    repo: Path,
    output_directory: Path,
    *,
    timestamp: datetime | None = None,
) -> tuple[Path, Path]:
    repo = repo.resolve()
    dirty = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise ReleaseError("working tree is not clean; commit the exact release first")

    commit = git(repo, "rev-parse", "HEAD")
    branch = git(repo, "branch", "--show-current") or "detached"
    if git(repo, "merge-base", "--is-ancestor", BASE_COMMIT, commit) != "":
        # merge-base --is-ancestor has no stdout; success is all that matters.
        raise AssertionError("unreachable")

    created = timestamp or datetime.now(UTC)
    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    output_directory.mkdir(parents=True, exist_ok=True)
    archive = output_directory / f"LarkBridge-bt500-aux-{commit[:12]}-{stamp}.zip"

    with tempfile.TemporaryDirectory(prefix="larkbridge-release-") as temporary:
        tar_path = Path(temporary) / "release.tar"
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "archive",
                "--format=tar",
                "-o",
                str(tar_path),
                commit,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise ReleaseError(result.stderr.strip() or "git archive failed")

        files: list[tuple[str, bytes, int]] = []
        with tarfile.open(tar_path, "r") as source:
            for member in source.getmembers():
                if not member.isfile():
                    continue
                handle = source.extractfile(member)
                if handle is None:
                    raise ReleaseError(f"cannot read archived member {member.name}")
                files.append((member.name, handle.read(), member.mode))

    files.sort(key=lambda item: item[0])
    manifest = "".join(f"{sha256(payload)}  {name}\n" for name, payload, _ in files)
    metadata = {
        "archive_schema": 1,
        "base_commit": BASE_COMMIT,
        "branch": branch,
        "commit": commit,
        "created_utc": created.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "profile": "pi3-usb-bt500-aux",
    }

    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for name, payload, mode in files:
            info = zipfile.ZipInfo(str(ARCHIVE_ROOT / name), created.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (mode & 0xFFFF) << 16
            target.writestr(info, payload)
        target.writestr(
            str(ARCHIVE_ROOT / "MANIFEST.sha256"),
            manifest.encode("utf-8"),
        )
        target.writestr(
            str(ARCHIVE_ROOT / "RELEASE.json"),
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    sidecar.write_text(
        f"{sha256(archive.read_bytes())}  {archive.name}\n", encoding="ascii"
    )
    return archive, sidecar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output-directory", type=Path, default=Path("archive"))
    arguments = parser.parse_args()
    try:
        archive, sidecar = build_release(arguments.repo, arguments.output_directory)
    except ReleaseError as error:
        parser.error(str(error))
    print(archive)
    print(sidecar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
