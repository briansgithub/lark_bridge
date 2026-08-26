#!/usr/bin/env python3
"""Prepare, capture, and preserve Larkbridge hardware-arrival evidence."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_ROOT = REPO_ROOT / "firmware"
DEFAULT_RESULTS_ROOT = REPO_ROOT / "docs" / "experiments" / "results" / "E18"
VALID_FINAL_STATUSES = ("PASS", "FAIL", "PENDING_HARDWARE")
GATE_RE = re.compile(r"H(?:[0-9]|1[0-7])\Z")
PORT_RE = re.compile(r"[A-Za-z0-9_./:\\\-]+\Z")


class ArrivalError(RuntimeError):
    """An operator-correctable arrival workflow error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_output(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def git_snapshot(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    try:
        commit = _git_output(repo_root, "rev-parse", "HEAD")
        status = _git_output(
            repo_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            ".",
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ArrivalError(f"cannot inspect source repository: {error}") from error
    dirty_paths = status.splitlines() if status else []
    return {"commit": commit, "dirty": bool(dirty_paths), "dirty_paths": dirty_paths}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "run"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _unique_directory(base: Path) -> Path:
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = base.with_name(f"{base.name}-{index}")
        if not candidate.exists():
            return candidate
    raise ArrivalError(f"cannot allocate a unique run directory below {base.parent}")


def create_run(
    gate: str,
    label: str,
    operator: str,
    output_root: Path = DEFAULT_RESULTS_ROOT,
    *,
    source: dict[str, Any] | None = None,
    started_at: str | None = None,
) -> Path:
    gate = gate.upper()
    if not GATE_RE.fullmatch(gate):
        raise ArrivalError("gate must be H0 through H17")
    if not label.strip():
        raise ArrivalError("label cannot be empty")
    if not operator.strip():
        raise ArrivalError("operator cannot be empty")

    timestamp = started_at or utc_now()
    source = source if source is not None else git_snapshot()
    compact_time = timestamp.replace("-", "").replace(":", "").split("+")[0]
    compact_time = compact_time.replace(".000000", "")
    run_id = f"{compact_time}-{gate}-{_slug(label)}"
    run_dir = _unique_directory(output_root.resolve() / run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    result = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "gate": gate,
        "label": label.strip(),
        "operator": operator.strip(),
        "status": "IN_PROGRESS",
        "hard_failure": False,
        "started_at": timestamp,
        "finished_at": None,
        "source": source,
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "evidence": [],
        "notes": [],
    }
    _write_json(run_dir / "result.json", result)
    return run_dir


def load_result(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    run_dir = run_dir.resolve()
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        raise ArrivalError(f"result file not found: {result_path}")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArrivalError(f"cannot read {result_path}: {error}") from error
    if result.get("schema_version") != 1 or not isinstance(result.get("evidence"), list):
        raise ArrivalError(f"unsupported or malformed result file: {result_path}")
    return run_dir, result


def _unique_artifact_path(artifact_dir: Path, requested_name: str, digest: str) -> Path:
    name = Path(requested_name).name
    if not name or name in (".", ".."):
        raise ArrivalError("artifact name is invalid")
    candidate = artifact_dir / name
    if not candidate.exists():
        return candidate
    if candidate.is_file() and sha256(candidate) == digest:
        return candidate
    suffixes = "".join(candidate.suffixes)
    stem = candidate.name[: -len(suffixes)] if suffixes else candidate.name
    return artifact_dir / f"{stem}-{digest[:8]}{suffixes}"


def _append_evidence(
    run_dir: Path,
    result: dict[str, Any],
    stored_path: Path,
    *,
    kind: str,
    original_path: str,
    note: str | None,
) -> dict[str, Any]:
    digest = sha256(stored_path)
    relative = stored_path.relative_to(run_dir).as_posix()
    existing = next(
        (
            entry
            for entry in result["evidence"]
            if entry.get("stored_path") == relative and entry.get("sha256") == digest
        ),
        None,
    )
    if existing is not None:
        return existing
    entry = {
        "kind": kind,
        "original_path": original_path,
        "stored_path": relative,
        "bytes": stored_path.stat().st_size,
        "sha256": digest,
        "recorded_at": utc_now(),
        "note": note.strip() if note and note.strip() else None,
    }
    result["evidence"].append(entry)
    _write_json(run_dir / "result.json", result)
    return entry


def record_artifact(run: Path, artifact: Path, note: str | None = None) -> dict[str, Any]:
    run_dir, result = load_result(run)
    source = artifact.resolve()
    if not source.is_file():
        raise ArrivalError(f"artifact is not a file: {source}")
    digest = sha256(source)
    destination = _unique_artifact_path(run_dir / "artifacts", source.name, digest)
    if source != destination.resolve():
        shutil.copy2(source, destination)
    return _append_evidence(
        run_dir,
        result,
        destination,
        kind="artifact",
        original_path=str(source),
        note=note,
    )


def finalize_run(run: Path, status: str, note: str | None = None) -> Path:
    run_dir, result = load_result(run)
    status = status.upper()
    if status not in VALID_FINAL_STATUSES:
        raise ArrivalError(f"status must be one of {', '.join(VALID_FINAL_STATUSES)}")
    if result.get("status") != "IN_PROGRESS":
        raise ArrivalError(f"run is already finalized as {result.get('status')}")
    result["status"] = status
    result["hard_failure"] = status == "FAIL"
    result["finished_at"] = utc_now()
    if note and note.strip():
        result["notes"].append({"recorded_at": utc_now(), "text": note.strip()})
    _write_json(run_dir / "result.json", result)
    return run_dir / "result.json"


def list_ports() -> list[dict[str, str | None]]:
    try:
        from serial.tools import list_ports as serial_list_ports  # type: ignore
    except ImportError as error:
        raise ArrivalError(
            "pyserial is required for port discovery; install it with "
            "'py -3 -m pip install pyserial'"
        ) from error
    return [
        {
            "device": port.device,
            "description": port.description,
            "hardware_id": port.hwid,
            "serial_number": getattr(port, "serial_number", None),
        }
        for port in serial_list_ports.comports()
    ]


def _safe_capture_name(name: str) -> str:
    if Path(name).name != name or name in ("", ".", ".."):
        raise ArrivalError("capture name must be a plain file name")
    return name


def _unique_capture_path(artifact_dir: Path, name: str) -> Path:
    candidate = artifact_dir / name
    if not candidate.exists():
        return candidate
    suffixes = "".join(candidate.suffixes)
    stem = candidate.name[: -len(suffixes)] if suffixes else candidate.name
    for index in range(2, 1000):
        candidate = artifact_dir / f"{stem}-{index}{suffixes}"
        if not candidate.exists():
            return candidate
    raise ArrivalError(f"cannot allocate a unique capture file below {artifact_dir}")


def capture_serial(
    run: Path,
    port: str,
    duration: float,
    *,
    baud: int = 115200,
    name: str = "serial.log",
    note: str | None = None,
    serial_factory: Callable[..., Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    if duration <= 0:
        raise ArrivalError("capture duration must be greater than zero")
    if baud <= 0:
        raise ArrivalError("baud must be greater than zero")
    if not PORT_RE.fullmatch(port):
        raise ArrivalError("serial port contains unsupported characters")
    name = _safe_capture_name(name)
    run_dir, result = load_result(run)
    destination = _unique_capture_path(run_dir / "artifacts", name)
    if serial_factory is None:
        try:
            import serial  # type: ignore
        except ImportError as error:
            raise ArrivalError(
                "pyserial is required for capture; install it with "
                "'py -3 -m pip install pyserial'"
            ) from error
        serial_factory = serial.Serial

    connection = serial_factory(port=port, baudrate=baud, timeout=0.2)
    deadline = time.monotonic() + duration
    interrupted = False
    try:
        with destination.open("w", encoding="utf-8", newline="\n") as log:
            log.write(f"[{utc_now()}] CAPTURE_START port={port} baud={baud}\n")
            try:
                while time.monotonic() < deadline:
                    raw = connection.readline()
                    if not raw:
                        continue
                    text = bytes(raw).decode("utf-8", errors="replace").rstrip("\r\n")
                    log.write(f"[{utc_now()}] {text}\n")
            except KeyboardInterrupt:
                interrupted = True
            log.write(
                f"[{utc_now()}] CAPTURE_{'INTERRUPTED' if interrupted else 'END'}\n"
            )
    finally:
        connection.close()

    entry = _append_evidence(
        run_dir,
        result,
        destination,
        kind="serial_log",
        original_path=f"serial://{port}?baud={baud}",
        note=note,
    )
    return destination, entry


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_build_manifest(
    target: str, firmware_root: Path = FIRMWARE_ROOT, *, allow_dirty: bool = False
) -> tuple[dict[str, Any], list[Path]]:
    if target not in ("s31", "output"):
        raise ArrivalError(f"unknown target: {target}")
    firmware_root = firmware_root.resolve()
    project = (firmware_root / target).resolve()
    manifest_path = project / "build" / "larkbridge-build-manifest.json"
    if not manifest_path.is_file():
        raise ArrivalError(f"build manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArrivalError(f"cannot read build manifest: {error}") from error
    if manifest.get("schema") != 1 or manifest.get("target") != target:
        raise ArrivalError(f"manifest does not describe target {target}")
    if manifest.get("source_dirty") and not allow_dirty:
        raise ArrivalError(
            "build manifest came from a dirty source tree; rebuild a committed "
            "checkpoint or pass the explicit allow-dirty option"
        )
    records = manifest.get("artifacts")
    if not isinstance(records, list) or not records:
        raise ArrivalError("manifest contains no artifacts")

    artifacts: list[Path] = []
    recorded_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ArrivalError("manifest contains a malformed artifact record")
        relative = Path(record["path"])
        artifact = (project / relative).resolve()
        if relative.is_absolute() or not _inside(artifact, project):
            raise ArrivalError(f"manifest artifact escapes project: {relative}")
        if not artifact.is_file():
            raise ArrivalError(f"manifest artifact is missing: {artifact}")
        if sha256(artifact) != record.get("sha256"):
            raise ArrivalError(f"manifest hash mismatch: {artifact}")
        if artifact.stat().st_size != record.get("bytes"):
            raise ArrivalError(f"manifest size mismatch: {artifact}")
        recorded_paths.add(relative.as_posix())
        artifacts.append(artifact)

    requirements = {
        "bootloader": "build/bootloader/bootloader.bin" in recorded_paths,
        "partition table": "build/partition_table/partition-table.bin" in recorded_paths,
        "flash arguments": "build/flash_args" in recorded_paths,
        "flash definition": "build/flasher_args.json" in recorded_paths,
        "application image": any(
            path.startswith("build/")
            and path.count("/") == 1
            and path.endswith(".bin")
            for path in recorded_paths
        ),
    }
    missing = [name for name, present in requirements.items() if not present]
    if missing:
        raise ArrivalError(f"manifest is missing required {', '.join(missing)}")

    toolchain_lock = firmware_root / "toolchain.lock.json"
    expected_toolchain = manifest.get("toolchain_lock_sha256")
    if expected_toolchain:
        if not toolchain_lock.is_file() or sha256(toolchain_lock) != expected_toolchain:
            raise ArrivalError("toolchain lock does not match the build manifest")
    component_lock = project / "dependencies.lock"
    expected_component = manifest.get("component_lock_sha256")
    if expected_component:
        if not component_lock.is_file() or sha256(component_lock) != expected_component:
            raise ArrivalError("component lock does not match the build manifest")
    return manifest, artifacts


def _flash_definition(target: str, firmware_root: Path) -> dict[str, Any]:
    path = firmware_root / target / "build" / "flasher_args.json"
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArrivalError(f"cannot read generated flash definition: {error}") from error
    extra = definition.get("extra_esptool_args")
    if not isinstance(extra, dict) or not isinstance(extra.get("chip"), str):
        raise ArrivalError(f"malformed generated flash definition: {path}")
    return definition


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def flash_command(
    target: str, port: str, firmware_root: Path = FIRMWARE_ROOT, *, allow_dirty: bool = False
) -> list[str]:
    if not PORT_RE.fullmatch(port):
        raise ArrivalError("serial port contains unsupported characters")
    validate_build_manifest(target, firmware_root, allow_dirty=allow_dirty)
    project = (firmware_root / target).resolve()
    definition = _flash_definition(target, firmware_root.resolve())
    extra = definition["extra_esptool_args"]
    no_stub = " --no-stub" if extra.get("stub") is False else ""
    return [
        f"Set-Location -LiteralPath {_powershell_literal(str(project / 'build'))}",
        f"python -m esptool --chip {extra['chip']} --port {port} "
        f"--before {extra.get('before', 'default-reset')} "
        f"--after {extra.get('after', 'hard-reset')}{no_stub} "
        "write-flash '@flash_args'",
        f"Set-Location -LiteralPath {_powershell_literal(str(project))}",
        f"idf.py -p {port} monitor",
    ]


def _bundle_flash_script(target: str, definition: dict[str, Any]) -> str:
    extra = definition["extra_esptool_args"]
    no_stub = " --no-stub" if extra.get("stub") is False else ""
    return (
        "param([Parameter(Mandatory=$true)][string]$Port)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$bundleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path\n"
        f"$buildRoot = Join-Path $bundleRoot 'firmware/{target}/build'\n"
        "Push-Location -LiteralPath $buildRoot\n"
        "try {\n"
        f"    & python -m esptool --chip {extra['chip']} --port $Port "
        f"--before {extra.get('before', 'default-reset')} "
        f"--after {extra.get('after', 'hard-reset')}{no_stub} "
        "write-flash '@flash_args'\n"
        "    if ($LASTEXITCODE -ne 0) { throw \"esptool failed with exit code $LASTEXITCODE\" }\n"
        "} finally {\n"
        "    Pop-Location\n"
        "}\n"
    )


def create_bundle(
    target: str,
    output: Path | None = None,
    *,
    firmware_root: Path = FIRMWARE_ROOT,
    force: bool = False,
    allow_dirty: bool = False,
) -> tuple[Path, Path]:
    firmware_root = firmware_root.resolve()
    manifest, artifacts = validate_build_manifest(
        target, firmware_root, allow_dirty=allow_dirty
    )
    project = (firmware_root / target).resolve()
    definition = _flash_definition(target, firmware_root)
    source_commit = str(manifest.get("source_commit", "unknown"))
    short_commit = re.sub(r"[^A-Za-z0-9]", "", source_commit)[:12] or "unknown"
    output = (
        output.resolve()
        if output is not None
        else firmware_root / "dist" / f"larkbridge-{target}-{short_commit}.zip"
    )
    if output.suffix.lower() != ".zip":
        raise ArrivalError("bundle output must end in .zip")
    checksum_path = output.with_suffix(output.suffix + ".sha256")
    if not force and (output.exists() or checksum_path.exists()):
        raise ArrivalError(f"bundle already exists (use --force to replace it): {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    manifest_path = project / "build" / "larkbridge-build-manifest.json"
    extras = [
        manifest_path,
        project / "sdkconfig",
        project / "sdkconfig.defaults",
        project / "dependencies.lock",
        firmware_root / "toolchain.lock.json",
    ]
    files = list(dict.fromkeys([*artifacts, *(path for path in extras if path.is_file())]))
    info = {
        "schema": 1,
        "generated_at": utc_now(),
        "target": target,
        "source_commit": source_commit,
        "source_dirty": bool(manifest.get("source_dirty")),
        "flash_from": f"firmware/{target}",
        "flash_command": ".\\FLASH.ps1 -Port <SERIAL_PORT>",
        "files": [],
    }
    flash_script = _bundle_flash_script(target, definition)
    flash_script_bytes = flash_script.encode("utf-8")

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in files:
            if _inside(source.resolve(), firmware_root):
                arcname = (Path("firmware") / source.relative_to(firmware_root)).as_posix()
            else:
                raise ArrivalError(f"bundle source escapes firmware tree: {source}")
            archive.write(source, arcname)
            info["files"].append(
                {"path": arcname, "bytes": source.stat().st_size, "sha256": sha256(source)}
            )
        archive.writestr("FLASH.ps1", flash_script_bytes)
        info["files"].append(
            {
                "path": "FLASH.ps1",
                "bytes": len(flash_script_bytes),
                "sha256": hashlib.sha256(flash_script_bytes).hexdigest(),
            }
        )
        archive.writestr(
            "BUNDLE_INFO.json", json.dumps(info, indent=2, sort_keys=True) + "\n"
        )
    checksum_path.write_text(
        f"{sha256(output)}  {output.name}\n", encoding="ascii", newline="\n"
    )
    return output, checksum_path


def _new_run_command(args: argparse.Namespace) -> int:
    run = create_run(args.gate, args.label, args.operator, args.output_root)
    print(run)
    return 0


def _record_command(args: argparse.Namespace) -> int:
    entry = record_artifact(args.run, args.artifact, args.note)
    print(json.dumps(entry, sort_keys=True))
    return 0


def _finalize_command(args: argparse.Namespace) -> int:
    print(finalize_run(args.run, args.status, args.note))
    return 0


def _ports_command(args: argparse.Namespace) -> int:
    ports = list_ports()
    if args.json:
        print(json.dumps(ports, indent=2, sort_keys=True))
    elif not ports:
        print("No serial ports found.")
    else:
        for port in ports:
            serial_number = f" serial={port['serial_number']}" if port["serial_number"] else ""
            print(
                f"{port['device']}: {port['description']} "
                f"[{port['hardware_id']}]{serial_number}"
            )
    return 0


def _capture_command(args: argparse.Namespace) -> int:
    destination, entry = capture_serial(
        args.run,
        args.port,
        args.seconds,
        baud=args.baud,
        name=args.name,
        note=args.note,
    )
    print(destination)
    print(f"sha256={entry['sha256']}")
    return 0


def _flash_command_command(args: argparse.Namespace) -> int:
    manifest, _ = validate_build_manifest(args.target, allow_dirty=args.allow_dirty)
    print(
        f"Validated {args.target} build from source {manifest.get('source_commit')} "
        f"(dirty={bool(manifest.get('source_dirty'))})."
    )
    for line in flash_command(args.target, args.port, allow_dirty=args.allow_dirty):
        print(line)
    return 0


def _bundle_command(args: argparse.Namespace) -> int:
    bundle, checksum = create_bundle(
        args.target, args.output, force=args.force, allow_dirty=args.allow_dirty
    )
    print(bundle)
    print(checksum)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_run = subparsers.add_parser("new-run", help="create an auditable gate run")
    new_run.add_argument("--gate", required=True, help="qualification gate H0 through H17")
    new_run.add_argument("--label", required=True, help="short run description")
    new_run.add_argument("--operator", default=getpass.getuser(), help="operator name")
    new_run.add_argument("--output-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    new_run.set_defaults(func=_new_run_command)

    record = subparsers.add_parser("record", help="copy and hash an evidence artifact")
    record.add_argument("--run", required=True, type=Path)
    record.add_argument("--artifact", required=True, type=Path)
    record.add_argument("--note")
    record.set_defaults(func=_record_command)

    finalize = subparsers.add_parser("finalize", help="close a gate run")
    finalize.add_argument("--run", required=True, type=Path)
    finalize.add_argument("--status", required=True, choices=VALID_FINAL_STATUSES)
    finalize.add_argument("--note")
    finalize.set_defaults(func=_finalize_command)

    ports = subparsers.add_parser("ports", help="list candidate serial ports")
    ports.add_argument("--json", action="store_true")
    ports.set_defaults(func=_ports_command)

    capture = subparsers.add_parser("capture", help="capture and attach a serial log")
    capture.add_argument("--run", required=True, type=Path)
    capture.add_argument("--port", required=True)
    capture.add_argument("--seconds", required=True, type=float)
    capture.add_argument("--baud", type=int, default=115200)
    capture.add_argument("--name", default="serial.log")
    capture.add_argument("--note")
    capture.set_defaults(func=_capture_command)

    flash = subparsers.add_parser(
        "flash-command", help="validate a build and print its safe flash command"
    )
    flash.add_argument("--target", required=True, choices=("s31", "output"))
    flash.add_argument("--port", required=True)
    flash.add_argument(
        "--allow-dirty", action="store_true", help="explicitly permit a dirty-source manifest"
    )
    flash.set_defaults(func=_flash_command_command)

    bundle = subparsers.add_parser("bundle", help="package a validated target build")
    bundle.add_argument("--target", required=True, choices=("s31", "output"))
    bundle.add_argument("--output", type=Path)
    bundle.add_argument("--force", action="store_true")
    bundle.add_argument(
        "--allow-dirty", action="store_true", help="explicitly permit a dirty-source manifest"
    )
    bundle.set_defaults(func=_bundle_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ArrivalError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
