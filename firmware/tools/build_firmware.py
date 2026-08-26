#!/usr/bin/env python3
"""Reproducible host and ESP-IDF builds for the two-board bridge."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_ROOT = REPO_ROOT / "firmware"
LOCK_PATH = FIRMWARE_ROOT / "toolchain.lock.json"


def load_lock(path: Path = LOCK_PATH) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        lock = json.load(stream)
    if lock.get("schema") != 1:
        raise ValueError(f"unsupported toolchain lock schema in {path}")
    return lock


def target_config(lock: dict[str, object], target: str) -> dict[str, object]:
    value = lock.get(target)
    if not isinstance(value, dict):
        raise ValueError(f"toolchain lock has no {target!r} entry")
    return value


def source_state() -> tuple[str, bool]:
    commit = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True, encoding="utf-8"
    ).strip()
    status = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True,
        encoding="utf-8",
    ).strip()
    return commit, bool(status)


def docker_command(
    target: str,
    lock: dict[str, object],
    docker: str = "docker",
    pull: bool = True,
) -> list[str]:
    config = target_config(lock, target)
    tag = str(config["esp_idf_tag"])
    commit = str(config["esp_idf_commit"])
    chip = str(config["target"])
    project = FIRMWARE_ROOT / target
    if not project.is_dir():
        raise FileNotFoundError(f"missing firmware project: {project}")
    source_commit, source_dirty = source_state()
    project_version = source_commit[:12] + ("-dirty" if source_dirty else "")

    idf_target = (
        f'idf.py -D PROJECT_VER="$PROJECT_VER" --preview set-target {chip}'
        if config.get("preview_target")
        else f'idf.py -D PROJECT_VER="$PROJECT_VER" set-target {chip}'
    )
    shell = (
        "set -euo pipefail; "
        'git config --global --add safe.directory "*"; '
        f'test "$(git -C "$IDF_PATH" rev-parse HEAD)" = "{commit}"; '
        f"{idf_target}; "
        "idf.py build; "
        "idf.py size; "
        f"python /work/firmware/tools/build_manifest.py {target}"
    )
    command = [docker, "run", "--rm"]
    if pull:
        command.extend(["--pull", "always"])
    command.extend(
        [
            "-e",
            f"IDF_TARGET={chip}",
            "-e",
            f"PROJECT_VER={project_version}",
            "-e",
            f"LARKBRIDGE_SOURCE_COMMIT={source_commit}",
            "-e",
            f"LARKBRIDGE_SOURCE_DIRTY={'1' if source_dirty else '0'}",
            "-v",
            f"{FIRMWARE_ROOT}:/work/firmware",
            "-w",
            f"/work/firmware/{target}",
            f"espressif/idf:{tag}",
            "/bin/bash",
            "-lc",
            shell,
        ]
    )
    return command


def find_cmake() -> str:
    found = shutil.which("cmake")
    if found:
        return found
    windows_candidate = Path(
        r"C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE"
        r"\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    )
    if windows_candidate.is_file():
        return str(windows_candidate)
    raise FileNotFoundError("cmake is not on PATH")


def find_ctest(cmake: str) -> str:
    found = shutil.which("ctest")
    if found:
        return found
    sibling = Path(cmake).with_name("ctest.exe" if os.name == "nt" else "ctest")
    if sibling.is_file():
        return str(sibling)
    raise FileNotFoundError("ctest is not on PATH or beside cmake")


def host_commands() -> list[list[str]]:
    cmake = find_cmake()
    ctest = find_ctest(cmake)
    build = FIRMWARE_ROOT / "tests" / "host" / "build"
    configure = [cmake, "-S", str(FIRMWARE_ROOT), "-B", str(build)]
    if shutil.which("ninja"):
        configure.extend(["-G", "Ninja"])
    return [
        configure,
        [cmake, "--build", str(build)],
        [ctest, "--test-dir", str(build), "--output-on-failure"],
    ]


def printable(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def host_environment() -> dict[str, str] | None:
    if os.name != "nt" or shutil.which("cl"):
        return None
    base = Path(r"C:\Program Files\Microsoft Visual Studio\2022")
    for edition in ("Community", "BuildTools", "Professional", "Enterprise"):
        script = base / edition / "Common7" / "Tools" / "VsDevCmd.bat"
        if not script.is_file():
            continue
        command = f'call "{script}" -no_logo -arch=x64 && set'
        output = subprocess.check_output(
            command, shell=True, text=True, encoding="utf-8", errors="replace"
        )
        environment = os.environ.copy()
        for line in output.splitlines():
            if "=" in line:
                name, value = line.split("=", 1)
                environment[name] = value
        return environment
    raise FileNotFoundError("MSVC environment is unavailable; run from a Developer shell")


def run(
    command: Sequence[str], *, dry_run: bool, environment: dict[str, str] | None = None
) -> None:
    print(f"+ {printable(command)}", flush=True)
    if not dry_run:
        subprocess.run(command, cwd=REPO_ROOT, check=True, env=environment)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("host", "s31", "output", "all"))
    parser.add_argument("--docker", default=os.environ.get("DOCKER", "docker"))
    parser.add_argument("--no-pull", action="store_true", help="reuse the local IDF image")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lock = load_lock()
    try:
        if args.target in ("host", "all"):
            environment = host_environment()
            for command in host_commands():
                run(command, dry_run=args.dry_run, environment=environment)
        for target in ("output", "s31"):
            if args.target in (target, "all"):
                run(
                    docker_command(target, lock, args.docker, not args.no_pull),
                    dry_run=args.dry_run,
                )
    except (FileNotFoundError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
