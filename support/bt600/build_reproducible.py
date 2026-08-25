#!/usr/bin/env python3
"""Run two clean pinned-container builds and publish only identical packages."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


SUPPORT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = SUPPORT_ROOT.parents[1]
MANIFEST = json.loads((SUPPORT_ROOT / "sources.json").read_text(encoding="utf-8"))
IMAGE_TAG = "larkbridge-bt600-builder:6.18.34-rpt-rpi-v8"


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-image-build", action="store_true")
    parser.add_argument("--image", default=IMAGE_TAG)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.skip_image_build:
        run(
            [
                "docker",
                "build",
                "--platform",
                "linux/arm64",
                "--pull",
                "-f",
                str(SUPPORT_ROOT / "Dockerfile"),
                "-t",
                args.image,
                str(SUPPORT_ROOT),
            ]
        )

    filename = (
        f"{MANIFEST['package']['name']}_{MANIFEST['package']['version']}_"
        f"{MANIFEST['package']['architecture']}.deb"
    )
    with tempfile.TemporaryDirectory(prefix="larkbridge-bt600-repro-") as temporary:
        temporary_root = Path(temporary)
        artifacts: list[Path] = []
        for number in (1, 2):
            run_output = temporary_root / f"run-{number}"
            run_output.mkdir()
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--platform",
                    "linux/arm64",
                    "--mount",
                    f"type=bind,src={REPOSITORY_ROOT},dst=/workspace,readonly",
                    "--mount",
                    f"type=bind,src={run_output},dst=/out",
                    args.image,
                    "--output",
                    "/out",
                ]
            )
            artifact = run_output / filename
            if not artifact.is_file():
                raise RuntimeError(f"build {number} did not produce {filename}")
            artifacts.append(artifact)

        first_hash = sha256(artifacts[0])
        second_hash = sha256(artifacts[1])
        if first_hash != second_hash or artifacts[0].read_bytes() != artifacts[1].read_bytes():
            raise RuntimeError(
                f"reproducibility failure: build 1={first_hash}, build 2={second_hash}"
            )

        output = args.output.resolve()
        output.mkdir(parents=True, exist_ok=True)
        published = output / filename
        shutil.copyfile(artifacts[0], published)
        (output / "SHA256SUMS").write_text(
            f"{first_hash}  {filename}\n", encoding="utf-8", newline="\n"
        )
        print(f"reproducibility verification: PASS ({first_hash})", flush=True)
        print(f"published: {published}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
