#!/usr/bin/env python3
"""Write a hash manifest for one completed ESP-IDF build."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_ROOT = REPO_ROOT / "firmware"
ARTIFACT_NAMES = (
    "*.bin",
    "*.elf",
    "*.map",
    "bootloader/bootloader.bin",
    "partition_table/partition-table.bin",
    "flasher_args.json",
    "flash_args",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_output(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), *args], text=True, encoding="utf-8"
    ).strip()


def artifact_records(project: Path) -> list[dict[str, object]]:
    build = project / "build"
    paths: set[Path] = set()
    for pattern in ARTIFACT_NAMES:
        paths.update(path for path in build.glob(pattern) if path.is_file())
    return [
        {
            "path": path.relative_to(project).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted(paths)
    ]


def create_manifest(target: str) -> Path:
    lock_path = FIRMWARE_ROOT / "toolchain.lock.json"
    with lock_path.open(encoding="utf-8") as stream:
        lock = json.load(stream)
    if target not in ("s31", "output") or target not in lock:
        raise ValueError(f"unknown target {target!r}")
    project = FIRMWARE_ROOT / target
    artifacts = artifact_records(project)
    if not artifacts:
        raise FileNotFoundError(f"no flash artifacts found under {project / 'build'}")
    environment_commit = os.environ.get("LARKBRIDGE_SOURCE_COMMIT")
    environment_dirty = os.environ.get("LARKBRIDGE_SOURCE_DIRTY")
    if environment_commit:
        source_commit = environment_commit
        source_dirty = environment_dirty == "1"
        dirty_paths = ["host worktree was dirty"] if source_dirty else []
    else:
        status = git_output("status", "--porcelain=v1", "--untracked-files=all", "--", ".")
        source_commit = git_output("rev-parse", "HEAD")
        source_dirty = bool(status)
        dirty_paths = status.splitlines()
    component_lock = project / "dependencies.lock"
    manifest = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "dirty_paths": dirty_paths,
        "toolchain": lock[target],
        "toolchain_lock_sha256": sha256(lock_path),
        "component_lock_sha256": sha256(component_lock) if component_lock.is_file() else None,
        "artifacts": artifacts,
    }
    output = project / "build" / "larkbridge-build-manifest.json"
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("s31", "output"))
    args = parser.parse_args(argv)
    create_manifest(args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
