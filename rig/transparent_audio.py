#!/usr/bin/env python3
"""Closed-loop development controller for transparent Pixel audio.

This program runs on the control PC.  Nothing reaches the Pi or Pixel unless the
operator supplies ``--live``.  Live mutations are scoped to one development session,
record exact preimages, and arm a Pi-side rollback unit before changing anything.

The public entry points are exposed by ``rig transparent-audio``::

    baseline       read-only bench/instrument snapshot
    session-start  stage a candidate in /run and arm rollback
    iterate        run one media or call measurement
    transition     wait for and capture a transport transition
    session-stop   restore the deployed preimages
    accept         create a compact acceptance manifest

``calibrate`` is an additional bench-maintenance command.  It deliberately records
one calibration stage at a time so a cable move can never be mistaken for a completed
three-stage qualification.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import tomllib

REPO = Path(__file__).resolve().parents[1]
RIG_ROOT = REPO / "rig"
DEFAULT_INVENTORY = RIG_ROOT / "inventory.toml"
DEFAULT_ARTIFACTS = REPO / "artifacts" / "e19-dev"
SESSION_FILE = "session.json"
CALIBRATION_FILE = "generalplus-calibration.json"

EXIT_FAILURE = 1
EXIT_HARDWARE = 78
RUNTIME_ROOT = "/run/user/1000/larkbridge-dev"
RECOVERY_ROOT = "/home/admin/.local/state/larkbridge-dev-recovery"
OVERRIDE_DIR = "/run/user/1000/systemd/user/bridge-supervisor.service.d"
OVERRIDE_PATH = f"{OVERRIDE_DIR}/90-larkbridge-dev.conf"
STATUS_PATH = "/run/user/1000/bridge-status.json"
CONFIG_PATH = "/home/admin/rpi-lark-bridge/config/bridge.toml"
WP_DEPLOYED_DIR = "/home/admin/.config/wireplumber/wireplumber.conf.d"
PHONE_MEDIA_REMOTE = "/sdcard/Download/larkbridge-transparent-audio.wav"

GENERALPLUS_USB_ID = "1b3f:2008"
GENERALPLUS_PORT = "1-1.5"
GENERALPLUS_RATE = 48_000
GENERALPLUS_CAPTURE_CHANNELS = 1
GENERALPLUS_PLAYBACK_CHANNELS = 2

REQUIRED_CALIBRATION_STAGES = ("self-loop", "aux-loop", "acoustic")
POLICY_PREFIXES = ("pi/wireplumber/",)
SUPERVISOR_PREFIXES = ("pi/bridged/",)
ALLOWED_UNTRACKED_PREFIXES = (
    "pi/bridged/",
    "pi/wireplumber/",
    "config/",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class RigFailure(RuntimeError):
    """A measured or safety failure."""


class HardwareRequired(RigFailure):
    """The bench needs an operator action; this is a pause, not a failed candidate."""


class SafetyFailure(RigFailure):
    """A transaction invariant failed; never continue by guessing."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    def require(self, label: str) -> CommandResult:
        if self.returncode:
            detail = self.stderr.strip() or self.stdout.strip() or "no output"
            raise RigFailure(f"{label} failed (exit {self.returncode}): {detail}")
        return self


class Backend(Protocol):
    def local(self, command: Sequence[str], *, cwd: Path | None = None, timeout: float = 60) -> CommandResult: ...

    def pi(self, script: str, *, timeout: float = 60, stdin: bytes | None = None) -> CommandResult: ...

    def adb(self, args: Sequence[str], *, timeout: float = 60) -> CommandResult: ...

    def fetch(self, remote: str, local: Path, *, recursive: bool = False) -> None: ...

    def wait(self, seconds: float) -> None: ...


class LiveBackend:
    """Subprocess transport.  Constructed only after ``--live`` is accepted."""

    def __init__(self, pi_host: str, adb: str, phone_serial: str = "") -> None:
        self.pi_host = pi_host
        self.adb_path = adb
        self.phone_serial = phone_serial

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = 60,
        stdin: bytes | None = None,
    ) -> CommandResult:
        try:
            proc = subprocess.run(
                list(command),
                cwd=cwd,
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
        return CommandResult(
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )

    def local(self, command: Sequence[str], *, cwd: Path | None = None, timeout: float = 60) -> CommandResult:
        return self._run(command, cwd=cwd, timeout=timeout)

    def pi(self, script: str, *, timeout: float = 60, stdin: bytes | None = None) -> CommandResult:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            self.pi_host,
            "export XDG_RUNTIME_DIR=/run/user/$(id -u); " + script,
        ]
        return self._run(command, timeout=timeout, stdin=stdin)

    def adb(self, args: Sequence[str], *, timeout: float = 60) -> CommandResult:
        command = [self.adb_path]
        if self.phone_serial:
            command.extend(("-s", self.phone_serial))
        command.extend(args)
        return self._run(command, timeout=timeout)

    def fetch(self, remote: str, local: Path, *, recursive: bool = False) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        command = ["scp", "-q"]
        if recursive:
            command.append("-r")
        command.extend((f"{self.pi_host}:{remote}", str(local)))
        self._run(command, timeout=120).require(f"fetch {remote}")

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class InstrumentSpec:
    usb_id: str = GENERALPLUS_USB_ID
    port_path: str = GENERALPLUS_PORT
    rate: int = GENERALPLUS_RATE
    capture_channels: int = GENERALPLUS_CAPTURE_CHANNELS
    playback_channels: int = GENERALPLUS_PLAYBACK_CHANNELS
    agc_control: str = "Auto Gain Control"
    sidetone_control: str = "Mic Playback Switch"
    capture_switch_control: str = "Mic Capture Switch"
    capture_volume_control: str = "Mic Capture Volume"


@dataclass(frozen=True)
class Thresholds:
    noise_floor_dbfs: float = -60.0
    linearity_error_db: float = 1.5
    dynamic_range_db: float = 50.0
    aux_above_floor_db: float = 40.0
    acoustic_snr_db: float = 20.0
    clipping_pct: float = 0.01
    call_raw_dbfs: float = -55.0
    aec_suppression_db: float = 10.0


@dataclass(frozen=True)
class Inventory:
    path: Path
    pi_host: str
    phone_serial: str
    pixel_bt_mac: str
    instrument: InstrumentSpec
    thresholds: Thresholds
    aux_volume: float
    cable_id: str
    speaker_position_id: str
    far_end_capture_command: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Inventory:
        if not path.is_file():
            raise HardwareRequired(
                f"missing {path}; copy rig/inventory.toml.example and describe the bench"
            )
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        instrument = InstrumentSpec(
            usb_id=str(data.get("generalplus_usb_id", GENERALPLUS_USB_ID)).lower(),
            port_path=str(data.get("generalplus_port_path", GENERALPLUS_PORT)),
            rate=int(data.get("generalplus_rate", GENERALPLUS_RATE)),
            capture_channels=int(data.get("generalplus_capture_channels", 1)),
            playback_channels=int(data.get("generalplus_playback_channels", 2)),
            agc_control=str(data.get("generalplus_agc_control", "Auto Gain Control")),
            sidetone_control=str(data.get("generalplus_sidetone_control", "Mic Playback Switch")),
            capture_switch_control=str(data.get("generalplus_capture_switch_control", "Mic Capture Switch")),
            capture_volume_control=str(data.get("generalplus_capture_volume_control", "Mic Capture Volume")),
        )
        thresholds = Thresholds(
            noise_floor_dbfs=float(data.get("e19_max_noise_floor_dbfs", -60.0)),
            linearity_error_db=float(data.get("e19_max_linearity_error_db", 1.5)),
            dynamic_range_db=float(data.get("e19_min_dynamic_range_db", 50.0)),
            aux_above_floor_db=float(data.get("e19_min_aux_above_floor_db", 40.0)),
            acoustic_snr_db=float(data.get("e19_min_acoustic_snr_db", 20.0)),
            clipping_pct=float(data.get("e19_max_clipping_pct", 0.01)),
            call_raw_dbfs=float(data.get("e19_min_call_raw_dbfs", -55.0)),
            aec_suppression_db=float(data.get("e19_min_aec_suppression_db", 10.0)),
        )
        return cls(
            path=path,
            pi_host=str(data.get("pi_host", "larkbridge")),
            phone_serial=str(data.get("phone_serial", "")),
            pixel_bt_mac=str(data.get("pixel_bt_mac", "")),
            instrument=instrument,
            thresholds=thresholds,
            aux_volume=float(data.get("e19_aux_volume", 0.95)),
            cable_id=str(data.get("generalplus_cable_id", "")),
            speaker_position_id=str(data.get("generalplus_speaker_position_id", "")),
            far_end_capture_command=tuple(
                str(item) for item in data.get("discord_far_end_capture_command", [])
            ),
        )


def locate_adb() -> str:
    candidates = [
        RIG_ROOT / "adb" / "platform-tools" / "adb.exe",
        Path(os.environ.get("ANDROID_SDK_ROOT", "")) / "platform-tools" / "adb.exe",
        Path(os.environ.get("ANDROID_HOME", "")) / "platform-tools" / "adb.exe",
        Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("adb")
    if found:
        return found
    raise HardwareRequired("adb not found; run `rig setup-adb` or set ANDROID_SDK_ROOT")


def require_live(arguments: argparse.Namespace, inventory: Inventory) -> LiveBackend:
    if not arguments.live:
        raise HardwareRequired(
            "live bench access was not authorized; inspect the dry-run output, then repeat with --live"
        )
    return LiveBackend(inventory.pi_host, locate_adb(), inventory.phone_serial)


def parse_json_result(result: CommandResult, label: str) -> dict[str, Any]:
    result.require(label)
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RigFailure(f"{label} returned malformed JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RigFailure(f"{label} did not return a JSON object")
    return document


REMOTE_PROBE = r'''python3 - <<'PY'
import glob,json,os,pathlib,re,subprocess
usb_id,required_port = __USB_ID__,__PORT__
vid,pid=usb_id.split(':')
devices=[]
for node in glob.glob('/sys/bus/usb/devices/*'):
 p=pathlib.Path(node)
 try:
  got_vid=(p/'idVendor').read_text().strip().lower()
  got_pid=(p/'idProduct').read_text().strip().lower()
 except OSError:
  continue
 if (got_vid,got_pid)!=(vid,pid): continue
 port=p.name
 cards=[]
 for card in glob.glob('/sys/class/sound/card*'):
  try: resolved=pathlib.Path(card).resolve()
  except OSError: continue
  if p.resolve() not in resolved.parents: continue
  number=int(re.search(r'card(\d+)$',card).group(1))
  try: alsa_id=pathlib.Path(f'/proc/asound/card{number}/id').read_text().strip()
  except OSError: alsa_id=''
  cards.append({'number':number,'alsa_id':alsa_id})
 devices.append({'port':port,'sysfs':str(p),'cards':cards})
result={'usb_id':usb_id,'required_port':required_port,'devices':devices}
matching=[d for d in devices if d['port']==required_port]
result['ready']=len(devices)==1 and len(matching)==1 and len(matching[0]['cards'])==1
if result['ready']:
 card=matching[0]['cards'][0]
  result['alsa_id']=card['alsa_id']
  result['ephemeral_card_number']=card['number']
 if not card['alsa_id']:
  result['ready']=False; result['reason']='resolved card has no ALSA id'
 else:
  proc=subprocess.run(['amixer','-c',str(card['number']),'contents'],capture_output=True,text=True)
  result['mixer_returncode']=proc.returncode
  result['mixer_contents']=proc.stdout
  result['mixer_sha256']=__import__('hashlib').sha256(proc.stdout.encode()).hexdigest()
  try: result['stream_capabilities']=pathlib.Path(f'/proc/asound/card{card["number"]}/stream0').read_text()
  except OSError as exc: result['stream_capabilities_error']=f'{type(exc).__name__}: {exc}'
elif len(devices)!=1:
 result['reason']=f'expected exactly one {usb_id}, found {len(devices)}'
elif not matching:
 result['reason']=f'{usb_id} is at {devices[0]["port"]}, required {required_port}'
else:
 result['reason']=f'expected one ALSA card, found {len(matching[0]["cards"])}'
print(json.dumps(result))
PY'''


def probe_instrument(backend: Backend, spec: InstrumentSpec) -> dict[str, Any]:
    script = REMOTE_PROBE.replace("__USB_ID__", repr(spec.usb_id)).replace(
        "__PORT__", repr(spec.port_path)
    )
    report = parse_json_result(backend.pi(script, timeout=30), "GeneralPlus probe")
    if not report.get("ready"):
        raise HardwareRequired(str(report.get("reason") or "GeneralPlus instrument is not ready"))
    if report.get("mixer_returncode") != 0:
        raise HardwareRequired("GeneralPlus amixer control map is unavailable")
    validate_instrument_capabilities(report, spec)
    return report


def validate_instrument_capabilities(report: Mapping[str, Any], spec: InstrumentSpec) -> None:
    raw = str(report.get("stream_capabilities", ""))
    playback, separator, capture = raw.partition("Capture:")
    if not separator:
        raise HardwareRequired("GeneralPlus ALSA playback/capture capabilities are unavailable")
    required = {
        "playback S16_LE": "Format: S16_LE" in playback,
        f"playback {spec.playback_channels}ch": f"Channels: {spec.playback_channels}" in playback,
        f"playback {spec.rate} Hz": str(spec.rate) in playback,
        "capture S16_LE": "Format: S16_LE" in capture,
        f"capture {spec.capture_channels}ch": f"Channels: {spec.capture_channels}" in capture,
        f"capture {spec.rate} Hz": str(spec.rate) in capture,
    }
    missing = [name for name, present in required.items() if not present]
    if missing:
        raise HardwareRequired(
            "GeneralPlus does not expose the qualified PCM contract: " + ", ".join(missing)
        )


def instrument_fingerprint(inventory: Inventory, report: Mapping[str, Any]) -> str:
    identity = {
        "usb_id": inventory.instrument.usb_id,
        "port": inventory.instrument.port_path,
        "rate": inventory.instrument.rate,
        "capture_channels": inventory.instrument.capture_channels,
        "playback_channels": inventory.instrument.playback_channels,
        # Values are checked independently before every measurement. Calibration is
        # bound to the control/capability map, not to a transient volume snapshot.
        "mixer_map_sha256": mixer_map_sha256(str(report.get("mixer_contents", ""))),
        "cable_id": inventory.cable_id,
        "speaker_position_id": inventory.speaker_position_id,
        "aux_volume": inventory.aux_volume,
    }
    return sha256_bytes(canonical_json(identity))


def _control_present(contents: str, name: str) -> bool:
    escaped = re.escape(name)
    return bool(re.search(rf"name='{escaped}'(?:,|\n)", contents))


def mixer_map_sha256(contents: str) -> str:
    lines = []
    for line in contents.splitlines():
        stripped = line.strip()
        if stripped.startswith(("numid=", "; type=", "; limits=", "| dBscale-")):
            lines.append(stripped)
    return sha256_bytes("\n".join(lines).encode())


def mixer_value(contents: str, control: str) -> str | None:
    blocks = re.split(r"(?=^numid=)", contents, flags=re.MULTILINE)
    for block in blocks:
        if not _control_present(block, control):
            continue
        match = re.search(r"^\s*: values=(.*)$", block, flags=re.MULTILINE)
        return match.group(1).strip().lower() if match else None
    return None


def validate_mixer_map(report: Mapping[str, Any], spec: InstrumentSpec) -> None:
    contents = str(report.get("mixer_contents", ""))
    required = (
        spec.agc_control,
        spec.sidetone_control,
        spec.capture_switch_control,
        spec.capture_volume_control,
    )
    missing = [control for control in required if not _control_present(contents, control)]
    if missing:
        raise HardwareRequired(
            "GeneralPlus mixer map has not been qualified; missing controls: " + ", ".join(missing)
        )


def validate_prepared_mixer_state(
    report: Mapping[str, Any],
    spec: InstrumentSpec,
    *,
    expected_capture_value: str | None = "0",
) -> None:
    validate_mixer_map(report, spec)
    contents = str(report.get("mixer_contents", ""))
    expected = {
        spec.agc_control: {"off", "off,off"},
        spec.sidetone_control: {"off", "off,off"},
        spec.capture_switch_control: {"on", "on,on"},
    }
    drift = [
        f"{name}={mixer_value(contents, name)!r}"
        for name, values in expected.items()
        if mixer_value(contents, name) not in values
    ]
    gain = mixer_value(contents, spec.capture_volume_control)
    if gain is None or (
        expected_capture_value is not None
        and gain != expected_capture_value
    ):
        drift.append(f"{spec.capture_volume_control}={gain!r}")
    if drift:
        raise HardwareRequired(
            "GeneralPlus mixer drifted from the calibrated safe state: " + ", ".join(drift)
        )


def safe_mixer_script(
    spec: InstrumentSpec, alsa_id: str, *, capture_gain: str = "0%"
) -> str:
    """Return fail-closed mixer setup using a freshly resolved stable ALSA ID."""

    card = shlex.quote(f"hw:CARD={alsa_id}")
    controls = {
        spec.agc_control: "off",
        spec.sidetone_control: "off",
        spec.capture_switch_control: "on",
        # Minimum hardware capture gain.  Calibration may subsequently choose a
        # higher *recorded* value; it is never inherited from an earlier session.
        spec.capture_volume_control: capture_gain,
    }
    lines = ["set -euo pipefail", f"card={card}"]
    for name, value in controls.items():
        lines.append(
            "amixer -D \"$card\" sset "
            + shlex.quote(name)
            + " "
            + shlex.quote(value)
            + " >/dev/null"
        )
    lines.append('amixer -D "$card" contents')
    return "; ".join(lines)


def prepare_mixer(
    backend: Backend,
    inventory: Inventory,
    report: Mapping[str, Any],
    *,
    capture_gain: str = "0%",
) -> dict[str, Any]:
    validate_mixer_map(report, inventory.instrument)
    alsa_id = str(report["alsa_id"])
    result = backend.pi(
        safe_mixer_script(inventory.instrument, alsa_id, capture_gain=capture_gain),
        timeout=30,
    )
    result.require("safe GeneralPlus mixer setup")
    prepared = dict(report)
    prepared["mixer_contents"] = result.stdout
    prepared["mixer_sha256"] = sha256_bytes(result.stdout.encode())
    prepared_value = mixer_value(result.stdout, inventory.instrument.capture_volume_control)
    if prepared_value is None:
        raise HardwareRequired("GeneralPlus capture gain could not be verified after setup")
    prepared["prepared_capture_gain_request"] = capture_gain
    prepared["prepared_capture_gain_value"] = prepared_value
    validate_prepared_mixer_state(
        prepared,
        inventory.instrument,
        expected_capture_value=prepared_value,
    )
    return prepared


def _mixer_restore_script(recovery: str, instrument: InstrumentSpec) -> str:
    return f'''#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import glob,json,pathlib,re,subprocess
vid,pid={instrument.usb_id!r}.split(':'); wanted={instrument.port_path!r}; card=None
for node in glob.glob('/sys/bus/usb/devices/*'):
 p=pathlib.Path(node)
 try: match=(p/'idVendor').read_text().strip().lower()==vid and (p/'idProduct').read_text().strip().lower()==pid and p.name==wanted
 except OSError: continue
 if not match: continue
 for value in glob.glob('/sys/class/sound/card*'):
  q=pathlib.Path(value)
  if p.resolve() in q.resolve().parents: card=int(re.search(r'card(\\d+)$',value).group(1)); break
if card is None:
 print(json.dumps({{'restored':False,'reason':'instrument not found at its qualified port'}})); raise SystemExit(1)
proc=subprocess.run(['alsactl','-f',{(recovery + '/mixer.state')!r},'restore',str(card)],capture_output=True,text=True)
result={{'restored':proc.returncode==0,'card':card,'stdout':proc.stdout,'stderr':proc.stderr}}
pathlib.Path({(recovery + '/mixer-recovery-result.json')!r}).write_text(json.dumps(result,sort_keys=True))
print(json.dumps(result,sort_keys=True)); raise SystemExit(proc.returncode)
PY
'''


def start_mixer_guard(
    backend: Backend,
    instrument: Mapping[str, Any],
    spec: InstrumentSpec,
    *,
    seconds: int = 600,
) -> tuple[str, str]:
    guard_id = f"calibration-{stamp()}"
    recovery = f"{RECOVERY_ROOT}/{guard_id}"
    script = _mixer_restore_script(recovery, spec).encode()
    encoded = base64.b64encode(script).decode()
    command = (
        f"set -euo pipefail; mkdir -p {shlex.quote(recovery)}; "
        f"alsactl -f {shlex.quote(recovery + '/mixer.state')} store "
        f"{int(instrument['ephemeral_card_number'])}; "
        f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(recovery + '/restore.sh')}; "
        f"chmod 700 {shlex.quote(recovery + '/restore.sh')}"
    )
    backend.pi(command, timeout=30).require("capture calibration mixer preimage")
    arm_deadman(backend, guard_id, recovery, seconds)
    return guard_id, recovery


def stop_mixer_guard(backend: Backend, guard_id: str, recovery: str) -> dict[str, Any]:
    result = backend.pi(f"/bin/bash {shlex.quote(recovery + '/restore.sh')}", timeout=45)
    report = parse_json_result(result, "restore calibration mixer preimage")
    if report.get("restored") is not True:
        raise SafetyFailure(f"calibration mixer preimage did not restore: {report}")
    cancel_deadman(backend, guard_id)
    return report


def load_calibration(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HardwareRequired(
            "GeneralPlus calibration is missing; run the three `rig transparent-audio calibrate` stages"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SafetyFailure(f"calibration file is malformed: {exc}") from exc
    if not isinstance(document, dict):
        raise SafetyFailure("calibration is not a JSON object")
    return document


def validate_calibration(
    document: Mapping[str, Any], fingerprint: str, thresholds: Thresholds
) -> None:
    if document.get("instrument_fingerprint") != fingerprint:
        raise HardwareRequired(
            "GeneralPlus calibration is stale (instrument, port, mixer, cable, AUX volume, or speaker position changed)"
        )
    stages = document.get("stages")
    if not isinstance(stages, dict):
        raise HardwareRequired("GeneralPlus calibration has no completed stages")
    missing = [stage for stage in REQUIRED_CALIBRATION_STAGES if stage not in stages]
    if missing:
        raise HardwareRequired("GeneralPlus calibration stages missing: " + ", ".join(missing))
    self_loop = stages["self-loop"]
    aux_loop = stages["aux-loop"]
    acoustic = stages["acoustic"]
    failures: list[str] = []
    if float(self_loop.get("noise_floor_dbfs", math.inf)) > thresholds.noise_floor_dbfs:
        failures.append("self-loop noise floor")
    if float(self_loop.get("linearity_error_db", math.inf)) > thresholds.linearity_error_db:
        failures.append("self-loop linearity")
    if float(self_loop.get("dynamic_range_db", -math.inf)) < thresholds.dynamic_range_db:
        failures.append("self-loop dynamic range")
    if float(aux_loop.get("above_floor_db", -math.inf)) < thresholds.aux_above_floor_db:
        failures.append("AUX signal margin")
    if float(acoustic.get("snr_db", -math.inf)) < thresholds.acoustic_snr_db:
        failures.append("acoustic SNR")
    for stage in REQUIRED_CALIBRATION_STAGES:
        if float(stages[stage].get("clipped_pct", math.inf)) > thresholds.clipping_pct:
            failures.append(f"{stage} clipping")
    if failures:
        raise HardwareRequired("GeneralPlus calibration does not meet gates: " + ", ".join(failures))


@dataclass(frozen=True)
class Candidate:
    source: str
    repository: Path
    revision: str
    candidate_id: str
    diff_sha256: str
    content_sha256: str
    changed_paths: tuple[str, ...]
    untracked: tuple[dict[str, str], ...]
    package_tar: bytes
    policy_files: Mapping[str, bytes]

    def manifest(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "repository": str(self.repository),
            "revision": self.revision,
            "candidate_id": self.candidate_id,
            "diff_sha256": self.diff_sha256,
            "content_sha256": self.content_sha256,
            "changed_paths": list(self.changed_paths),
            "untracked": list(self.untracked),
            "package_tar_sha256": sha256_bytes(self.package_tar),
            "policy_files": {
                path: sha256_bytes(content) for path, content in sorted(self.policy_files.items())
            },
        }


def _git(repo: Path, *arguments: str, binary: bool = False) -> bytes | str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RigFailure(f"git is unavailable: {exc}") from exc
    if proc.returncode:
        detail = proc.stderr.decode(errors="replace").strip()
        raise RigFailure(f"git {' '.join(arguments)} failed: {detail}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")


def _candidate_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for prefix in ("pi/bridged", "pi/wireplumber"):
        directory = root / prefix
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _archive_files(repo: Path, revision: str) -> dict[str, bytes]:
    archive = _git(
        repo,
        "archive",
        "--format=tar",
        revision,
        "pi/bridged",
        "pi/wireplumber",
        binary=True,
    )
    assert isinstance(archive, bytes)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
        for member in handle.getmembers():
            if not member.isfile():
                continue
            stream = handle.extractfile(member)
            if stream is not None:
                files[PurePosixPath(member.name).as_posix()] = stream.read()
    return files


def _package_tar(files: Mapping[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as handle:
        for path, content in sorted(files.items()):
            if not path.startswith("pi/bridged/"):
                continue
            relative = path.removeprefix("pi/bridged/")
            info = tarfile.TarInfo(relative)
            info.size = len(content)
            info.mtime = 0
            info.mode = 0o755 if content.startswith(b"#!") else 0o644
            handle.addfile(info, io.BytesIO(content))
    return stream.getvalue()


def resolve_candidate(source: str, *, repository: Path = REPO) -> Candidate:
    candidate_path = Path(source).resolve()
    is_worktree = candidate_path.is_dir() and (
        (candidate_path / ".git").exists() or (candidate_path / "pi" / "bridged").is_dir()
    )
    if is_worktree:
        repo = candidate_path
        revision = str(_git(repo, "rev-parse", "HEAD")).strip()
        diff = _git(repo, "diff", "--binary", "--no-ext-diff", "HEAD", binary=True)
        assert isinstance(diff, bytes)
        changed = set(
            str(_git(repo, "diff", "--name-only", "HEAD")).splitlines()
        )
        untracked_paths = str(
            _git(repo, "ls-files", "--others", "--exclude-standard")
        ).splitlines()
        allowed_untracked: list[dict[str, str]] = []
        for path in sorted(untracked_paths):
            normalized = PurePosixPath(path).as_posix()
            if not normalized.startswith(ALLOWED_UNTRACKED_PREFIXES):
                continue
            absolute = repo / Path(normalized)
            if absolute.is_file():
                digest = sha256_bytes(absolute.read_bytes())
                allowed_untracked.append({"path": normalized, "sha256": digest})
                changed.add(normalized)
        files = _candidate_files(repo)
    else:
        repo = repository
        revision = str(_git(repo, "rev-parse", source)).strip()
        diff = b""
        changed = set()
        allowed_untracked = []
        files = _archive_files(repo, revision)
    if "pi/bridged/bridge_supervisor.py" not in files:
        raise RigFailure("candidate does not contain the complete pi/bridged package")
    content_manifest = {path: sha256_bytes(content) for path, content in sorted(files.items())}
    content_hash = sha256_bytes(canonical_json(content_manifest))
    diff_hash = sha256_bytes(diff)
    identity = {
        "revision": revision,
        "diff_sha256": diff_hash,
        "content_sha256": content_hash,
        "untracked": allowed_untracked,
    }
    identity_hash = sha256_bytes(canonical_json(identity))
    candidate_id = f"{revision[:10]}-{identity_hash[:12]}"
    policies = {
        path.removeprefix("pi/wireplumber/wireplumber.conf.d/"): content
        for path, content in files.items()
        if path.startswith("pi/wireplumber/wireplumber.conf.d/")
    }
    return Candidate(
        source=source,
        repository=repo,
        revision=revision,
        candidate_id=candidate_id,
        diff_sha256=diff_hash,
        content_sha256=content_hash,
        changed_paths=tuple(sorted(changed)),
        untracked=tuple(allowed_untracked),
        package_tar=_package_tar(files),
        policy_files=policies,
    )


def classify_restart(previous: Mapping[str, Any], candidate: Candidate) -> str:
    old_content = previous.get("content_sha256")
    old_policies = previous.get("policy_files")
    new_policies = candidate.manifest()["policy_files"]
    if old_policies != new_policies:
        return "audio-stack"
    if old_content != candidate.content_sha256:
        return "supervisor"
    return "none"


def _remote_manifest_script() -> str:
    return r'''python3 - <<'PY'
import glob,hashlib,json,os,pathlib,subprocess
paths=[]
for pattern in (
 '/home/admin/rpi-lark-bridge/pi/bridged/*.py',
 '/home/admin/.config/wireplumber/wireplumber.conf.d/*',
 '/home/admin/rpi-lark-bridge/config/bridge.toml',
 '/home/admin/.config/systemd/user/bridge-supervisor.service'):
 paths.extend(glob.glob(pattern))
hashes={}
for value in sorted(set(paths)):
 p=pathlib.Path(value)
 if p.is_file(): hashes[value]=hashlib.sha256(p.read_bytes()).hexdigest()
def run(command):
 p=subprocess.run(command,capture_output=True,text=True)
 return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
result={
 'timestamp':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
 'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'deployed_hashes':hashes,
 'deployed_head':run(['git','-C','/home/admin/rpi-lark-bridge','rev-parse','HEAD']),
 'services':run(['systemctl','--user','show','pipewire.service','pipewire-pulse.service','wireplumber.service','bridge-supervisor.service','--property=Id','--property=ActiveState','--property=NRestarts','--property=ExecMainStartTimestampMonotonic','--property=Environment','--property=ExecStart']),
 'system_services':run(['systemctl','show','bluetooth.service','bridge-btwatchdog@call.service','--property=Id','--property=ActiveState','--property=NRestarts','--property=ExecMainStartTimestampMonotonic']),
 'bluetooth':run(['bluetoothctl','show']),
 'usb':run(['lsusb']),
 'usb_topology':run(['lsusb','-t']),
 'graph':run(['pw-dump']),
 'links':run(['pw-link','-l']),
 'kernel_errors':run(['/bin/bash','-lc',"journalctl -k --no-pager -n 1000 | grep -Ei 'bluetooth.*(timeout|unexpected)|usb.*(reset|error|fail)|under-voltage|over-current' || true"]),
 'watchdog':run(['sudo','-n','cat','/run/larkbridge/bt-watchdog/call.json']),
 'status':{},
}
try: result['status']=json.loads(pathlib.Path('/run/user/1000/bridge-status.json').read_text())
except Exception as exc: result['status_error']=f'{type(exc).__name__}: {exc}'
print(json.dumps(result))
PY'''


def capture_snapshot(backend: Backend, *, full: bool = False) -> dict[str, Any]:
    pi = parse_json_result(backend.pi(_remote_manifest_script(), timeout=45), "Pi snapshot")
    android_audio = backend.adb(("shell", "dumpsys", "audio"), timeout=30)
    android_bt = backend.adb(("shell", "dumpsys", "bluetooth_manager"), timeout=30)
    devices = backend.adb(("devices",), timeout=15)
    devices.require("ADB device inventory")
    android_audio.require("Android audio snapshot")
    android_bt.require("Android Bluetooth snapshot")
    pi["android"] = {
        "adb_devices": asdict(devices),
        "audio": asdict(android_audio),
        "bluetooth_manager": asdict(android_bt),
    }
    if full:
        journals = backend.pi(
            "journalctl --no-pager -n 500 -u bluetooth.service -u bridge-btwatchdog@call.service; "
            "journalctl --user --no-pager -n 500 -u pipewire.service -u pipewire-pulse.service "
            "-u wireplumber.service -u bridge-supervisor.service",
            timeout=45,
        )
        pi["journals"] = asdict(journals)
    return pi


def service_restarts(snapshot: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for block_name in ("services", "system_services"):
        block = snapshot.get(block_name)
        if not isinstance(block, dict):
            continue
        current = ""
        for line in str(block.get("stdout", "")).splitlines():
            if line.startswith("Id="):
                current = line.partition("=")[2]
            elif line.startswith("NRestarts=") and current:
                with contextlib.suppress(ValueError):
                    result[current] = int(line.partition("=")[2])
    return result


def changed_restarts(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, int]:
    first, second = service_restarts(before), service_restarts(after)
    return {
        unit: second.get(unit, 0) - first.get(unit, 0)
        for unit in sorted(first.keys() | second.keys())
        if second.get(unit, 0) != first.get(unit, 0)
    }


def watchdog_recoveries(snapshot: Mapping[str, Any]) -> int:
    block = snapshot.get("watchdog")
    if not isinstance(block, dict):
        return 0
    raw = block.get("stdout", "")
    try:
        document = json.loads(str(raw))
    except json.JSONDecodeError:
        return 0
    return int(document.get("recoveries", 0)) if isinstance(document, dict) else 0


def new_kernel_errors(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    def lines(document: Mapping[str, Any]) -> set[str]:
        block = document.get("kernel_errors")
        if not isinstance(block, dict):
            return set()
        return {line.strip() for line in str(block.get("stdout", "")).splitlines() if line.strip()}

    return sorted(lines(after) - lines(before))


def status_phone(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    status = snapshot.get("status")
    if not isinstance(status, dict):
        return {}
    phone = status.get("phone")
    return phone if isinstance(phone, dict) else {}


def wait_for(
    backend: Backend,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float,
    interval: float = 0.5,
    label: str,
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() - started < timeout:
        last = capture_snapshot(backend, full=False)
        if predicate(last):
            return last, time.monotonic() - started
        backend.wait(interval)
    raise RigFailure(f"timed out after {timeout:g}s waiting for {label}; last phone status={status_phone(last)}")


def session_path(artifacts: Path) -> Path:
    return artifacts / SESSION_FILE


def load_session(artifacts: Path) -> dict[str, Any]:
    path = session_path(artifacts)
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RigFailure("no transparent-audio session is active; run session-start") from exc
    except json.JSONDecodeError as exc:
        raise SafetyFailure(f"session checkpoint is malformed: {exc}") from exc
    if not isinstance(session, dict) or session.get("status") not in {"active", "restoring"}:
        raise RigFailure("transparent-audio session is not active")
    return session


def _preimage_script(paths: Sequence[str], recovery: str, card_number: int) -> str:
    encoded_paths = base64.b64encode(canonical_json(list(paths))).decode()
    return f'''set -euo pipefail
mkdir -p {shlex.quote(recovery)}
python3 - <<'PY'
import base64,hashlib,json,os,pathlib,stat
paths=json.loads(base64.b64decode({encoded_paths!r}))
result={{}}
for raw in paths:
 p=pathlib.Path(raw)
 if p.exists():
  data=p.read_bytes()
  result[raw]={{'exists':True,'content_b64':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),'mode':stat.S_IMODE(p.stat().st_mode)}}
 else:
  result[raw]={{'exists':False}}
pathlib.Path({(recovery + '/preimages.json')!r}).write_text(json.dumps(result,sort_keys=True))
print(json.dumps(result,sort_keys=True))
PY
alsactl -f {shlex.quote(recovery + '/mixer.state')} store {int(card_number)}
sha256sum {shlex.quote(recovery + '/preimages.json')} {shlex.quote(recovery + '/mixer.state')}
'''


def capture_preimages(
    backend: Backend,
    candidate: Candidate,
    session_id: str,
    instrument: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    recovery = f"{RECOVERY_ROOT}/{session_id}"
    paths = [OVERRIDE_PATH]
    paths.extend(f"{WP_DEPLOYED_DIR}/{name}" for name in sorted(candidate.policy_files))
    report = backend.pi(
        _preimage_script(paths, recovery, int(instrument["ephemeral_card_number"])),
        timeout=45,
    ).require("capture exact preimages")
    first_line = report.stdout.splitlines()[0] if report.stdout else ""
    try:
        preimages = json.loads(first_line)
    except json.JSONDecodeError as exc:
        raise SafetyFailure(f"preimage capture did not return its manifest: {exc}") from exc
    return preimages, recovery


def extend_preimages(
    backend: Backend,
    session: dict[str, Any],
    policy_names: Sequence[str],
) -> None:
    existing = session.get("preimages")
    if not isinstance(existing, dict):
        raise SafetyFailure("session preimage manifest is unavailable")
    new_paths: list[str] = []
    for name in policy_names:
        if "/" in name or name in {".", ".."}:
            raise SafetyFailure(f"unsafe WirePlumber policy filename: {name!r}")
        path = f"{WP_DEPLOYED_DIR}/{name}"
        if path not in existing:
            new_paths.append(path)
    if not new_paths:
        return
    recovery = str(session["recovery_root"])
    encoded_paths = base64.b64encode(canonical_json(new_paths)).decode()
    script = f'''python3 - <<'PY'
import base64,hashlib,json,os,pathlib,stat
manifest=pathlib.Path({(recovery + '/preimages.json')!r})
document=json.loads(manifest.read_text())
paths=json.loads(base64.b64decode({encoded_paths!r}))
for raw in paths:
 p=pathlib.Path(raw)
 if p.exists():
  data=p.read_bytes(); document[raw]={{'exists':True,'content_b64':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),'mode':stat.S_IMODE(p.stat().st_mode)}}
 else: document[raw]={{'exists':False}}
tmp=manifest.with_suffix('.json.new'); tmp.write_text(json.dumps(document,sort_keys=True)); os.replace(tmp,manifest)
print(json.dumps(document,sort_keys=True))
PY'''
    updated = parse_json_result(backend.pi(script, timeout=30), "extend exact policy preimages")
    session["preimages"] = updated


def _recovery_script(session_id: str, recovery: str, instrument: InstrumentSpec) -> str:
    """Script stored on the Pi and invoked by both stop and the deadman."""

    usb_id = instrument.usb_id
    port = instrument.port_path
    return f'''#!/bin/bash
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000
recovery={shlex.quote(recovery)}
python3 - <<'PY'
import base64,json,os,pathlib
root=pathlib.Path({recovery!r})
preimages=json.loads((root/'preimages.json').read_text())
for raw,item in preimages.items():
 p=pathlib.Path(raw)
 if item.get('exists'):
  data=base64.b64decode(item['content_b64'])
  p.parent.mkdir(parents=True,exist_ok=True)
  tmp=p.with_name(p.name+'.e19-restore')
  tmp.write_bytes(data); os.chmod(tmp,int(item.get('mode',420))); os.replace(tmp,p)
 else:
  try: p.unlink()
  except FileNotFoundError: pass
PY
python3 - <<'PY'
import glob,pathlib,re,subprocess
vid,pid={usb_id!r}.split(':'); wanted={port!r}; card=None
for node in glob.glob('/sys/bus/usb/devices/*'):
 p=pathlib.Path(node)
 try: match=(p/'idVendor').read_text().strip().lower()==vid and (p/'idProduct').read_text().strip().lower()==pid and p.name==wanted
 except OSError: continue
 if not match: continue
 for value in glob.glob('/sys/class/sound/card*'):
  q=pathlib.Path(value)
  if p.resolve() in q.resolve().parents: card=int(re.search(r'card(\\d+)$',value).group(1)); break
if card is not None: subprocess.run(['alsactl','-f',{(recovery + '/mixer.state')!r},'restore',str(card)],check=False)
PY
rm -rf {shlex.quote(f'{RUNTIME_ROOT}/{session_id}')}
systemctl --user daemon-reload
systemctl --user stop bridge-supervisor.service || true
systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
systemctl --user start bridge-supervisor.service
for i in $(seq 1 120); do
  systemctl --user is-active --quiet pipewire.service pipewire-pulse.service wireplumber.service bridge-supervisor.service && break
  sleep 0.25
done
systemctl --user is-active pipewire.service pipewire-pulse.service wireplumber.service bridge-supervisor.service
python3 - <<'PY'
import hashlib,json,pathlib
root=pathlib.Path({recovery!r}); before=json.loads((root/'preimages.json').read_text()); after={{}}; ok=True
for raw,item in before.items():
 p=pathlib.Path(raw); exists=p.exists(); observed=hashlib.sha256(p.read_bytes()).hexdigest() if exists else None
 match=exists==bool(item.get('exists')) and (not exists or observed==item.get('sha256'))
 after[raw]={{'exists':exists,'sha256':observed,'matches':match}}; ok=ok and match
document={{'session_id':{session_id!r},'restored':ok,'files':after}}
(root/'recovery-result.json').write_text(json.dumps(document,sort_keys=True))
print(json.dumps(document,sort_keys=True))
raise SystemExit(0 if ok else 1)
PY
'''


def install_recovery_script(
    backend: Backend,
    session_id: str,
    recovery: str,
    instrument: InstrumentSpec,
) -> None:
    content = _recovery_script(session_id, recovery, instrument).encode()
    encoded = base64.b64encode(content).decode()
    backend.pi(
        f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(recovery + '/restore.sh')}; "
        f"chmod 700 {shlex.quote(recovery + '/restore.sh')}",
        timeout=20,
    ).require("install recovery script")


def deadman_unit(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)
    return f"larkbridge-dev-deadman-{safe}"


def arm_deadman(backend: Backend, session_id: str, recovery: str, seconds: int = 900) -> None:
    if seconds < 60 or seconds > 3600:
        raise SafetyFailure("deadman must be between 60 and 3600 seconds")
    unit = deadman_unit(session_id)
    script = (
        f"if systemctl --user is-active --quiet {shlex.quote(unit)}.service; then "
        f"echo 'recovery is already running' >&2; exit 75; fi; "
        f"systemctl --user stop {shlex.quote(unit)}.timer 2>/dev/null || true; "
        f"systemctl --user reset-failed {shlex.quote(unit)}.timer {shlex.quote(unit)}.service 2>/dev/null || true; "
        f"systemd-run --user --collect --unit={shlex.quote(unit)} --on-active={seconds}s "
        f"/bin/bash {shlex.quote(recovery + '/restore.sh')}"
    )
    backend.pi(script, timeout=20).require("arm Pi-side recovery deadman")


def cancel_deadman(backend: Backend, session_id: str) -> None:
    unit = deadman_unit(session_id)
    backend.pi(
        f"systemctl --user stop {shlex.quote(unit)}.timer {shlex.quote(unit)}.service 2>/dev/null || true; "
        f"systemctl --user reset-failed {shlex.quote(unit)}.service 2>/dev/null || true",
        timeout=20,
    ).require("cancel recovery deadman")


def _write_remote_file(path: str, content: bytes) -> str:
    encoded = base64.b64encode(content).decode()
    temporary = path + ".e19-new"
    return (
        f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}; "
        f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(temporary)}; "
        f"chmod 0644 {shlex.quote(temporary)}; mv -f {shlex.quote(temporary)} {shlex.quote(path)}"
    )


def stage_candidate_files(backend: Backend, candidate: Candidate, session_id: str) -> str:
    candidate_root = f"{RUNTIME_ROOT}/{session_id}/candidates/{candidate.candidate_id}"
    backend.pi(
        f"mkdir -p {shlex.quote(candidate_root)}; tar -xzf - -C {shlex.quote(candidate_root)}",
        timeout=60,
        stdin=candidate.package_tar,
    ).require("stage volatile supervisor candidate")
    verify = backend.pi(
        f"test -f {shlex.quote(candidate_root + '/bridge_supervisor.py')} && "
        f"sha256sum {shlex.quote(candidate_root + '/bridge_supervisor.py')}",
        timeout=15,
    )
    verify.require("verify staged supervisor")
    return candidate_root


def apply_candidate(
    backend: Backend,
    candidate: Candidate,
    session_id: str,
    restart_class: str,
    *,
    remove_policy_names: Sequence[str] = (),
) -> None:
    for name in (*candidate.policy_files.keys(), *remove_policy_names):
        if "/" in name or name in {".", ".."}:
            raise SafetyFailure(f"unsafe WirePlumber policy filename: {name!r}")
    candidate_root = stage_candidate_files(backend, candidate, session_id)
    override = (
        "[Service]\n"
        "ExecStart=\n"
        f"ExecStart=/usr/bin/python3 {candidate_root}/bridge_supervisor.py\n"
        f"Environment=LARKBRIDGE_DEV_CANDIDATE={candidate.candidate_id}\n"
    ).encode()
    commands = [_write_remote_file(OVERRIDE_PATH, override)]
    if restart_class == "audio-stack":
        for name, content in sorted(candidate.policy_files.items()):
            commands.append(_write_remote_file(f"{WP_DEPLOYED_DIR}/{name}", content))
        for name in sorted(remove_policy_names):
            commands.append(f"rm -f {shlex.quote(f'{WP_DEPLOYED_DIR}/{name}')}")
    commands.append("systemctl --user daemon-reload")
    if restart_class == "audio-stack":
        commands.extend(
            (
                "systemctl --user stop bridge-supervisor.service",
                "systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service",
                "systemctl --user start bridge-supervisor.service",
            )
        )
    elif restart_class == "supervisor":
        commands.append("systemctl --user restart bridge-supervisor.service")
    backend.pi("; ".join(commands), timeout=75).require(f"apply {restart_class} candidate")


def verify_runtime(backend: Backend, expected_volume: float) -> dict[str, Any]:
    snapshot, _elapsed = wait_for(
        backend,
        lambda item: all(
            item.get("services", {}).get("stdout", "").count("ActiveState=active") >= 4
            for _ in (0,)
        ),
        timeout=30,
        label="the rebuilt audio stack",
    )
    status = snapshot.get("status")
    if not isinstance(status, dict):
        raise RigFailure("supervisor status is unavailable after candidate restart")
    output = status.get("output") if isinstance(status.get("output"), dict) else {}
    observed = output.get("observed_volume", output.get("volume"))
    if observed is not None and not math.isclose(float(observed), expected_volume, abs_tol=0.011):
        raise RigFailure(
            f"AUX volume is {observed}, expected {expected_volume:.2f} after audio-stack restart"
        )
    bluetooth = str(snapshot.get("bluetooth", {}).get("stdout", ""))
    if "Audio Sink" not in bluetooth and "0000110b" not in bluetooth.lower():
        raise RigFailure("adapter does not advertise the A2DP sink role after restart")
    return snapshot


def restore_remote_session(backend: Backend, session: Mapping[str, Any]) -> dict[str, Any]:
    recovery = str(session["recovery_root"])
    result = backend.pi(
        f"/bin/bash {shlex.quote(recovery + '/restore.sh')}", timeout=120
    )
    report = parse_json_result(result, "restore development session")
    if not report.get("restored"):
        raise SafetyFailure(f"session preimages did not restore exactly: {report}")
    cancel_deadman(backend, str(session["session_id"]))
    return report


def generate_stimulus(
    path: Path,
    *,
    mode: str,
    seconds: float,
    dbfs: float = -12.0,
    channels: int | None = None,
) -> dict[str, Any]:
    selected_channels = channels if channels is not None else (2 if mode == "sine" else 1)
    command = [
        sys.executable,
        str(REPO / "tools" / "audio" / "tone_gen.py"),
        "--out",
        str(path),
        "--mode",
        mode,
        "--seconds",
        str(seconds),
        "--rate",
        str(GENERALPLUS_RATE),
        "--channels",
        str(selected_channels),
        "--dbfs",
        str(dbfs),
        "--lead-silence",
        "1",
        "--trail-silence",
        "1",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except OSError as exc:
        raise RigFailure(f"could not generate deterministic stimulus: {exc}") from exc
    if proc.returncode:
        raise RigFailure(f"stimulus generation failed: {proc.stderr.strip()}")
    data = path.read_bytes()
    return {
        "path": str(path),
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "mode": mode,
        "seconds": seconds,
        "rate": GENERALPLUS_RATE,
        "dbfs": dbfs,
        "channels": selected_channels,
    }


def _load_analysis_modules() -> tuple[Any, Any]:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from rig.analysis import glitch_detect, wav_level

    return wav_level, glitch_detect


def score_media(
    capture: Path,
    *,
    calibrated_noise_floor_dbfs: float,
    thresholds: Thresholds,
) -> dict[str, Any]:
    wav_level, glitch_detect = _load_analysis_modules()
    level = wav_level.analyse(str(capture), 1000.0, 1.5, 5.0)
    channels, rate = glitch_detect.read_wav(str(capture))
    if rate != GENERALPLUS_RATE or len(channels) != GENERALPLUS_CAPTURE_CHANNELS:
        raise RigFailure(
            f"capture format is {rate} Hz/{len(channels)}ch; expected 48000 Hz/1ch"
        )
    samples = channels[0][int(rate * 1.5) :]
    bursts, _floor = glitch_detect.hp_burst(samples, rate, 1000.0, 25.0, 5.0)
    steps = glitch_detect.step(samples, rate, 1000.0, 5.0)
    channel = level["per_channel"][0]
    signal_margin = float(channel.get("tone_dbfs", -200.0)) - calibrated_noise_floor_dbfs
    failures: list[str] = []
    if signal_margin < thresholds.aux_above_floor_db:
        failures.append(
            f"signal margin {signal_margin:.2f} dB is below {thresholds.aux_above_floor_db:.2f} dB"
        )
    if float(channel.get("clipped_pct", math.inf)) > thresholds.clipping_pct:
        failures.append("capture clipped")
    if bursts or steps:
        failures.append(
            f"detected discontinuities after startup (hp={len(bursts)}, step={len(steps)})"
        )
    return {
        "verdict": "PASS" if not failures else "FAIL",
        "capture": str(capture),
        "capture_sha256": sha256_bytes(capture.read_bytes()),
        "level": level,
        "signal_above_calibrated_floor_db": round(signal_margin, 2),
        "discontinuities": {"hp_burst": bursts, "step": steps},
        "thresholds": asdict(thresholds),
        "failures": failures,
    }


def score_call(
    backend: Backend,
    *,
    reference: Path,
    raw: Path,
    clean: Path,
    thresholds: Thresholds,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(RIG_ROOT / "analysis" / "aec_metrics.py"),
        "--reference",
        str(reference),
        "--raw",
        str(raw),
        "--clean",
        str(clean),
        "--signal",
        "speech",
        "--min-raw-tone-dbfs",
        str(thresholds.call_raw_dbfs),
        "--min-suppression-db",
        str(thresholds.aec_suppression_db),
    ]
    result = backend.local(command, cwd=REPO, timeout=120)
    try:
        metrics = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RigFailure(f"AEC scorer returned malformed JSON: {exc}: {result.stderr}") from exc
    if not isinstance(metrics, dict):
        raise RigFailure("AEC scorer did not return a JSON object")
    metrics["files"] = {
        "reference": {"path": str(reference), "sha256": sha256_bytes(reference.read_bytes())},
        "raw": {"path": str(raw), "sha256": sha256_bytes(raw.read_bytes())},
        "clean": {"path": str(clean), "sha256": sha256_bytes(clean.read_bytes())},
    }
    return metrics


def _upload(backend: Backend, local: Path, remote: str) -> None:
    backend.pi(
        f"mkdir -p {shlex.quote(str(PurePosixPath(remote).parent))}; cat > {shlex.quote(remote)}",
        timeout=90,
        stdin=local.read_bytes(),
    ).require(f"upload {local.name}")


def wait_unit_inactive(backend: Backend, unit: str, *, timeout: float) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        state = backend.pi(
            f"systemctl --user show {shlex.quote(unit)} --property=ActiveState --value",
            timeout=10,
        )
        if state.returncode:
            missing = (state.stderr + state.stdout).lower()
            if "not found" in missing or "could not be found" in missing:
                return
            state.require(f"query {unit}")
        if state.stdout.strip() in {"inactive", "failed"}:
            if state.stdout.strip() == "failed":
                detail = backend.pi(
                    f"systemctl --user status {shlex.quote(unit)} --no-pager", timeout=15
                )
                raise RigFailure(f"{unit} failed: {detail.stdout or detail.stderr}")
            return
        backend.wait(0.25)
    raise RigFailure(f"timed out waiting for {unit}")


def wait_unit_active(backend: Backend, unit: str, *, timeout: float = 10.0) -> None:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        state = backend.pi(
            f"systemctl --user show {shlex.quote(unit)} --property=ActiveState --value",
            timeout=10,
        )
        if state.returncode == 0 and state.stdout.strip() == "active":
            return
        if state.returncode and "not found" not in (state.stderr + state.stdout).lower():
            state.require(f"query {unit}")
        backend.wait(0.1)
    raise RigFailure(f"timed out waiting for {unit} to start")


def wait_phone_transport(
    backend: Backend, expected: str, *, timeout: float
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() - started < timeout:
        result = backend.pi(f"cat {STATUS_PATH}", timeout=10)
        result.require("read supervisor phone transport")
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RigFailure(f"supervisor status is malformed: {exc}") from exc
        phone = status.get("phone") if isinstance(status, dict) else None
        last = phone if isinstance(phone, dict) else {}
        if last.get("transport") == expected:
            return last, time.monotonic() - started
        backend.wait(0.1)
    raise RigFailure(
        f"timed out after {timeout:g}s waiting for phone transport {expected}; last={last}"
    )


def media_smoke(
    backend: Backend,
    inventory: Inventory,
    instrument: Mapping[str, Any],
    artifact: Path,
    *,
    seconds: float,
    calibration: Mapping[str, Any],
) -> dict[str, Any]:
    artifact.mkdir(parents=True, exist_ok=True)
    stimulus_path = artifact / "stimulus.wav"
    stimulus = generate_stimulus(stimulus_path, mode="sine", seconds=seconds)
    package = backend.adb(("shell", "pm", "path", "org.videolan.vlc"), timeout=20)
    if package.returncode or "package:" not in package.stdout:
        raise HardwareRequired("VLC (org.videolan.vlc) is not installed on the Pixel")
    pushed = backend.adb(("push", str(stimulus_path), PHONE_MEDIA_REMOTE), timeout=90)
    pushed.require("push hashed media stimulus to Pixel")
    remote_capture = f"{RUNTIME_ROOT}/capture-media-{stamp()}.wav"
    unit = "larkbridge-e19-media-capture"
    alsa_id = str(instrument["alsa_id"])
    capture_seconds = math.ceil(seconds + 5)
    start = backend.pi(
        f"systemctl --user reset-failed {unit}.service 2>/dev/null || true; "
        f"systemd-run --user --unit={unit} --collect /usr/bin/arecord "
        f"-D {shlex.quote('plughw:CARD=' + alsa_id + ',DEV=0')} -q -t wav -f S16_LE "
        f"-r {inventory.instrument.rate} -c {inventory.instrument.capture_channels} "
        f"-d {capture_seconds} {shlex.quote(remote_capture)}",
        timeout=20,
    )
    start.require("start GeneralPlus AUX capture")
    launched = backend.adb(
        (
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            "file://" + PHONE_MEDIA_REMOTE,
            "-t",
            "audio/wav",
            "-p",
            "org.videolan.vlc",
        ),
        timeout=30,
    )
    launched.require("launch the installed VLC explicitly")
    active_status, active_elapsed = wait_phone_transport(
        backend, "MEDIA_ACTIVE", timeout=3.0
    )
    wait_unit_inactive(backend, unit + ".service", timeout=capture_seconds + 20)
    capture = artifact / "aux-capture.wav"
    backend.fetch(remote_capture, capture)
    metrics = score_media(
        capture,
        calibrated_noise_floor_dbfs=float(
            calibration["stages"]["self-loop"]["noise_floor_dbfs"]
        ),
        thresholds=inventory.thresholds,
    )
    return {
        "stimulus": stimulus,
        "vlc_launch": asdict(launched),
        "media_active_after_launch_s": round(active_elapsed, 3),
        "active_phone_status": active_status,
        "metrics": metrics,
    }


def _find_call_wavs(directory: Path) -> tuple[Path, Path, Path]:
    matches: dict[str, Path] = {}
    for path in directory.rglob("*.wav"):
        name = path.name.lower()
        for role in ("reference", "raw", "clean"):
            if role in name:
                matches.setdefault(role, path)
    missing = [role for role in ("reference", "raw", "clean") if role not in matches]
    if missing:
        raise RigFailure("call capture is missing taps: " + ", ".join(missing))
    return matches["reference"], matches["raw"], matches["clean"]


def call_smoke(
    backend: Backend,
    inventory: Inventory,
    instrument: Mapping[str, Any],
    artifact: Path,
    *,
    seconds: float,
) -> dict[str, Any]:
    before = capture_snapshot(backend)
    phone = status_phone(before)
    if before.get("status", {}).get("state") != "ACTIVE" or phone.get("transport") != "CALL":
        raise HardwareRequired(
            "start a Discord call on the Pixel and select LarkBridge Bluetooth audio, then rerun"
        )
    android_audio = str(before.get("android", {}).get("audio", {}).get("stdout", ""))
    if "MODE_IN_COMMUNICATION" not in android_audio and "mode: 3" not in android_audio.lower():
        raise HardwareRequired("Discord is not confirmed as owning Android communication mode")
    artifact.mkdir(parents=True, exist_ok=True)
    stimulus_path = artifact / "near-end-stimulus.wav"
    stimulus = generate_stimulus(stimulus_path, mode="speech", seconds=seconds)
    remote_root = f"{RUNTIME_ROOT}/call-{stamp()}"
    remote_stimulus = remote_root + "/stimulus.wav"
    _upload(backend, stimulus_path, remote_stimulus)
    capture_unit = "larkbridge-e19-call-capture"
    play_unit = "larkbridge-e19-call-stimulus"
    capture_script = (
        f"mkdir -p {shlex.quote(remote_root)}; "
        f"python3 /home/admin/rpi-lark-bridge/rig/pi/measure/call_capture.py "
        f"--label quick --seconds {seconds:g} --mode echo --outdir {shlex.quote(remote_root)}"
    )
    backend.pi(
        f"systemd-run --user --unit={capture_unit} --collect /bin/bash -lc "
        f"{shlex.quote(capture_script)}",
        timeout=20,
    ).require("start post-AEC call capture")
    alsa_id = str(instrument["alsa_id"])
    backend.pi(
        f"systemd-run --user --unit={play_unit} --collect /usr/bin/aplay -q "
        f"-D {shlex.quote('plughw:CARD=' + alsa_id + ',DEV=0')} {shlex.quote(remote_stimulus)}",
        timeout=20,
    ).require("play GeneralPlus near-end acoustic stimulus")
    wait_unit_inactive(backend, capture_unit + ".service", timeout=seconds + 30)
    local_capture = artifact / "call-capture"
    backend.fetch(remote_root, local_capture, recursive=True)
    reference, raw, clean = _find_call_wavs(local_capture)
    metrics = score_call(
        backend,
        reference=reference,
        raw=raw,
        clean=clean,
        thresholds=inventory.thresholds,
    )
    return {"stimulus": stimulus, "metrics": metrics}


def _calibration_capture(
    backend: Backend,
    *,
    capture_command: str,
    playback_command: str | None,
    remote_capture: str,
    local_capture: Path,
    duration: float,
) -> None:
    token = f"{time.time_ns() & 0xFFFFFF:x}"
    capture_unit = f"larkbridge-e19-cal-capture-{token}"
    play_unit = f"larkbridge-e19-cal-play-{token}"
    try:
        backend.pi(
            f"systemd-run --user --unit={capture_unit} --collect /bin/bash -lc "
            f"{shlex.quote(capture_command)}",
            timeout=20,
        ).require("start calibration capture")
        wait_unit_active(backend, capture_unit + ".service")
        if playback_command is not None:
            backend.pi(
                f"systemd-run --user --unit={play_unit} --collect /bin/bash -lc "
                f"{shlex.quote(playback_command)}",
                timeout=20,
            ).require("start calibration playback")
        wait_unit_inactive(backend, capture_unit + ".service", timeout=duration + 20)
        backend.fetch(remote_capture, local_capture)
    finally:
        backend.pi(
            f"systemctl --user stop {capture_unit}.service {play_unit}.service 2>/dev/null || true",
            timeout=15,
        ).require("clean up calibration capture units")


def _analyse_tone(path: Path) -> dict[str, Any]:
    wav_level, _glitch = _load_analysis_modules()
    report = wav_level.analyse(str(path), 1000.0, 1.5, 5.0)
    channels = report.get("per_channel") or []
    if len(channels) != 1:
        raise RigFailure(f"calibration capture {path} is not mono")
    return dict(channels[0])


def _analyse_silence(path: Path) -> dict[str, Any]:
    wav_level, _glitch = _load_analysis_modules()
    report = wav_level.analyse(str(path), None, 0.5, 0.0)
    channels = report.get("per_channel") or []
    if len(channels) != 1:
        raise RigFailure(f"calibration capture {path} is not mono")
    return dict(channels[0])


def self_loop_metrics(
    silence: Path, tones: Mapping[float, Path]
) -> dict[str, Any]:
    quiet = _analyse_silence(silence)
    measured = {level: _analyse_tone(path) for level, path in tones.items()}
    offsets = [float(item["tone_dbfs"]) - level for level, item in measured.items()]
    mean_offset = sum(offsets) / len(offsets)
    linearity_error = max(abs(value - mean_offset) for value in offsets)
    loudest = max(float(item["tone_dbfs"]) for item in measured.values())
    clipped = max(
        [float(quiet.get("clipped_pct", 0))]
        + [float(item.get("clipped_pct", 0)) for item in measured.values()]
    )
    return {
        "noise_floor_dbfs": float(quiet["rms_dbfs"]),
        "linearity_error_db": round(linearity_error, 3),
        "dynamic_range_db": round(loudest - float(quiet["rms_dbfs"]), 3),
        "clipped_pct": clipped,
        "levels": {str(level): item for level, item in sorted(measured.items())},
        "silence": quiet,
    }


def calibration_stage_failures(
    stage: str, metrics: Mapping[str, Any], thresholds: Thresholds
) -> list[str]:
    failures: list[str] = []
    if float(metrics.get("clipped_pct", math.inf)) > thresholds.clipping_pct:
        failures.append(f"clipping exceeds {thresholds.clipping_pct:.3f}%")
    if stage == "self-loop":
        if float(metrics.get("noise_floor_dbfs", math.inf)) > thresholds.noise_floor_dbfs:
            failures.append(f"noise floor exceeds {thresholds.noise_floor_dbfs:.1f} dBFS")
        if float(metrics.get("linearity_error_db", math.inf)) > thresholds.linearity_error_db:
            failures.append(f"linearity error exceeds {thresholds.linearity_error_db:.1f} dB")
        if float(metrics.get("dynamic_range_db", -math.inf)) < thresholds.dynamic_range_db:
            failures.append(f"dynamic range is below {thresholds.dynamic_range_db:.1f} dB")
    elif stage == "aux-loop":
        if float(metrics.get("above_floor_db", -math.inf)) < thresholds.aux_above_floor_db:
            failures.append(f"AUX signal margin is below {thresholds.aux_above_floor_db:.1f} dB")
    elif float(metrics.get("snr_db", -math.inf)) < thresholds.acoustic_snr_db:
        failures.append(f"acoustic SNR is below {thresholds.acoustic_snr_db:.1f} dB")
    return failures


def _instrument_pcm(spec: InstrumentSpec, instrument: Mapping[str, Any]) -> str:
    return f"plughw:CARD={instrument['alsa_id']},DEV=0"


def perform_calibration_stage(
    backend: Backend,
    inventory: Inventory,
    instrument: Mapping[str, Any],
    stage: str,
    artifact: Path,
    existing: Mapping[str, Any],
) -> dict[str, Any]:
    artifact.mkdir(parents=True, exist_ok=False)
    remote_root = f"{RUNTIME_ROOT}/calibration-{stamp()}-{stage}"
    backend.pi(f"mkdir -p {shlex.quote(remote_root)}", timeout=15).require(
        "create calibration runtime directory"
    )
    pcm = _instrument_pcm(inventory.instrument, instrument)
    capture_duration = 7
    if stage == "self-loop":
        silence = artifact / "silence.wav"
        remote_silence = remote_root + "/silence.wav"
        _calibration_capture(
            backend,
            capture_command=(
                f"arecord -D {shlex.quote(pcm)} -q -t wav -f S16_LE -r 48000 -c 1 "
                f"-d {capture_duration} {shlex.quote(remote_silence)}"
            ),
            playback_command=None,
            remote_capture=remote_silence,
            local_capture=silence,
            duration=capture_duration,
        )
        tones: dict[float, Path] = {}
        for level in (-36.0, -24.0, -12.0):
            label = str(abs(int(level)))
            stimulus = artifact / f"stimulus-minus-{label}.wav"
            generate_stimulus(
                stimulus, mode="sine", seconds=4, dbfs=level, channels=2
            )
            remote_stimulus = remote_root + f"/stimulus-minus-{label}.wav"
            remote_capture = remote_root + f"/capture-minus-{label}.wav"
            _upload(backend, stimulus, remote_stimulus)
            local_capture = artifact / f"capture-minus-{label}.wav"
            _calibration_capture(
                backend,
                capture_command=(
                    f"arecord -D {shlex.quote(pcm)} -q -t wav -f S16_LE -r 48000 -c 1 "
                    f"-d {capture_duration} {shlex.quote(remote_capture)}"
                ),
                playback_command=f"aplay -q -D {shlex.quote(pcm)} {shlex.quote(remote_stimulus)}",
                remote_capture=remote_capture,
                local_capture=local_capture,
                duration=capture_duration,
            )
            tones[level] = local_capture
        return self_loop_metrics(silence, tones)

    previous_stages = existing.get("stages")
    if not isinstance(previous_stages, dict) or "self-loop" not in previous_stages:
        raise HardwareRequired("record a passing self-loop calibration before this stage")
    floor = float(previous_stages["self-loop"]["noise_floor_dbfs"])
    stimulus = artifact / "stimulus.wav"
    generate_stimulus(stimulus, mode="sine", seconds=4, dbfs=-12, channels=2)
    remote_stimulus = remote_root + "/stimulus.wav"
    remote_capture = remote_root + "/capture.wav"
    local_capture = artifact / "capture.wav"
    _upload(backend, stimulus, remote_stimulus)
    if stage == "aux-loop":
        current = capture_snapshot(backend)
        target = status_phone(current).get("expected_target")
        if not target:
            raise HardwareRequired("supervisor status does not identify the configured AUX target")
        capture_command = (
            f"arecord -D {shlex.quote(pcm)} -q -t wav -f S16_LE -r 48000 -c 1 "
            f"-d {capture_duration} {shlex.quote(remote_capture)}"
        )
        playback_command = (
            f"pw-play --target {shlex.quote(str(target))} {shlex.quote(remote_stimulus)}"
        )
    else:
        current = capture_snapshot(backend)
        status = current.get("status") if isinstance(current.get("status"), dict) else {}
        endpoints = status.get("endpoints") if isinstance(status.get("endpoints"), dict) else {}
        microphone = endpoints.get("microphone")
        if not microphone:
            raise HardwareRequired("no selected Lark/FIFINE microphone is ready for acoustic calibration")
        capture_command = (
            f"timeout --signal=INT {capture_duration}s pw-record --target {shlex.quote(str(microphone))} "
            f"--rate 48000 --channels 1 --channel-map mono --format s16 {shlex.quote(remote_capture)}"
        )
        playback_command = f"aplay -q -D {shlex.quote(pcm)} {shlex.quote(remote_stimulus)}"
    _calibration_capture(
        backend,
        capture_command=capture_command,
        playback_command=playback_command,
        remote_capture=remote_capture,
        local_capture=local_capture,
        duration=capture_duration,
    )
    measured = _analyse_tone(local_capture)
    if stage == "aux-loop":
        return {
            "above_floor_db": round(float(measured["tone_dbfs"]) - floor, 3),
            "clipped_pct": float(measured.get("clipped_pct", 0)),
            "capture": measured,
        }
    return {
        "snr_db": float(measured.get("snr_db", -math.inf)),
        "clipped_pct": float(measured.get("clipped_pct", 0)),
        "capture": measured,
    }


def cache_candidate(artifacts: Path, candidate: Candidate) -> Path:
    target = artifacts / "candidates" / candidate.candidate_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "package.tar.gz").write_bytes(candidate.package_tar)
    policy_root = target / "policies"
    policy_root.mkdir(exist_ok=True)
    for name, content in candidate.policy_files.items():
        (policy_root / name).write_bytes(content)
    atomic_json(target / "manifest.json", candidate.manifest())
    return target


def load_cached_candidate(artifacts: Path, candidate_id: str) -> Candidate:
    root = artifacts / "candidates" / candidate_id
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        package = (root / "package.tar.gz").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyFailure(f"cached last-good candidate {candidate_id} is unavailable: {exc}") from exc
    policies: dict[str, bytes] = {}
    policy_root = root / "policies"
    if policy_root.is_dir():
        for path in policy_root.iterdir():
            if path.is_file():
                policies[path.name] = path.read_bytes()
    if sha256_bytes(package) != manifest.get("package_tar_sha256"):
        raise SafetyFailure(f"cached candidate {candidate_id} package hash changed")
    return Candidate(
        source=str(manifest["source"]),
        repository=Path(str(manifest["repository"])),
        revision=str(manifest["revision"]),
        candidate_id=str(manifest["candidate_id"]),
        diff_sha256=str(manifest["diff_sha256"]),
        content_sha256=str(manifest["content_sha256"]),
        changed_paths=tuple(str(item) for item in manifest.get("changed_paths", [])),
        untracked=tuple(dict(item) for item in manifest.get("untracked", [])),
        package_tar=package,
        policy_files=policies,
    )


def policy_restart_from_snapshot(snapshot: Mapping[str, Any], candidate: Candidate) -> str:
    deployed = snapshot.get("deployed_hashes")
    if not isinstance(deployed, dict):
        return "audio-stack"
    for name, content in candidate.policy_files.items():
        if deployed.get(f"{WP_DEPLOYED_DIR}/{name}") != sha256_bytes(content):
            return "audio-stack"
    return "supervisor"


def _write_artifact_manifest(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "evidence-manifest.json":
            continue
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_bytes(path.read_bytes()),
            }
        )
    document = {"created": utc_now(), "files": files}
    atomic_json(root / "evidence-manifest.json", document)
    return document


def _route_failures(mode: str, snapshot: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    status = snapshot.get("status")
    status = status if isinstance(status, dict) else {}
    phone = status_phone(snapshot)
    if mode == "media":
        if phone.get("transport") != "MEDIA_ACTIVE":
            failures.append(f"phone transport is {phone.get('transport')!r}, not MEDIA_ACTIVE")
        if phone.get("route_verified") is not True:
            failures.append("phone media route is not verified")
        count = phone.get("route_count")
        if count is not None and count != 1:
            failures.append(f"phone media has {count} routes, expected exactly one")
        target = str(phone.get("expected_target") or phone.get("target") or "")
        if target and "generalplus" in target.lower():
            failures.append("phone media targeted the GeneralPlus decoy output")
    else:
        if status.get("state") != "ACTIVE":
            failures.append(f"supervisor state is {status.get('state')!r}, not ACTIVE")
        if phone.get("transport") != "CALL":
            failures.append(f"phone transport is {phone.get('transport')!r}, not CALL")
        if phone.get("android_microphone_transport") is not True:
            failures.append("Android microphone transport is not verified open")
        uplinks = phone.get("microphone_uplink_count")
        if uplinks is not None and uplinks != 1:
            failures.append(f"phone has {uplinks} microphone uplinks, expected one")
        if phone.get("physical_microphone_bypass") is True:
            failures.append("physical microphone bypasses the post-AEC uplink")
    return failures


def _current_session_guard(session: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("boot_id") != session.get("baseline", {}).get("boot_id"):
        raise SafetyFailure(
            "Pi boot ID changed during the development session; run session-stop to reconcile preimages"
        )
    current = session.get("current_candidate")
    if isinstance(current, dict) and current.get("candidate_id"):
        expected = f"LARKBRIDGE_DEV_CANDIDATE={current['candidate_id']}"
        services = str(snapshot.get("services", {}).get("stdout", ""))
        if expected not in services:
            raise SafetyFailure(
                "volatile candidate marker is absent; the deadman may have restored the deployed baseline"
            )


def confirm_candidate_still_current(candidate: Candidate) -> None:
    refreshed = resolve_candidate(candidate.source, repository=candidate.repository)
    if refreshed.candidate_id != candidate.candidate_id:
        raise SafetyFailure(
            "candidate changed after focused tests; rerun so code, test result, and staged hash agree"
        )


def run_focused_tests(backend: Backend, candidate: Candidate) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "pi/bridged/tests",
        "-p",
        "test_phone_transport.py",
    ]
    source_path = Path(candidate.source).resolve()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    test_root = candidate.repository
    try:
        if not source_path.is_dir():
            temporary = tempfile.TemporaryDirectory(prefix="larkbridge-candidate-test-")
            test_root = Path(temporary.name)
            archive = _git(
                candidate.repository,
                "archive",
                "--format=tar",
                candidate.revision,
                binary=True,
            )
            assert isinstance(archive, bytes)
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as handle:
                for member in handle.getmembers():
                    relative = PurePosixPath(member.name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise SafetyFailure(f"unsafe path in candidate archive: {member.name}")
                    target = test_root.joinpath(*relative.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        stream = handle.extractfile(member)
                        if stream is None:
                            raise RigFailure(f"could not read {member.name} from candidate archive")
                        target.write_bytes(stream.read())
            result = backend.local(command, cwd=test_root, timeout=180)
        else:
            result = backend.local(command, cwd=test_root, timeout=180)
    finally:
        if temporary is not None:
            temporary.cleanup()
    document = {"command": command, **asdict(result)}
    if result.returncode:
        raise RigFailure("focused phone transport tests failed; candidate was not staged")
    return document


def command_baseline(arguments: argparse.Namespace, inventory: Inventory, backend: Backend) -> int:
    root = arguments.artifacts / "baselines" / stamp()
    root.mkdir(parents=True, exist_ok=False)
    snapshot = capture_snapshot(backend, full=True)
    instrument = probe_instrument(backend, inventory.instrument)
    validate_mixer_map(instrument, inventory.instrument)
    fingerprint = instrument_fingerprint(inventory, instrument)
    calibration_status: dict[str, Any]
    try:
        calibration = load_calibration(arguments.artifacts / CALIBRATION_FILE)
        validate_calibration(calibration, fingerprint, inventory.thresholds)
        calibration_status = {
            "valid": True,
            "sha256": sha256_bytes(canonical_json(calibration)),
        }
    except HardwareRequired as exc:
        calibration_status = {"valid": False, "reason": str(exc)}
    document = {
        "schema_version": 1,
        "created": utc_now(),
        "snapshot": snapshot,
        "instrument": instrument,
        "instrument_fingerprint": fingerprint,
        "calibration": calibration_status,
        "aux_volume_required": inventory.aux_volume,
    }
    atomic_json(root / "baseline.json", document)
    _write_artifact_manifest(root)
    atomic_json(arguments.artifacts / "latest-baseline.json", document)
    print(json.dumps({"baseline": str(root), "calibration": calibration_status}, indent=2))
    return 0


def command_session_start(arguments: argparse.Namespace, inventory: Inventory, backend: Backend) -> int:
    active = session_path(arguments.artifacts)
    if active.exists():
        try:
            existing = json.loads(active.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SafetyFailure(f"existing session checkpoint is malformed: {exc}") from exc
        if existing.get("status") in {"active", "restoring", "starting"}:
            raise SafetyFailure("a transparent-audio session is already active")
    candidate = resolve_candidate(arguments.candidate)
    cache_candidate(arguments.artifacts, candidate)
    baseline = capture_snapshot(backend, full=True)
    instrument = probe_instrument(backend, inventory.instrument)
    validate_mixer_map(instrument, inventory.instrument)
    fingerprint = instrument_fingerprint(inventory, instrument)
    calibration = load_calibration(arguments.artifacts / CALIBRATION_FILE)
    validate_calibration(calibration, fingerprint, inventory.thresholds)
    session_id = f"{stamp()}-{candidate.candidate_id}"
    preimages, recovery = capture_preimages(backend, candidate, session_id, instrument)
    session: dict[str, Any] = {
        "schema_version": 1,
        "status": "starting",
        "session_id": session_id,
        "started": utc_now(),
        "baseline": baseline,
        "instrument": instrument,
        "instrument_fingerprint": fingerprint,
        "calibration_sha256": sha256_bytes(canonical_json(calibration)),
        "recovery_root": recovery,
        "preimages": preimages,
        "current_candidate": candidate.manifest(),
        "last_good_candidate": candidate.candidate_id,
        "iterations": [],
    }
    atomic_json(active, session)
    install_recovery_script(backend, session_id, recovery, inventory.instrument)
    arm_deadman(backend, session_id, recovery, arguments.deadman)
    try:
        prepared = prepare_mixer(
            backend,
            inventory,
            instrument,
            capture_gain=str(calibration.get("capture_gain_request", "0%")),
        )
        session["instrument"] = prepared
        restart_class = policy_restart_from_snapshot(baseline, candidate)
        apply_candidate(backend, candidate, session_id, restart_class)
        verified = verify_runtime(backend, inventory.aux_volume)
        session["start_restart_class"] = restart_class
        session["start_snapshot"] = verified
        session["status"] = "active"
        atomic_json(active, session)
    except BaseException:
        with contextlib.suppress(Exception):
            restore_remote_session(backend, session)
        session["status"] = "start-failed-restored"
        atomic_json(active, session)
        raise
    print(json.dumps({"session": session_id, "candidate": candidate.candidate_id}, indent=2))
    return 0


def command_iterate(arguments: argparse.Namespace, inventory: Inventory, backend: Backend) -> int:
    session = load_session(arguments.artifacts)
    candidate = resolve_candidate(arguments.candidate)
    focused = run_focused_tests(backend, candidate)
    confirm_candidate_still_current(candidate)
    cache_candidate(arguments.artifacts, candidate)
    before = capture_snapshot(backend, full=True)
    _current_session_guard(session, before)
    if arguments.mode == "call":
        phone_before = status_phone(before)
        android_audio = str(before.get("android", {}).get("audio", {}).get("stdout", ""))
        if (
            before.get("status", {}).get("state") != "ACTIVE"
            or phone_before.get("transport") != "CALL"
            or (
                "MODE_IN_COMMUNICATION" not in android_audio
                and "mode: 3" not in android_audio.lower()
            )
        ):
            raise HardwareRequired(
                "start a Discord call on the Pixel and select LarkBridge Bluetooth audio, then rerun"
            )
    calibration = load_calibration(arguments.artifacts / CALIBRATION_FILE)
    validate_calibration(calibration, str(session["instrument_fingerprint"]), inventory.thresholds)
    current_instrument = probe_instrument(backend, inventory.instrument)
    if instrument_fingerprint(inventory, current_instrument) != session["instrument_fingerprint"]:
        raise HardwareRequired(
            "GeneralPlus identity/control map changed during the session; recalibration is required"
        )
    validate_prepared_mixer_state(
        current_instrument,
        inventory.instrument,
        expected_capture_value=str(
            session["instrument"].get("prepared_capture_gain_value", "0")
        ),
    )
    restart_class = classify_restart(session["current_candidate"], candidate)
    previous_policy_names = set(session["current_candidate"].get("policy_files", {}))
    candidate_policy_names = set(candidate.policy_files)
    extend_preimages(backend, session, sorted(candidate_policy_names))
    atomic_json(session_path(arguments.artifacts), session)
    iteration_root = arguments.artifacts / "iterations" / f"{stamp()}-{arguments.mode}-{candidate.candidate_id}"
    iteration_root.mkdir(parents=True, exist_ok=False)
    record: dict[str, Any] = {
        "started": utc_now(),
        "mode": arguments.mode,
        "candidate": candidate.manifest(),
        "restart_class": restart_class,
        "focused_tests": focused,
        "before": before,
        "status": "running",
    }
    atomic_json(iteration_root / "iteration.json", record)
    arm_deadman(
        backend,
        str(session["session_id"]),
        str(session["recovery_root"]),
        arguments.deadman,
    )
    try:
        apply_candidate(
            backend,
            candidate,
            str(session["session_id"]),
            restart_class,
            remove_policy_names=sorted(previous_policy_names - candidate_policy_names),
        )
        verify_runtime(backend, inventory.aux_volume)
        call_restored_s: float | None = None
        if arguments.mode == "call":
            _call_status, call_restored_s = wait_phone_transport(
                backend, "CALL", timeout=12.0
            )
        if arguments.mode == "media":
            smoke = media_smoke(
                backend,
                inventory,
                session["instrument"],
                iteration_root,
                seconds=arguments.seconds,
                calibration=calibration,
            )
        else:
            smoke = call_smoke(
                backend,
                inventory,
                session["instrument"],
                iteration_root,
                seconds=arguments.seconds,
            )
            smoke["call_restored_after_candidate_s"] = round(call_restored_s or 0.0, 3)
        after = capture_snapshot(backend, full=True)
        failures = _route_failures(arguments.mode, after)
        restarts = changed_restarts(before, after)
        allowed_restarts = {
            "none": set(),
            "supervisor": {"bridge-supervisor.service"},
            "audio-stack": {
                "bridge-supervisor.service",
                "pipewire.service",
                "pipewire-pulse.service",
                "wireplumber.service",
            },
        }[restart_class]
        unexpected_restarts = {
            unit: count
            for unit, count in restarts.items()
            if unit not in allowed_restarts
        }
        if unexpected_restarts:
            failures.append(f"unexpected service restarts: {unexpected_restarts}")
        recovery_delta = watchdog_recoveries(after) - watchdog_recoveries(before)
        if recovery_delta:
            failures.append(f"Bluetooth watchdog performed {recovery_delta} recoveries")
        kernel_errors = new_kernel_errors(before, after)
        if kernel_errors:
            failures.append(f"new USB/HCI errors: {kernel_errors}")
        metrics = smoke.get("metrics") if isinstance(smoke, dict) else {}
        if isinstance(metrics, dict) and metrics.get("verdict") != "PASS":
            failures.extend(str(item) for item in metrics.get("failures", ["measurement failed"]))
        record.update(
            finished=utc_now(),
            smoke=smoke,
            after=after,
            restart_deltas=restarts,
            watchdog_recovery_delta=recovery_delta,
            new_kernel_errors=kernel_errors,
            failures=failures,
            status="passed" if not failures else "failed",
        )
        atomic_json(iteration_root / "iteration.json", record)
        _write_artifact_manifest(iteration_root)
        if failures:
            raise RigFailure("; ".join(failures))
        session["current_candidate"] = candidate.manifest()
        session["last_good_candidate"] = candidate.candidate_id
        session["iterations"].append(
            {
                "mode": arguments.mode,
                "candidate_id": candidate.candidate_id,
                "status": "passed",
                "artifact": str(iteration_root),
            }
        )
        atomic_json(session_path(arguments.artifacts), session)
        print(json.dumps({"verdict": "PASS", "artifact": str(iteration_root)}, indent=2))
        return 0
    except BaseException as exc:
        record.update(finished=utc_now(), status="failed", error=f"{type(exc).__name__}: {exc}")
        atomic_json(iteration_root / "iteration.json", record)
        _write_artifact_manifest(iteration_root)
        last_good = load_cached_candidate(arguments.artifacts, str(session["last_good_candidate"]))
        rollback_class = classify_restart(candidate.manifest(), last_good)
        try:
            apply_candidate(
                backend,
                last_good,
                str(session["session_id"]),
                rollback_class,
                remove_policy_names=sorted(
                    set(candidate.policy_files) - set(last_good.policy_files)
                ),
            )
            verify_runtime(backend, inventory.aux_volume)
        except Exception as rollback_exc:  # noqa: BLE001 - any rollback defect restores baseline
            restore_remote_session(backend, session)
            session["status"] = "iteration-failed-baseline-restored"
            session["rollback_error"] = f"{type(rollback_exc).__name__}: {rollback_exc}"
            atomic_json(session_path(arguments.artifacts), session)
            raise SafetyFailure(
                f"candidate failed and last-good rollback failed; deployed baseline restored: {rollback_exc}"
            ) from exc
        session["current_candidate"] = last_good.manifest()
        session["iterations"].append(
            {
                "mode": arguments.mode,
                "candidate_id": candidate.candidate_id,
                "status": "failed-rolled-back",
                "artifact": str(iteration_root),
            }
        )
        atomic_json(session_path(arguments.artifacts), session)
        raise


def command_transition(arguments: argparse.Namespace, inventory: Inventory, backend: Backend) -> int:
    session = load_session(arguments.artifacts)
    before = capture_snapshot(backend, full=True)
    _current_session_guard(session, before)
    if arguments.expect == "call" and status_phone(before).get("transport") != "CALL":
        raise HardwareRequired(
            "start a Discord call on the Pixel and select LarkBridge Bluetooth audio, then rerun transition"
        )
    expected = {
        "media": "MEDIA_ACTIVE",
        "call": "CALL",
        "paused": "MEDIA_RESTORED_APP_PAUSED",
    }[arguments.expect]
    after, elapsed = wait_for(
        backend,
        lambda item: status_phone(item).get("transport") == expected,
        timeout=arguments.timeout,
        label=expected,
    )
    root = arguments.artifacts / "transitions" / f"{stamp()}-{arguments.expect}"
    root.mkdir(parents=True, exist_ok=False)
    document = {
        "created": utc_now(),
        "expect": arguments.expect,
        "expected_transport": expected,
        "elapsed_s": round(elapsed, 3),
        "before": before,
        "after": after,
    }
    atomic_json(root / "transition.json", document)
    _write_artifact_manifest(root)
    print(json.dumps({"transition": expected, "elapsed_s": round(elapsed, 3)}, indent=2))
    return 0


def command_session_stop(arguments: argparse.Namespace, _inventory: Inventory, backend: Backend) -> int:
    session = load_session(arguments.artifacts)
    session["status"] = "restoring"
    atomic_json(session_path(arguments.artifacts), session)
    report = restore_remote_session(backend, session)
    session["status"] = "stopped"
    session["stopped"] = utc_now()
    session["restore"] = report
    atomic_json(session_path(arguments.artifacts), session)
    print(json.dumps(report, indent=2))
    return 0


def command_calibrate(arguments: argparse.Namespace, inventory: Inventory, backend: Backend) -> int:
    if not arguments.hardware_ready:
        instructions = {
            "self-loop": "connect GeneralPlus output directly to its input",
            "aux-loop": "connect Pi AUX output to the GeneralPlus input",
            "acoustic": "connect GeneralPlus output to the fixed speaker and keep the speaker/microphone position fixed",
        }[arguments.stage]
        raise HardwareRequired(f"{instructions}; then repeat with --hardware-ready")
    report = probe_instrument(backend, inventory.instrument)
    validate_mixer_map(report, inventory.instrument)
    fingerprint = instrument_fingerprint(inventory, report)
    path = arguments.artifacts / CALIBRATION_FILE
    if path.exists():
        document = load_calibration(path)
        if document.get("instrument_fingerprint") != fingerprint:
            document = {
                "schema_version": 1,
                "instrument_fingerprint": fingerprint,
                "invalidated_previous": True,
                "stages": {},
            }
    else:
        document = {
            "schema_version": 1,
            "instrument_fingerprint": fingerprint,
            "stages": {},
        }
    requested_gain = arguments.capture_gain
    if arguments.stage == "self-loop":
        requested_gain = requested_gain or "0%"
        if not re.fullmatch(r"(?:100|[0-9]{1,2})%", requested_gain):
            raise RigFailure("--capture-gain must be a percentage from 0% through 100%")
        if document.get("capture_gain_request") not in (None, requested_gain):
            document["stages"] = {}
            document["capture_gain_changed"] = True
        document["capture_gain_request"] = requested_gain
    else:
        calibrated_gain = document.get("capture_gain_request")
        if calibrated_gain is None:
            raise HardwareRequired("record self-loop calibration to select capture gain first")
        if requested_gain is not None and requested_gain != calibrated_gain:
            raise HardwareRequired(
                f"this calibration is bound to capture gain {calibrated_gain}; "
                "rerun self-loop to change it"
            )
        requested_gain = str(calibrated_gain)
    artifact = arguments.artifacts / "calibration" / f"{stamp()}-{arguments.stage}"
    guard_id, recovery = start_mixer_guard(
        backend, report, inventory.instrument, seconds=600
    )
    restore: dict[str, Any] | None = None
    try:
        prepared = prepare_mixer(
            backend,
            inventory,
            report,
            capture_gain=str(requested_gain),
        )
        stage_metrics = perform_calibration_stage(
            backend,
            inventory,
            prepared,
            arguments.stage,
            artifact,
            document,
        )
    finally:
        restore = stop_mixer_guard(backend, guard_id, recovery)
    stage_metrics["recorded"] = utc_now()
    stage_metrics["artifact"] = str(artifact)
    document["instrument"] = {
        "usb_id": inventory.instrument.usb_id,
        "port_path": inventory.instrument.port_path,
        "mixer_map_sha256": mixer_map_sha256(str(report.get("mixer_contents", ""))),
        "cable_id": inventory.cable_id,
        "speaker_position_id": inventory.speaker_position_id,
        "aux_volume": inventory.aux_volume,
    }
    document["stages"][arguments.stage] = stage_metrics
    document["last_mixer_restore"] = restore
    document["updated"] = utc_now()
    stage_failures = calibration_stage_failures(
        arguments.stage, stage_metrics, inventory.thresholds
    )
    atomic_json(path, document)
    atomic_json(artifact / "metrics.json", stage_metrics)
    _write_artifact_manifest(artifact)
    valid = False
    reason = "remaining stages have not been recorded"
    try:
        validate_calibration(document, fingerprint, inventory.thresholds)
        valid, reason = True, "all calibration gates pass"
    except HardwareRequired as exc:
        reason = str(exc)
    print(
        json.dumps(
            {
                "recorded_stage": arguments.stage,
                "calibration": str(path),
                "valid": valid,
                "status": reason,
                "stage_verdict": "PASS" if not stage_failures else "FAIL",
                "stage_failures": stage_failures,
            },
            indent=2,
        )
    )
    return 0 if not stage_failures else 1


def command_accept(arguments: argparse.Namespace, _inventory: Inventory, _backend: Backend | None) -> int:
    session = load_session(arguments.artifacts)
    passed: dict[str, Mapping[str, Any]] = {}
    for item in session.get("iterations", []):
        if isinstance(item, dict) and item.get("status") == "passed":
            passed[str(item.get("mode"))] = item
    missing = [mode for mode in ("media", "call") if mode not in passed]
    if missing:
        raise RigFailure("cannot accept: no passing " + " and ".join(missing) + " iteration")
    candidate_ids = {str(item.get("candidate_id")) for item in passed.values()}
    if len(candidate_ids) != 1:
        raise RigFailure("cannot accept: latest media and call passes used different candidates")
    candidate_id = candidate_ids.pop()
    files: list[dict[str, Any]] = []
    for mode in ("media", "call"):
        artifact = Path(str(passed[mode]["artifact"]))
        manifest = artifact / "evidence-manifest.json"
        files.append(
            {
                "mode": mode,
                "artifact": str(artifact),
                "manifest_sha256": sha256_bytes(manifest.read_bytes()),
            }
        )
    acceptance = {
        "schema_version": 1,
        "accepted": utc_now(),
        "session_id": session["session_id"],
        "candidate_id": candidate_id,
        "inner_loop": files,
        "discord_far_end": None,
        "note": "Discord far-end milestone evidence is intentionally separate from synthetic inner-loop acceptance.",
    }
    milestones = sorted((arguments.artifacts / "milestones").glob("*/milestone.json"))
    if milestones:
        latest = milestones[-1]
        document = json.loads(latest.read_text(encoding="utf-8"))
        if document.get("candidate_id") == candidate_id and document.get("verdict") == "PASS":
            acceptance["discord_far_end"] = {
                "artifact": str(latest.parent),
                "manifest_sha256": sha256_bytes(
                    (latest.parent / "evidence-manifest.json").read_bytes()
                ),
            }
    target = arguments.artifacts / "accepted" / f"{stamp()}-{candidate_id}"
    target.mkdir(parents=True, exist_ok=False)
    atomic_json(target / "acceptance.json", acceptance)
    _write_artifact_manifest(target)
    print(json.dumps({"accepted": candidate_id, "artifact": str(target)}, indent=2))
    return 0


def _correlate_far_end(clean: Path, far_end: Path) -> dict[str, Any]:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from rig.analysis.aec_metrics import correlated_level, load_energy_envelope

    reference, reference_rate = load_energy_envelope(clean)
    observed, observed_rate = load_energy_envelope(far_end)
    if reference_rate != observed_rate:
        raise RigFailure(
            f"Pi post-AEC and Discord far-end analysis rates differ ({reference_rate}/{observed_rate})"
        )
    level, lag, correlation = correlated_level(reference, observed, reference_rate, max_lag_s=5.0)
    return {
        "correlated_level": level,
        "lag_ms": round(lag * 1000.0 / reference_rate, 2),
        "correlation": round(correlation, 4),
        "clean_sha256": sha256_bytes(clean.read_bytes()),
        "far_end_sha256": sha256_bytes(far_end.read_bytes()),
    }


def command_milestone(arguments: argparse.Namespace, inventory: Inventory, backend: Backend) -> int:
    session = load_session(arguments.artifacts)
    snapshot = capture_snapshot(backend)
    _current_session_guard(session, snapshot)
    if (
        snapshot.get("status", {}).get("state") != "ACTIVE"
        or status_phone(snapshot).get("transport") != "CALL"
    ):
        raise HardwareRequired(
            "start a Discord call with a consenting far-end participant and select LarkBridge audio"
        )
    if not arguments.operator_confirmed_input_muted:
        raise HardwareRequired(
            "mute the Windows Discord input so the far-end recording can only contain the Pi uplink, "
            "then repeat with --operator-confirmed-input-muted"
        )
    command_template = list(inventory.far_end_capture_command)
    if not command_template and arguments.ffmpeg_device:
        command_template = [
            "ffmpeg",
            "-y",
            "-f",
            "dshow",
            "-i",
            f"audio={arguments.ffmpeg_device}",
            "-t",
            "{seconds}",
            "-ac",
            "1",
            "-ar",
            "48000",
            "{out}",
        ]
    if not command_template:
        raise HardwareRequired(
            "configure discord_far_end_capture_command in rig/inventory.toml or pass --ffmpeg-device"
        )
    root = arguments.artifacts / "milestones" / f"{stamp()}-discord-far-end"
    root.mkdir(parents=True, exist_ok=False)
    far_end = root / "discord-far-end.wav"
    command = [
        token.replace("{out}", str(far_end)).replace("{seconds}", str(arguments.seconds))
        for token in command_template
    ]
    stimulus = generate_stimulus(root / "near-end-stimulus.wav", mode="speech", seconds=arguments.seconds)
    remote_root = f"{RUNTIME_ROOT}/milestone-{stamp()}"
    remote_stimulus = remote_root + "/stimulus.wav"
    _upload(backend, root / "near-end-stimulus.wav", remote_stimulus)
    instrument = probe_instrument(backend, inventory.instrument)
    capture_unit = "larkbridge-e19-milestone-capture"
    play_unit = "larkbridge-e19-milestone-stimulus"
    backend.pi(
        f"mkdir -p {shlex.quote(remote_root)}; systemd-run --user --unit={capture_unit} --collect "
        f"python3 /home/admin/rpi-lark-bridge/rig/pi/measure/call_capture.py --label milestone "
        f"--seconds {arguments.seconds:g} --mode echo --outdir {shlex.quote(remote_root)}",
        timeout=20,
    ).require("start Pi milestone taps")
    backend.pi(
        f"systemd-run --user --unit={play_unit} --collect /usr/bin/aplay -q "
        f"-D {shlex.quote('plughw:CARD=' + str(instrument['alsa_id']) + ',DEV=0')} "
        f"{shlex.quote(remote_stimulus)}",
        timeout=20,
    ).require("start milestone near-end stimulus")
    host_capture = backend.local(command, cwd=REPO, timeout=arguments.seconds + 30)
    host_capture.require("Windows Discord far-end loopback capture")
    wait_unit_inactive(backend, capture_unit + ".service", timeout=arguments.seconds + 30)
    pi_capture = root / "pi-call-capture"
    backend.fetch(remote_root, pi_capture, recursive=True)
    _reference, _raw, clean = _find_call_wavs(pi_capture)
    correlation = _correlate_far_end(clean, far_end)
    failures = [] if correlation["correlation"] >= 0.3 else [
        f"far-end correlation {correlation['correlation']:.4f} is below 0.3"
    ]
    document = {
        "schema_version": 1,
        "created": utc_now(),
        "candidate_id": session["current_candidate"]["candidate_id"],
        "kind": "discord-far-end",
        "separate_from_inner_loop": True,
        "operator_confirmed_windows_input_muted": True,
        "capture_command": command,
        "stimulus": stimulus,
        "correlation": correlation,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }
    atomic_json(root / "milestone.json", document)
    _write_artifact_manifest(root)
    print(json.dumps({"verdict": document["verdict"], "artifact": str(root)}, indent=2))
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    commands = parser.add_subparsers(dest="command", required=True)

    def live(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--live",
            action="store_true",
            help="authorize this invocation to contact and, where documented, mutate the bench",
        )

    baseline = commands.add_parser("baseline", help="capture a read-only dynamic baseline")
    live(baseline)

    start = commands.add_parser("session-start", help="start a guarded volatile candidate session")
    start.add_argument("--candidate", required=True)
    start.add_argument("--deadman", type=int, default=900)
    live(start)

    iterate = commands.add_parser("iterate", help="stage and score one candidate")
    iterate.add_argument("--mode", choices=("media", "call"), required=True)
    iterate.add_argument("--candidate", required=True)
    iterate.add_argument("--seconds", type=float, default=25.0)
    iterate.add_argument("--deadman", type=int, default=900)
    live(iterate)

    transition = commands.add_parser("transition", help="wait for a measured phone transport state")
    transition.add_argument("--expect", choices=("media", "call", "paused"), required=True)
    transition.add_argument("--timeout", type=float, default=12.0)
    live(transition)

    stop = commands.add_parser("session-stop", help="restore exact deployed preimages")
    live(stop)

    commands.add_parser("accept", help="write a compact accepted-candidate manifest")

    calibrate = commands.add_parser("calibrate", help="record one physically verified calibration stage")
    calibrate.add_argument("--stage", choices=REQUIRED_CALIBRATION_STAGES, required=True)
    calibrate.add_argument("--hardware-ready", action="store_true")
    calibrate.add_argument(
        "--capture-gain",
        help="explicit GeneralPlus capture gain; self-loop begins at 0% by default",
    )
    live(calibrate)

    milestone = commands.add_parser(
        "milestone", help="record a real Discord far end separately from the synthetic loop"
    )
    milestone.add_argument("--seconds", type=float, default=25.0)
    milestone.add_argument("--ffmpeg-device")
    milestone.add_argument("--operator-confirmed-input-muted", action="store_true")
    live(milestone)
    return parser


def main(argv: Sequence[str] | None = None, *, backend: Backend | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        inventory = Inventory.load(arguments.inventory)
        arguments.artifacts = arguments.artifacts.resolve()
        arguments.artifacts.mkdir(parents=True, exist_ok=True)
        if arguments.command == "accept":
            return command_accept(arguments, inventory, backend)
        selected_backend = backend
        if selected_backend is None:
            selected_backend = require_live(arguments, inventory)
        elif not arguments.live:
            raise HardwareRequired("tests must still pass --live to exercise a backend")
        handlers = {
            "baseline": command_baseline,
            "session-start": command_session_start,
            "iterate": command_iterate,
            "transition": command_transition,
            "session-stop": command_session_stop,
            "calibrate": command_calibrate,
            "milestone": command_milestone,
        }
        return handlers[arguments.command](arguments, inventory, selected_backend)
    except HardwareRequired as exc:
        print(
            json.dumps(
                {
                    "verdict": "HARDWARE_REQUIRED",
                    "action": str(exc),
                    "note": "No live action is attempted without --live and explicit hardware confirmation.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return EXIT_HARDWARE
    except RigFailure as exc:
        print(json.dumps({"verdict": "FAIL", "error": str(exc)}, indent=2), file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print(json.dumps({"verdict": "INTERRUPTED"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
