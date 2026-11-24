#!/usr/bin/env python3
"""LarkBridge ESP32 hardware-in-the-loop runner, v0.

Wraps ``firmware/tools/arrival.py`` with a rig lease, app-only flashing, read-only console
capture, reducers, and the ``firmware/qual/gates.toml`` registry. Each run writes
``docs/experiments/results/E21/<UTC>-<gate>/`` with ``result.json`` (verdict + revisions),
``metrics.json`` and the raw artifacts.

Safety:

* capture requests deasserted DTR and RTS before opening the specified port
  (``arrival._quiet_serial_factory``). The host driver controls the physical lines;
* anything that resets or flashes a board (``run --flash``) also needs ``--allow-reset``;
* flashing writes the application partition only, so NVS and Bluetooth bonds survive; it
  never erases flash, and never touches eFuses, secure boot or flash encryption.

Run under Windows Python (``py -3``) for COM ports; ``gates``, ``next``, ``reduce`` and
``lease`` also work under WSL/Linux Python 3.11+.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIRMWARE_ROOT = REPO_ROOT / "firmware"
GATES_PATH = FIRMWARE_ROOT / "qual" / "gates.toml"
RESULTS_ROOT = REPO_ROOT / "docs" / "experiments" / "results" / "E21"
VERDICTS = ("PASS", "FAIL", "INCONCLUSIVE", "PARTIAL")

sys.path.insert(0, str(FIRMWARE_ROOT / "tools"))
import arrival  # noqa: E402


class HilError(RuntimeError):
    """An operator-correctable runner error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- gates ---------------------------------------------------------------------------------


def load_gates(path: Path = GATES_PATH) -> dict[str, dict[str, Any]]:
    if tomllib is None:
        raise HilError("Python 3.11+ is required to read gates.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise HilError(f"{path}: unsupported schema")
    gates = data.get("gates")
    if not isinstance(gates, dict) or not gates:
        raise HilError(f"{path}: no gates")
    return gates


def validate_gates(gates: dict[str, dict[str, Any]]) -> list[str]:
    """Returns a list of problems; empty means the registry is consistent."""

    problems: list[str] = []
    required = ("title", "tier", "needs", "human", "resets", "reducer", "status")
    for name, gate in gates.items():
        for key in required:
            if key not in gate:
                problems.append(f"{name}: missing {key}")
        for need in gate.get("needs", []):
            if need not in gates:
                problems.append(f"{name}: unknown prerequisite {need}")
        if gate.get("status") not in ("PENDING", "PASS", "FAIL", "PARTIAL"):
            problems.append(f"{name}: bad status {gate.get('status')!r}")
        if gate.get("tier") not in ("T0", "T1", "T2", "T3", "T4", "T5"):
            problems.append(f"{name}: bad tier {gate.get('tier')!r}")
        reducer = gate.get("reducer")
        if reducer not in REDUCERS and reducer not in ("manual", "all_needs"):
            problems.append(f"{name}: unknown reducer {reducer!r}")
        if reducer == "t2_smoke" and gate.get("board") not in ("s31", "output"):
            problems.append(f"{name}: t2_smoke needs board = s31|output")
    # Cycle check (depth-first).
    state: dict[str, int] = {}

    def visit(name: str, trail: list[str]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            problems.append("prerequisite cycle: " + " -> ".join(trail + [name]))
            return
        state[name] = 1
        for need in gates.get(name, {}).get("needs", []):
            if need in gates:
                visit(need, trail + [name])
        state[name] = 2

    for name in gates:
        visit(name, [])
    for index in range(1, 18):
        if f"H{index}" not in gates:
            problems.append(f"H{index} missing from registry")
    return problems


def latest_verdicts(results_root: Path = RESULTS_ROOT) -> dict[str, dict[str, Any]]:
    """The newest recorded hil.py verdict per gate."""

    latest: dict[str, dict[str, Any]] = {}
    if not results_root.is_dir():
        return latest
    for result in sorted(results_root.glob("*/result.json")):
        try:
            record = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        gate = record.get("gate")
        if isinstance(gate, str) and record.get("verdict") in VERDICTS:
            record["_path"] = str(result.parent)
            latest[gate] = record
    return latest


def effective_status(gates: dict[str, dict[str, Any]],
                     verdicts: dict[str, dict[str, Any]]) -> dict[str, str]:
    status = {name: str(gate.get("status", "PENDING")) for name, gate in gates.items()}
    for name, record in verdicts.items():
        if name in status:
            status[name] = record["verdict"]
    for name, gate in gates.items():
        if gate.get("reducer") == "all_needs" and gate.get("needs"):
            if all(status.get(need) == "PASS" for need in gate["needs"]):
                status[name] = "PASS"
    return status


def runnable(gates: dict[str, dict[str, Any]], status: dict[str, str], *,
             allow_human: bool = False, allow_reset: bool = True) -> list[str]:
    ready = []
    for name, gate in gates.items():
        if status.get(name) == "PASS":
            continue
        if gate.get("human") and not allow_human:
            continue
        if gate.get("resets") and not allow_reset:
            continue
        if gate.get("reducer") == "all_needs":
            continue
        if all(status.get(need) == "PASS" for need in gate.get("needs", [])):
            ready.append(name)
    return ready


# --- telemetry parsing ---------------------------------------------------------------------

LOG_PREFIX_RE = re.compile(r"^\[[^\]]+\] ")
BOOT_MARKER_RE = re.compile(r"rst:0x|ESP-ROM:")
CRASH_RE = re.compile(r"Guru Meditation|Brownout detector|abort\(\) was called")
REBOOT_RE = re.compile(f"{BOOT_MARKER_RE.pattern}|{CRASH_RE.pattern}")


def iter_log_lines(text: str) -> Iterable[str]:
    for line in text.splitlines():
        yield LOG_PREFIX_RE.sub("", line, count=1)


def json_records(text: str) -> list[dict[str, Any]]:
    """Every JSON-lines telemetry record in a console log (other lines are ignored)."""

    records = []
    for line in iter_log_lines(text):
        start = line.find('{"t":')
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("t"), str):
            records.append(record)
    return records


def expected_version(manifest: dict[str, Any]) -> str:
    version = str(manifest.get("source_commit", ""))[:12]
    if manifest.get("source_dirty"):
        version += "-dirty"
    if manifest.get("image_kind") == "test":
        version += "-test"
    return version


# --- reducers ------------------------------------------------------------------------------


def reduce_t2_smoke(text: str, *, board: str, manifest: dict[str, Any] | None,
                    require_boot: bool = True) -> tuple[str, dict[str, Any], list[str]]:
    records = json_records(text)
    boots = [r for r in records if r["t"] == "boot" and r.get("role") == board]
    statuses = [r for r in records if r["t"] == "status" and r.get("role") == board]
    stats = [r for r in records if r["t"] == "stats" and r.get("role") == board]
    reboot_markers = sum(1 for line in iter_log_lines(text) if REBOOT_RE.search(line))
    metrics: dict[str, Any] = {
        "board": board,
        "boot_records": len(boots),
        "status_records": len(statuses),
        "stats_records": len(stats),
        "reboot_markers": reboot_markers,
        "errors": sum(1 for r in records if r["t"] == "error"),
    }
    reasons: list[str] = []
    if manifest is None:
        reasons.append("no build manifest to compare against")
        return "INCONCLUSIVE", metrics, reasons
    want_version = expected_version(manifest)
    want_image = manifest.get("image_kind", "release")
    metrics["expected_version"] = want_version
    metrics["expected_image"] = want_image
    if want_image != "test":
        reasons.append("Stage B smoke requires a test image manifest")
    # A boot record, or a `status` reply (same identity fields) when the port opened late.
    full = boots[-1] if boots else (statuses[-1] if statuses else None)
    identity = full if full is not None else (stats[-1] if stats else None)
    if identity is None:
        reasons.append("no boot or stats JSON record from this board")
        return "FAIL", metrics, reasons
    metrics["version"] = identity.get("version")
    metrics["image"] = identity.get("image")
    if identity.get("version") != want_version:
        reasons.append(f"version {identity.get('version')!r} != manifest {want_version!r}")
    if identity.get("image") != want_image:
        reasons.append(f"image {identity.get('image')!r} != manifest {want_image!r}")
    if full is not None:
        boot = full
        metrics["identity_source"] = boot["t"]
        metrics.update({k: boot.get(k) for k in ("reset", "bt", "erase_bonds", "aec_publish", "idf")})
        if boot.get("erase_bonds") != 0:
            reasons.append("erase_bonds is not 0")
        if board == "s31" and boot.get("aec_publish") != 0:
            reasons.append("aec_publish is not 0")
        toolchain = manifest.get("toolchain", {})
        tag = toolchain.get("esp_idf_tag") if isinstance(toolchain, dict) else None
        if tag and not str(boot.get("idf", "")).startswith(tag):
            reasons.append(f"idf {boot.get('idf')!r} != manifest tag {tag!r}")
    seen_telemetry = False
    for line in iter_log_lines(text):
        if CRASH_RE.search(line) or (seen_telemetry and BOOT_MARKER_RE.search(line)):
            reasons.append("crash or reboot during telemetry capture")
            break
        if not seen_telemetry:
            seen_telemetry = any(
                r.get("role") == board and r["t"] in ("boot", "status", "stats")
                for r in json_records(line)
            )
    if len(boots) > 1:
        reasons.append(f"more than one boot record ({len(boots)})")
    if any(r.get("version") != want_version or r.get("image") != want_image for r in stats):
        reasons.append("one or more stats records differ from the manifest")
    uptime = [r.get("up_ms") for r in stats]
    if any(isinstance(a, int) and isinstance(b, int) and b < a
           for a, b in zip(uptime, uptime[1:])):
        reasons.append("stats uptime decreased")
    if len(stats) < 2:
        reasons.append(f"fewer than 2 stats records ({len(stats)})")
    if metrics["errors"]:
        reasons.append(f"{metrics['errors']} telemetry error records")
    if reasons:
        return "FAIL", metrics, reasons
    if require_boot and full is None:
        return "INCONCLUSIVE", metrics, ["identity matched from stats only; no boot/status record"]
    return "PASS", metrics, [f"{full['t'] if full else 'stats'} JSON matches the manifest"]


UAC_FAILURE_RE = re.compile(
    r"UAC transfer error|capture read failed|USB audio device disconnected|"
    r"lacks required 48 kHz|automatic recovery exhausted"
)
UAC_READY_RE = re.compile(r"LARK (?P<vid>[0-9a-f]{4}):(?P<pid>[0-9a-f]{4}) capture ready at "
                          r"(?P<fmt>48 kHz/S16/stereo)")
STAMP_RE = re.compile(r"^\[(?P<ts>[^\]]+)\] ")


def _capture_span_seconds(text: str) -> float:
    stamps = []
    for line in text.splitlines():
        match = STAMP_RE.match(line)
        if match:
            try:
                stamps.append(datetime.fromisoformat(match.group("ts")))
            except ValueError:
                pass
    if len(stamps) < 2:
        return 0.0
    return (max(stamps) - min(stamps)).total_seconds()


UAC_BYTES_PER_S = 48000 * 2 * 2   # 48 kHz, S16, stereo
TAP_ACTIVE_RMS = 300              # about -40 dBFS: speech or music, not an idle link


def reduce_h6_uac(text: str, *, boot_log: str | None = None, min_seconds: float = 1800.0,
                  span_seconds: float | None = None) -> tuple[str, dict[str, Any], list[str]]:
    """H6 from S31 console text: descriptor/format at enumeration and zero UAC failures.

    Test-image evidence adds ``tap lark_in`` replies (format, RMS) and the 1 Hz ``stats`` UAC
    counters: receive-rate gaps against 48 kHz/S16/stereo, and reconnects (``disc``/``opens``).
    PASS needs at least 100 reconnects that all recovered (the Pico fake Lark, or a person
    replugging); otherwise a clean run is PARTIAL.
    """

    combined = (boot_log or "") + "\n" + text
    ready = [m.groupdict() for m in (UAC_READY_RE.search(l) for l in iter_log_lines(combined)) if m]
    failures = [l for l in iter_log_lines(text) if UAC_FAILURE_RE.search(l)]
    stats = [r for r in json_records(text) if r["t"] == "stats" and r.get("role") == "s31"]
    # Several logs with gaps between them: the caller passes the sum of per-log spans.
    span = _capture_span_seconds(text) if span_seconds is None else span_seconds
    metrics: dict[str, Any] = {
        "enumerations": ready,
        "uac_failure_lines": len(failures),
        "capture_seconds": round(span, 1),
        "stats_records": len(stats),
        "reboot_markers": sum(1 for l in iter_log_lines(text) if REBOOT_RE.search(l)),
    }
    if stats:
        first, last = stats[0].get("uac", {}), stats[-1].get("uac", {})
        metrics["uac_rx_packets_delta"] = last.get("rx", 0) - first.get("rx", 0)
        metrics["uac_xfer_err_delta"] = last.get("xfer_err", 0) - first.get("xfer_err", 0)
        metrics["uac_read_err_delta"] = last.get("read_err", 0) - first.get("read_err", 0)
        metrics["uac_disconnects"] = last.get("disc", 0) - first.get("disc", 0)
        metrics["uac_reopens"] = last.get("opens", 0) - first.get("opens", 0)
        gaps = 0
        for before, now in zip(stats, stats[1:]):
            b, n = before.get("uac", {}), now.get("uac", {})
            seconds = (now.get("up_ms", 0) - before.get("up_ms", 0)) / 1000.0
            steady = (b.get("started") and n.get("started") and b.get("disc") == n.get("disc")
                      and seconds > 0)
            if steady and (n.get("bytes", 0) - b.get("bytes", 0)) % 2**32 < 0.5 * UAC_BYTES_PER_S * seconds:
                gaps += 1
        metrics["uac_rx_gap_intervals"] = gaps
    taps = [r for r in json_records(text) if r["t"] == "tap" and r.get("stage") == "lark_in"]
    if taps:
        rms = sorted(int(r.get("rms", 0)) for r in taps)
        metrics["tap_records"] = len(taps)
        metrics["tap_ok"] = sum(1 for r in taps if r.get("ok"))
        metrics["tap_formats"] = sorted({str(r.get("fmt")) for r in taps})
        metrics["tap_rms"] = {"min": rms[0], "median": rms[len(rms) // 2], "max": rms[-1]}
        metrics["tap_active"] = sum(1 for value in rms if value >= TAP_ACTIVE_RMS)
    reasons: list[str] = []
    if failures:
        return "FAIL", metrics, [f"{len(failures)} UAC failure lines, first: {failures[0][:120]}"]
    if metrics.get("uac_xfer_err_delta") or metrics.get("uac_read_err_delta"):
        return "FAIL", metrics, ["UAC error counters increased"]
    if metrics["reboot_markers"]:
        return "FAIL", metrics, ["board rebooted during the capture"]
    if not any(r["vid"] == "3547" and r["pid"] == "0407" for r in ready):
        reasons.append("no 3547:0407 48 kHz/S16/stereo enumeration line in the evidence")
    if span < min_seconds:
        reasons.append(f"capture {span:.0f} s < {min_seconds:.0f} s")
    if not stats:
        reasons.append("no JSON stats (pre-Stage-B image): transfer errors judged from log lines only, "
                       "UAC packet flow not proven")
    elif metrics["uac_rx_gap_intervals"]:
        return "FAIL", metrics, [f"{metrics['uac_rx_gap_intervals']} stats intervals below half the "
                                 "48 kHz/S16/stereo byte rate"]
    if taps and (metrics["tap_formats"] != ["s16le_stereo_48k"] or not metrics["tap_ok"]):
        return "FAIL", metrics, [f"tap lark_in format {metrics['tap_formats']}, ok {metrics['tap_ok']}"]
    if not taps:
        reasons.append("no tap lark_in records: format not verified from tap")
    disconnects = metrics.get("uac_disconnects", 0)
    recovered = min(disconnects, metrics.get("uac_reopens", 0))
    if recovered < disconnects:
        return "FAIL", metrics, [f"only {recovered}/{disconnects} Lark reconnects recovered"]
    if disconnects < 100:
        reasons.append(f"{recovered}/{disconnects} Lark reconnects observed; 100/100 hot-plugs and the "
                       "negative-descriptor tests need the Pico fake Lark or a person")
    if reasons:
        return "PARTIAL", metrics, reasons
    return "PASS", metrics, [f"{recovered}/{disconnects} reconnects, zero UAC errors, format verified"]


def reduce_bt_soak(summary: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
    metrics = {
        key: summary.get(key)
        for key in ("started_utc", "finished_utc", "polls", "polls_link_up", "poll_errors",
                    "pixel_acl_events", "s31_lines", "s31_reboot_markers")
    }
    metrics["link_drops"] = len(summary.get("link_drops", []))
    metrics["serial_errors"] = len(summary.get("serial_errors", []))
    reasons = []
    if not summary.get("polls"):
        return "INCONCLUSIVE", metrics, ["no adb polls"]
    if metrics["link_drops"]:
        reasons.append(f"{metrics['link_drops']} link drops seen by polling")
    if summary.get("s31_reboot_markers"):
        reasons.append("S31 reboot markers in the console capture")
    if summary.get("polls_link_up") != summary.get("polls"):
        reasons.append(f"link up in {summary.get('polls_link_up')}/{summary.get('polls')} polls")
    if reasons:
        return "FAIL", metrics, reasons
    if not summary.get("finished_utc") or summary.get("completed") is False:
        reasons.append("soak did not finish")
    if summary.get("poll_errors"):
        reasons.append("one or more ADB polls failed")
    if metrics["serial_errors"]:
        reasons.append("one or more S31 serial capture errors")
    if summary.get("serial_enabled", True) and not summary.get("s31_lines"):
        reasons.append("no S31 console lines captured")
    if reasons:
        return "INCONCLUSIVE", metrics, reasons
    if summary.get("serial_enabled") is False:
        return "PASS", metrics, ["link up at every ADB poll, serial capture disabled"]
    return "PASS", metrics, ["link up at every poll, no observed drops or reboot"]


REDUCERS = ("t2_smoke", "h6_uac", "bt_soak", "h2h5")


# --- lease ---------------------------------------------------------------------------------


def default_lease_path() -> Path:
    override = os.environ.get("LARKBRIDGE_RIG_LEASE")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path.home() / ".larkbridge" / "rig.lease"
    # Under WSL, share the Windows-side lease so py.exe and Linux runs see one lock.
    match = re.match(r"^(/mnt/[a-z]/Users/[^/]+)/", str(REPO_ROOT))
    if match:
        return Path(match.group(1)) / ".larkbridge" / "rig.lease"
    return Path.home() / ".larkbridge" / "rig.lease"


def read_lease(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        raise HilError(f"could not read rig lease {path}")


@contextmanager
def lease_transaction(path: Path):
    # Atomic directory creation works for both Windows and WSL on the shared volume.
    lock = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise HilError(f"rig lease update in progress or interrupted: {lock}") from error
    try:
        yield
    finally:
        lock.rmdir()


def acquire_lease(path: Path, owner: str, ttl: float, *, now: float | None = None,
                  renew: bool = True) -> dict[str, Any]:
    if not math.isfinite(ttl) or ttl <= 0:
        raise HilError("rig lease TTL must be positive and finite")
    now = time.time() if now is None else now
    record = {"owner": owner, "host": socket.gethostname(), "pid": os.getpid(),
              "acquired": now, "expires": now + ttl}
    with lease_transaction(path):
        current = read_lease(path)
        if current is not None and float(current["expires"]) > now:
            if not renew or current["owner"] != owner:
                raise HilError(f"rig lease held by {current['owner']}")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            pending = Path(stream.name)
            try:
                json.dump(record, stream)
                stream.close()
                pending.replace(path)
            finally:
                pending.unlink(missing_ok=True)
    return record


def release_lease(path: Path, owner: str) -> bool:
    with lease_transaction(path):
        current = read_lease(path)
        if current is None:
            return False
        if current.get("owner") != owner:
            raise HilError(f"rig lease belongs to {current.get('owner')}, not {owner}")
        path.unlink()
    return True


# --- flashing ------------------------------------------------------------------------------

def app_flash_command(target: str, port: str, python: str = sys.executable) -> list[str]:
    """Build the app-only esptool command from a verified bundle."""
    if not arrival.PORT_RE.fullmatch(port):
        raise HilError("serial port contains unsupported characters")
    _, artifacts = arrival.validate_build_manifest(target)
    build = FIRMWARE_ROOT / target / "build"
    definition = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    app = definition.get("app", {})
    expected = {"output": ("esp32", "larkbridge_output.bin"),
                "s31": ("esp32s31", "larkbridge_s31_gateway.bin")}
    chip, filename = expected[target]
    binary = (build / filename).resolve()
    if (app.get("file") != filename or app.get("offset") != "0x10000"
            or app.get("encrypted") not in (False, "false")
            or binary not in artifacts):
        raise HilError("flash definition does not describe the verified app at 0x10000")

    extra = definition.get("extra_esptool_args", {})
    before = extra.get("before", "default-reset")
    after = extra.get("after", "hard-reset")
    if (extra.get("chip") != chip or before != "default-reset"
            or after != "hard-reset"):
        raise HilError("unexpected esptool chip or reset settings")

    write = definition.get("write_flash_args", [])
    allowed = {"--flash-mode": r"dio|dout|qio|qout|keep",
               "--flash-size": r"(?:1|2|4|8|16|32|64|128)MB|keep|detect",
               "--flash-freq": r"(?:20|26|40|80)m|keep"}
    if not isinstance(write, list) or len(write) % 2 or not all(
        isinstance(flag, str) and flag in allowed and isinstance(value, str)
        and re.fullmatch(allowed[flag], value) for flag, value in zip(write[::2], write[1::2])
    ):
        raise HilError("unexpected esptool write options")

    command = [python, "-m", "esptool", "--chip", chip, "--port", port,
               "--before", before, "--after", after]
    if extra.get("stub") is False:
        command.append("--no-stub")
    return command + ["write-flash", *write, app["offset"], str(binary)]


# --- runs ----------------------------------------------------------------------------------


def revisions(target: str | None) -> dict[str, Any]:
    snapshot = arrival.git_snapshot(REPO_ROOT)
    info: dict[str, Any] = {"checkout": snapshot, "hil_version": 0}
    lock = FIRMWARE_ROOT / "toolchain.lock.json"
    if lock.is_file():
        info["toolchain_lock_sha256"] = arrival.sha256(lock)
    if target:
        manifest_path = FIRMWARE_ROOT / target / "build" / "larkbridge-build-manifest.json"
        if manifest_path.is_file():
            info["manifest_sha256"] = arrival.sha256(manifest_path)
    return info


def write_result(gate: str, verdict: str, metrics: dict[str, Any], reasons: list[str], *,
                 target: str | None, artifacts: Sequence[Path] = (), results_root: Path = RESULTS_ROOT,
                 extra: dict[str, Any] | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = results_root / f"{stamp}-{gate}"
    suffix = 1
    while run_dir.exists():
        suffix += 1
        run_dir = results_root / f"{stamp}-{gate}-{suffix}"
    (run_dir / "artifacts").mkdir(parents=True)
    recorded = []
    for artifact in artifacts:
        destination = run_dir / "artifacts" / artifact.name
        shutil.copy2(artifact, destination)
        recorded.append({"name": artifact.name, "sha256": arrival.sha256(destination),
                         "bytes": destination.stat().st_size})
    result = {"schema": 1, "gate": gate, "verdict": verdict, "reasons": reasons,
              "recorded_at": utc_now(), "revisions": revisions(target), "artifacts": recorded,
              **(extra or {})}
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8")
    return run_dir


def capture(port: str, seconds: float, destination: Path,
            commands: Sequence[tuple[float, str]] = ()) -> Path:
    """Read-only capture, plus optional console lines sent at the given offsets.

    Writes send data bytes only. The host driver controls the physical DTR and RTS lines.
    """

    import serial  # type: ignore

    connection = arrival._quiet_serial_factory(serial.Serial)(port=port, baudrate=115200, timeout=0.2)
    start = time.monotonic()
    deadline = start + seconds
    pending = sorted(commands)
    try:
        with destination.open("w", encoding="utf-8", newline="\n") as log:
            log.write(f"[{utc_now()}] CAPTURE_START port={port}\n")
            while time.monotonic() < deadline:
                while pending and time.monotonic() - start >= pending[0][0]:
                    _, line = pending.pop(0)
                    connection.write((line + "\n").encode("ascii"))
                    log.write(f"[{utc_now()}] HOST_SENT {line}\n")
                raw = connection.readline()
                if raw:
                    text = bytes(raw).decode("utf-8", errors="replace").rstrip("\r\n")
                    log.write(f"[{utc_now()}] {text}\n")
            log.write(f"[{utc_now()}] CAPTURE_END\n")
    finally:
        connection.close()
    return destination


def load_manifest(target: str) -> dict[str, Any] | None:
    path = FIRMWARE_ROOT / target / "build" / "larkbridge-build-manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# --- commands ------------------------------------------------------------------------------


def cmd_gates(args: argparse.Namespace) -> int:
    gates = load_gates(args.gates)
    problems = validate_gates(gates)
    status = effective_status(gates, latest_verdicts(args.results))
    if args.json:
        print(json.dumps({"problems": problems, "status": status}, indent=2, sort_keys=True))
    else:
        for name, gate in gates.items():
            flags = ("H" if gate.get("human") else "-") + ("R" if gate.get("resets") else "-")
            print(f"{name:16} {gate['tier']} {flags} {status[name]:12} {gate['title']}")
        for problem in problems:
            print(f"PROBLEM: {problem}")
    return 1 if problems else 0


def cmd_next(args: argparse.Namespace) -> int:
    gates = load_gates(args.gates)
    status = effective_status(gates, latest_verdicts(args.results))
    ready = runnable(gates, status, allow_human=args.human, allow_reset=args.allow_reset)
    print(json.dumps(ready))
    return 0


def h2h5_module():
    sys.path.insert(0, str(REPO_ROOT))
    import h2h5
    return h2h5


def cmd_reduce(args: argparse.Namespace) -> int:
    gates = load_gates(args.gates)
    gate = gates.get(args.gate)
    if gate is None:
        raise HilError(f"unknown gate {args.gate}")
    reducer = gate.get("reducer")
    artifacts = [Path(p) for p in args.log]
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in artifacts] if reducer != "h2h5" else []
    text = "\n".join(texts)
    target = gate.get("board")
    if reducer == "t2_smoke":
        manifest = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else load_manifest(target)
        verdict, metrics, reasons = reduce_t2_smoke(text, board=target, manifest=manifest)
    elif reducer == "h6_uac":
        boot = args.boot_log.read_text(encoding="utf-8", errors="replace") if args.boot_log else None
        if args.boot_log:
            artifacts.append(args.boot_log)
        verdict, metrics, reasons = reduce_h6_uac(
            text, boot_log=boot, min_seconds=float(gate.get("seconds", 1800)),
            span_seconds=sum(_capture_span_seconds(t) for t in texts))
    elif reducer == "h2h5":
        module = h2h5_module()
        logs = [path for path in artifacts if path.suffix == ".jsonl"]
        if len(logs) != 1:
            raise HilError("H2-H5 replay requires one bench.jsonl with optional analyzer artifacts")
        records = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
        verdict, metrics, reasons = module.reduce(args.gate, gate, records, artifacts)
    elif reducer == "bt_soak":
        verdict, metrics, reasons = reduce_bt_soak(json.loads(text))
    else:
        raise HilError(f"gate {args.gate} has reducer {reducer!r}; record it with arrival.py")
    out = {"gate": args.gate, "verdict": verdict, "reasons": reasons, "metrics": metrics}
    if args.record:
        run_dir = write_result(args.gate, verdict, metrics, reasons, target=target,
                               artifacts=artifacts, results_root=args.results,
                               extra={"note": args.note} if args.note else None)
        out["run_dir"] = str(run_dir)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if verdict in ("PASS", "PARTIAL") else 2


def run_two_boards(args, gate):
    module = h2h5_module()
    if (gate["resets"] or args.flash) and not args.allow_reset:
        raise HilError("this run resets a board; add --allow-reset")
    ports = (args.s31_port, args.output_port)
    if any(port is None or not arrival.PORT_RE.fullmatch(port) for port in ports) or ports[0] == ports[1]:
        raise HilError("specify distinct --s31-port and --output-port")
    lease = default_lease_path()
    acquire_lease(lease, args.owner, ttl=max(600, gate.get("stream_seconds", 0) + 900,
                                            gate.get("clean_seconds", 0) + 900), renew=False)
    try:
        with tempfile.TemporaryDirectory(prefix="lark-hil-") as directory:
            staging = Path(directory)
            bench = None
            records = []
            try:
                expected = module.identify_ports(ports)
                if args.flash:
                    manifests = {}
                    commands = {}
                    for role, port in zip(module.ROLES, ports):
                        manifest, _ = arrival.validate_build_manifest(role)
                        if manifest.get("image_kind") != "test":
                            raise HilError(f"{role} build must be a test image")
                        manifests[role] = expected_version(manifest)
                        commands[role] = app_flash_command(role, port)
                    for role, port in zip(module.ROLES, ports):
                        module.identify_ports((port,), (expected[role],))
                        with (staging / f"{role}-flash.log").open("w", encoding="utf-8") as log:
                            completed = subprocess.run(commands[role], stdout=log, stderr=subprocess.STDOUT,
                                                       timeout=120)
                        if completed.returncode:
                            raise HilError(f"{role} app-only flash failed")
                        shutil.copy2(FIRMWARE_ROOT / role / "build" / "larkbridge-build-manifest.json",
                                     staging / f"{role}-manifest.json")
                        time.sleep(3)
                bench = module.Bench(ports, staging, arrival._quiet_serial_factory)
                records = bench.records
                for role, status in bench.pair("status").items():
                    if status.get("image") != "test" or status.get("erase_bonds") != 0 or (
                        "dec_err" not in status.get("uart", {})
                    ):
                        raise HilError(f"{role} requires the H2-H5 test image with bond erase disabled")
                    if args.flash and status.get("version") != manifests[role]:
                        raise HilError(f"{role} reported a version different from the flashed manifest")
                module.run_scenario(bench, args.gate, gate, args.analyzer)
            except (OSError, HilError, module.BenchError, subprocess.TimeoutExpired) as error:
                if bench is None:
                    records.append({"t": "abort", "reason": str(error)})
                    (staging / "bench.jsonl").write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
                else:
                    bench.add(t="abort", reason=str(error))
            finally:
                if bench is not None:
                    bench.close()
            artifacts = list(staging.iterdir())
            verdict, metrics, reasons = module.reduce(args.gate, gate, records, artifacts)
            run_dir = write_result(args.gate, verdict, metrics, reasons, target=None, artifacts=artifacts,
                                   results_root=args.results, extra={"ports": ports, "flashed": args.flash})
            print(json.dumps({"gate": args.gate, "verdict": verdict, "reasons": reasons,
                              "run_dir": str(run_dir)}, indent=2))
            return 0 if verdict == "PASS" else 2
    finally:
        release_lease(lease, args.owner)


def cmd_run(args: argparse.Namespace) -> int:
    gates = load_gates(args.gates)
    gate = gates.get(args.gate)
    if gate is None:
        raise HilError(f"unknown gate {args.gate}")
    if gate.get("reducer") == "h2h5":
        return run_two_boards(args, gate)
    if gate.get("reducer") != "t2_smoke":
        raise HilError("hil.py v0 runs only t2_smoke gates; use reduce for recorded logs")
    target = gate["board"]
    if not args.port:
        raise HilError("T2 smoke requires --port")
    if args.flash and not args.allow_reset:
        raise HilError("--flash resets the board; add --allow-reset to confirm it is allowed now")
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        raise HilError("--seconds must be positive and finite")
    lease_path = default_lease_path()
    acquire_lease(lease_path, args.owner, ttl=max(600.0, args.seconds + 300.0), renew=False)
    try:
        manifest, _ = arrival.validate_build_manifest(target)
        if manifest.get("image_kind") != "test":
            raise HilError("Stage B smoke requires a test image build")
        staging = Path(os.environ.get("TEMP", "/tmp")) / f"hil-{os.getpid()}"
        staging.mkdir(parents=True, exist_ok=True)
        artifacts = []
        if args.flash:
            command = app_flash_command(target, args.port)
            flash_log = staging / "flash.log"
            with flash_log.open("w", encoding="utf-8") as log:
                log.write("+ " + " ".join(command) + "\n")
                log.flush()
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            artifacts.append(flash_log)
            if completed.returncode != 0:
                run_dir = write_result(args.gate, "FAIL", {"flash_exit": completed.returncode},
                                       ["app-only flash failed"], target=target, artifacts=artifacts,
                                       results_root=args.results)
                print(run_dir)
                return 2
        log_path = capture(args.port, args.seconds, staging / f"{target}-console.log",
                           commands=[(3.0, "status"), (max(4.0, args.seconds - 3.0), "status")])
        artifacts.append(log_path)
        manifest_path = FIRMWARE_ROOT / target / "build" / "larkbridge-build-manifest.json"
        artifacts.append(manifest_path)
        verdict, metrics, reasons = reduce_t2_smoke(
            log_path.read_text(encoding="utf-8", errors="replace"), board=target, manifest=manifest)
        run_dir = write_result(args.gate, verdict, metrics, reasons, target=target,
                               artifacts=artifacts, results_root=args.results,
                               extra={"port": args.port, "flashed": args.flash})
        print(json.dumps({"gate": args.gate, "verdict": verdict, "reasons": reasons,
                          "run_dir": str(run_dir)}, indent=2))
        return 0 if verdict == "PASS" else 2
    finally:
        release_lease(lease_path, args.owner)


def cmd_lease(args: argparse.Namespace) -> int:
    path = default_lease_path()
    if args.action == "acquire":
        print(json.dumps(acquire_lease(path, args.owner, args.ttl)))
    elif args.action == "release":
        print("released" if release_lease(path, args.owner) else "not held")
    else:
        print(json.dumps({"path": str(path), "lease": read_lease(path)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LarkBridge ESP32 HIL runner v0")
    parser.add_argument("--gates", type=Path, default=GATES_PATH)
    parser.add_argument("--results", type=Path, default=RESULTS_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    gates = sub.add_parser("gates", help="list gates, statuses and registry problems")
    gates.add_argument("--json", action="store_true")
    gates.set_defaults(func=cmd_gates)

    nxt = sub.add_parser("next", help="gates whose prerequisites have passed")
    nxt.add_argument("--human", action="store_true", help="include gates that need a person")
    nxt.add_argument("--no-reset", dest="allow_reset", action="store_false",
                     help="exclude gates that reset a board")
    nxt.set_defaults(func=cmd_next)

    reduce = sub.add_parser("reduce", help="reduce recorded logs to a verdict")
    reduce.add_argument("gate")
    reduce.add_argument("--log", type=Path, action="append", required=True)
    reduce.add_argument("--boot-log", type=Path)
    reduce.add_argument("--manifest", type=Path)
    reduce.add_argument("--record", action="store_true", help="write an E21 result directory")
    reduce.add_argument("--note")
    reduce.set_defaults(func=cmd_reduce)

    run = sub.add_parser("run", help="run a qualification gate on hardware")
    run.add_argument("gate")
    run.add_argument("--port")
    run.add_argument("--s31-port")
    run.add_argument("--output-port")
    run.add_argument("--analyzer", help="sigrok driver for H4 (for example fx2lafw)")
    run.add_argument("--seconds", type=float, default=20.0)
    run.add_argument("--flash", action="store_true", help="app-only flash first (resets the board)")
    run.add_argument("--allow-reset", action="store_true")
    run.add_argument("--owner", default=f"hil-{socket.gethostname()}-{os.getpid()}")
    run.set_defaults(func=cmd_run)

    lease = sub.add_parser("lease", help="rig lease")
    lease.add_argument("action", choices=("acquire", "release", "status"))
    lease.add_argument("--owner", default=f"hil-{socket.gethostname()}")
    lease.add_argument("--ttl", type=float, default=3600.0)
    lease.set_defaults(func=cmd_lease)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (HilError, arrival.ArrivalError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
