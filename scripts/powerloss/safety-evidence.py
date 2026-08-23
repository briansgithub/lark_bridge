#!/usr/bin/env python3
"""Create and verify the mandatory backup/recovery-card safety record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = 1


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def create(arguments: argparse.Namespace) -> None:
    image = arguments.image.resolve(strict=True)
    if not image.is_file() or image.stat().st_size < 1024 * 1024 * 1024:
        raise ValueError("backup image must be a regular file of at least 1 GiB")
    if not arguments.acknowledge_recovery_boot:
        raise ValueError("a physically boot-tested recovery card must be acknowledged")
    image_hash = sha256(image)
    document = {
        "backup": {
            "image": str(image),
            "sha256": image_hash,
            "size": image.stat().st_size,
            "verified_unix_ns": time.time_ns(),
        },
        "recovery_card": {
            "boot_id": arguments.recovery_boot_id,
            "card_serial": arguments.recovery_card_serial,
            "physically_boot_tested": True,
            "source_image_sha256": image_hash,
            "verified_unix_ns": time.time_ns(),
        },
        "schema": SCHEMA,
    }
    atomic_json(arguments.output, document)
    print(arguments.output.resolve())


def validate_document(path: Path, *, rehash: bool) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid evidence file: {error}") from error
    if document.get("schema") != SCHEMA:
        raise ValueError("unsupported safety-evidence schema")
    backup = document.get("backup", {})
    recovery = document.get("recovery_card", {})
    if not recovery.get("physically_boot_tested"):
        raise ValueError("recovery card is not recorded as physically boot-tested")
    if not recovery.get("boot_id") or not recovery.get("card_serial"):
        raise ValueError("recovery card boot ID and serial are required")
    if recovery.get("source_image_sha256") != backup.get("sha256"):
        raise ValueError("recovery card is not tied to the verified backup image")
    image = Path(backup.get("image", ""))
    if not image.is_file() or image.stat().st_size != backup.get("size"):
        raise ValueError("backup image is missing or its size changed")
    if rehash and sha256(image) != backup.get("sha256"):
        raise ValueError("backup image checksum changed")
    return document


def verify(arguments: argparse.Namespace) -> None:
    document = validate_document(arguments.evidence, rehash=not arguments.no_rehash)
    print(
        json.dumps(
            {
                "backup_sha256": document["backup"]["sha256"],
                "backup_size": document["backup"]["size"],
                "recovery_boot_id": document["recovery_card"]["boot_id"],
                "status": "VERIFIED",
            },
            sort_keys=True,
        )
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    create_command = commands.add_parser("create")
    create_command.add_argument("--image", type=Path, required=True)
    create_command.add_argument("--output", type=Path, required=True)
    create_command.add_argument("--recovery-card-serial", required=True)
    create_command.add_argument("--recovery-boot-id", required=True)
    create_command.add_argument("--acknowledge-recovery-boot", action="store_true")
    create_command.set_defaults(function=create)
    verify_command = commands.add_parser("verify")
    verify_command.add_argument("--evidence", type=Path, required=True)
    verify_command.add_argument(
        "--no-rehash", action="store_true", help=argparse.SUPPRESS
    )
    verify_command.set_defaults(function=verify)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        arguments.function(arguments)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
