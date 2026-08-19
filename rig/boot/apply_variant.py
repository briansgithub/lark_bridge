#!/usr/bin/env python3
"""Deploy and arm one supported userspace boot variant on the physical Pi."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = ROOT / "rig" / "inventory.toml"
REMOTE_ROOT = "/tmp/larkbridge-boot-validate"
SYNC_FILES = (
    "scripts/install.sh",
    "scripts/bootstrap/70-verify.sh",
    "pi/systemd/system/bridge-tuning.service",
    "pi/systemd/system/bridge-btfw.service",
    "pi/systemd/system/bridge-boot-trial-rollback.service",
    "pi/systemd/system/bridge-boot-trial-rollback.timer",
    "pi/systemd/system/NetworkManager-10-larkbridge-netplan-startup.conf",
    "pi/scripts/set-sco-routing.sh",
    "pi/scripts/boot-transaction.sh",
    "pi/scripts/boot-trial.sh",
    "pi/scripts/netplan-startup-fastpath",
    "pi/pipewire/pipewire.conf.d/20-bridge-endpoints.notes.txt",
)
VARIANTS = {
    "baseline": "disable",
    "netplan-fastpath": "enable",
}


def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, check=False, **kwargs)


def sync_sources(host: str) -> None:
    directories = sorted(
        {str(Path(relative).parent).replace("\\", "/") for relative in SYNC_FILES}
    )
    mkdir = run(
        [
            "ssh",
            host,
            "mkdir -p "
            + " ".join(shlex.quote(f"{REMOTE_ROOT}/{path}") for path in directories),
        ]
    )
    if mkdir.returncode != 0:
        raise RuntimeError("failed to create remote boot-variant staging directories")
    for relative in SYNC_FILES:
        source = ROOT / relative
        destination = f"{host}:{REMOTE_ROOT}/{relative}"
        result = run(["scp", str(source), destination])
        if result.returncode != 0:
            raise RuntimeError(f"failed to stage {relative}")


def deploy(host: str, candidate: str, revision: str) -> str:
    try:
        fastpath = VARIANTS[revision]
    except KeyError as exc:
        raise RuntimeError(f"unsupported boot variant revision: {revision}") from exc
    sync_sources(host)
    label = shlex.quote(f"{candidate}-{revision}")
    command = (
        f"cd {REMOTE_ROOT} && sudo -n bash scripts/install.sh --boot-only "
        f"--source-root {REMOTE_ROOT} --transaction-label {label} "
        f"--networkmanager-fastpath {fastpath}"
    )
    result = run(["ssh", host, command], capture_output=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"variant installation failed: {revision}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("variant installer did not return a transaction ID")
    transaction = lines[-1]
    arm = run(
        [
            "ssh",
            host,
            "sudo -n /usr/local/lib/rpi-lark-bridge/boot-trial.sh arm "
            + shlex.quote(transaction),
        ],
        capture_output=True,
    )
    sys.stdout.write(arm.stdout)
    sys.stderr.write(arm.stderr)
    if arm.returncode != 0:
        raise RuntimeError(f"failed to arm transaction: {transaction}")
    return transaction


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    result.add_argument("--candidate", required=True)
    result.add_argument("--revision", choices=tuple(VARIANTS), required=True)
    result.add_argument("--run-dir")
    return result


def main() -> int:
    args = parser().parse_args()
    with args.inventory.open("rb") as stream:
        inventory = tomllib.load(stream)
    host = str(inventory.get("pi_host", "larkbridge"))
    transaction = deploy(host, args.candidate, args.revision)
    print(f"armed={transaction}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
