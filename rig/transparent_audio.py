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

The normal fast path runs ``quick-calibrate`` once after wiring AUX into the GeneralPlus
input, then reuses that gate for every RAM-only candidate. ``calibrate`` retains the
three-stage qualification used for promotion; it is not an iteration-time blocker.
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
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
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
QUICK_CALIBRATION_FILE = "generalplus-quick-calibration.json"

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
FIXED_AUX_TARGET = "alsa_output.platform-3f00b840.mailbox.stereo-fallback"
MEDIA_STIMULUS_FREQUENCY_HZ = 1000.0
MIN_MEDIA_TONE_TO_RESIDUAL_DB = 10.0

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
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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
    def local(
        self, command: Sequence[str], *, cwd: Path | None = None, timeout: float = 60
    ) -> CommandResult: ...

    def pi(
        self, script: str, *, timeout: float = 60, stdin: bytes | None = None
    ) -> CommandResult: ...

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

    def local(
        self, command: Sequence[str], *, cwd: Path | None = None, timeout: float = 60
    ) -> CommandResult:
        return self._run(command, cwd=cwd, timeout=timeout)

    def pi(
        self, script: str, *, timeout: float = 60, stdin: bytes | None = None
    ) -> CommandResult:
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
    quick_aux_above_floor_db: float = 20.0
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
    aux_target: str
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
            sidetone_control=str(
                data.get("generalplus_sidetone_control", "Mic Playback Switch")
            ),
            capture_switch_control=str(
                data.get("generalplus_capture_switch_control", "Mic Capture Switch")
            ),
            capture_volume_control=str(
                data.get("generalplus_capture_volume_control", "Mic Capture Volume")
            ),
        )
        thresholds = Thresholds(
            noise_floor_dbfs=float(data.get("e19_max_noise_floor_dbfs", -60.0)),
            linearity_error_db=float(data.get("e19_max_linearity_error_db", 1.5)),
            dynamic_range_db=float(data.get("e19_min_dynamic_range_db", 50.0)),
            quick_aux_above_floor_db=float(
                data.get("e19_min_quick_aux_above_floor_db", 20.0)
            ),
            aux_above_floor_db=float(data.get("e19_min_aux_above_floor_db", 40.0)),
            acoustic_snr_db=float(data.get("e19_min_acoustic_snr_db", 20.0)),
            clipping_pct=float(data.get("e19_max_clipping_pct", 0.01)),
            call_raw_dbfs=float(data.get("e19_min_call_raw_dbfs", -55.0)),
            aec_suppression_db=float(data.get("e19_min_aec_suppression_db", 10.0)),
        )
        aux_target = str(data.get("e19_aux_target", FIXED_AUX_TARGET))
        aux_volume = float(data.get("e19_aux_volume", 0.95))
        instrument_contract = (
            instrument.usb_id == GENERALPLUS_USB_ID
            and instrument.port_path == GENERALPLUS_PORT
            and instrument.rate == GENERALPLUS_RATE
            and instrument.capture_channels == GENERALPLUS_CAPTURE_CHANNELS
            and instrument.playback_channels == GENERALPLUS_PLAYBACK_CHANNELS
        )
        if not instrument_contract:
            raise SafetyFailure(
                "E19 requires GeneralPlus 1b3f:2008 at 1-1.5 with 48 kHz mono capture "
                "and stereo playback; the inventory cannot weaken this fixture contract"
            )
        if aux_target != FIXED_AUX_TARGET or not math.isclose(
            aux_volume, 0.95, abs_tol=1e-9
        ):
            raise SafetyFailure(
                "E19 requires the fixed Pi AUX target and volume 0.95; inventory overrides are not accepted"
            )
        gates_are_approved = (
            thresholds.noise_floor_dbfs <= -60.0
            and 0 <= thresholds.linearity_error_db <= 1.5
            and thresholds.dynamic_range_db >= 50.0
            and thresholds.quick_aux_above_floor_db >= 20.0
            and thresholds.aux_above_floor_db >= 40.0
            and thresholds.acoustic_snr_db >= 20.0
            and 0 <= thresholds.clipping_pct <= 0.01
            and -55.0 <= thresholds.call_raw_dbfs <= 0
            and thresholds.aec_suppression_db >= 10.0
        )
        if not gates_are_approved:
            raise SafetyFailure(
                "E19 acceptance thresholds are weaker than the approved contract or outside sane domains"
            )
        return cls(
            path=path,
            pi_host=str(data.get("pi_host", "larkbridge")),
            phone_serial=str(data.get("phone_serial", "")),
            pixel_bt_mac=str(data.get("pixel_bt_mac", "")),
            instrument=instrument,
            thresholds=thresholds,
            aux_target=aux_target,
            aux_volume=aux_volume,
            cable_id=str(data.get("generalplus_cable_id", "")),
            speaker_position_id=str(data.get("generalplus_speaker_position_id", "")),
            far_end_capture_command=tuple(
                str(item) for item in data.get("discord_far_end_capture_command", [])
            ),
        )


def locate_adb() -> str:
    explicit = os.environ.get("LARKBRIDGE_ADB") or os.environ.get("ADB_PATH")
    if explicit:
        resolved = shutil.which(explicit)
        candidate = Path(resolved) if resolved else Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        raise HardwareRequired(
            f"the explicit ADB executable does not exist: {candidate} "
            "(LARKBRIDGE_ADB/ADB_PATH)"
        )

    candidates = [RIG_ROOT / "adb" / "platform-tools" / "adb.exe"]
    for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if root := os.environ.get(variable):
            candidates.append(Path(root) / "platform-tools" / "adb.exe")
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        candidates.append(
            Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
        )
    with contextlib.suppress(RuntimeError):
        candidates.append(
            Path.home()
            / "AppData"
            / "Local"
            / "Android"
            / "Sdk"
            / "platform-tools"
            / "adb.exe"
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("adb")
    if found:
        return found
    raise HardwareRequired(
        "adb not found; run `rig setup-adb`, set LARKBRIDGE_ADB, or set ANDROID_SDK_ROOT"
    )


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


REMOTE_PROBE = r"""python3 - <<'PY'
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
PY"""


def render_remote_probe(spec: InstrumentSpec) -> str:
    return REMOTE_PROBE.replace("__USB_ID__", repr(spec.usb_id)).replace(
        "__PORT__", repr(spec.port_path)
    )


def probe_instrument(backend: Backend, spec: InstrumentSpec) -> dict[str, Any]:
    script = render_remote_probe(spec)
    report = parse_json_result(backend.pi(script, timeout=30), "GeneralPlus probe")
    if not report.get("ready"):
        raise HardwareRequired(
            str(report.get("reason") or "GeneralPlus instrument is not ready")
        )
    if report.get("mixer_returncode") != 0:
        raise HardwareRequired("GeneralPlus amixer control map is unavailable")
    validate_instrument_capabilities(report, spec)
    return report


def validate_instrument_capabilities(
    report: Mapping[str, Any], spec: InstrumentSpec
) -> None:
    raw = str(report.get("stream_capabilities", ""))
    playback, separator, capture = raw.partition("Capture:")
    if not separator:
        raise HardwareRequired(
            "GeneralPlus ALSA playback/capture capabilities are unavailable"
        )

    def qualified_altsetting(section: str, channels: int) -> bool:
        blocks = re.split(r"(?m)(?=^\s*Altset\s+\d+\s*$)", section)
        return any(
            re.search(r"(?m)^\s*Format:\s*S16_LE\s*$", block)
            and re.search(rf"(?m)^\s*Channels:\s*{channels}\s*$", block)
            and re.search(rf"(?m)^\s*Rates?:.*\b{spec.rate}\b", block)
            for block in blocks
            if re.search(r"(?m)^\s*Altset\s+\d+\s*$", block)
        )

    missing = []
    if not qualified_altsetting(playback, spec.playback_channels):
        missing.append(
            f"one playback S16_LE/{spec.playback_channels}ch/{spec.rate} Hz altsetting"
        )
    if not qualified_altsetting(capture, spec.capture_channels):
        missing.append(
            f"one capture S16_LE/{spec.capture_channels}ch/{spec.rate} Hz altsetting"
        )
    if missing:
        raise HardwareRequired(
            "GeneralPlus does not expose the qualified PCM contract: "
            + ", ".join(missing)
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
        "aux_target": inventory.aux_target,
        "aux_volume": inventory.aux_volume,
    }
    return sha256_bytes(canonical_json(identity))


def require_fixture_label(value: str, label: str) -> None:
    if not value.strip() or value.strip().upper().startswith("REPLACE_"):
        raise HardwareRequired(
            f"record a nonempty physical {label} label in rig/inventory.toml"
        )


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
    missing = [
        control for control in required if not _control_present(contents, control)
    ]
    if missing:
        raise HardwareRequired(
            "GeneralPlus mixer map has not been qualified; missing controls: "
            + ", ".join(missing)
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
        expected_capture_value is not None and gain != expected_capture_value
    ):
        drift.append(f"{spec.capture_volume_control}={gain!r}")
    if drift:
        raise HardwareRequired(
            "GeneralPlus mixer drifted from the calibrated safe state: "
            + ", ".join(drift)
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
            'amixer -D "$card" cset '
            + shlex.quote("name=" + name)
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
    prepared_value = mixer_value(
        result.stdout, inventory.instrument.capture_volume_control
    )
    if prepared_value is None:
        raise HardwareRequired(
            "GeneralPlus capture gain could not be verified after setup"
        )
    prepared["prepared_capture_gain_request"] = capture_gain
    prepared["prepared_capture_gain_value"] = prepared_value
    validate_prepared_mixer_state(
        prepared,
        inventory.instrument,
        expected_capture_value=prepared_value,
    )
    return prepared


def mixer_preimage_document(
    instrument: Mapping[str, Any], spec: InstrumentSpec
) -> dict[str, Any]:
    validate_mixer_map(instrument, spec)
    contents = str(instrument.get("mixer_contents", ""))
    names = (
        spec.agc_control,
        spec.sidetone_control,
        spec.capture_switch_control,
        spec.capture_volume_control,
    )
    controls = {name: mixer_value(contents, name) for name in names}
    missing = [name for name, value in controls.items() if value is None]
    if missing:
        raise HardwareRequired(
            "cannot snapshot GeneralPlus mixer controls: " + ", ".join(missing)
        )
    return {
        "schema_version": 1,
        "usb_id": spec.usb_id,
        "port_path": spec.port_path,
        "alsa_id_at_capture": instrument.get("alsa_id"),
        "ephemeral_card_number_at_capture": instrument.get("ephemeral_card_number"),
        "mixer_map_sha256": mixer_map_sha256(contents),
        "controls": controls,
    }


def _mixer_restore_command(
    recovery: str, instrument: InstrumentSpec, *, quiet: bool = False
) -> str:
    redirect = (
        f" > {shlex.quote(recovery + '/mixer-restore.log')} 2>&1" if quiet else ""
    )
    return f"""python3 - <<'PY'{redirect}
import glob,json,pathlib,re,subprocess
root=pathlib.Path({recovery!r}); preimage=json.loads((root/'mixer-preimage.json').read_text())
vid,pid={instrument.usb_id!r}.split(':'); wanted={instrument.port_path!r}; card=None
for node in glob.glob('/sys/bus/usb/devices/*'):
 p=pathlib.Path(node)
 try: match=(p/'idVendor').read_text().strip().lower()==vid and (p/'idProduct').read_text().strip().lower()==pid and p.name==wanted
 except OSError: continue
 if not match: continue
 for value in glob.glob('/sys/class/sound/card*'):
  q=pathlib.Path(value)
  if p.resolve() in q.resolve().parents: card=int(re.search(r'card(\\d+)$',value).group(1)); break
command_errors=[]; readback_errors=[]; observed={{}}
if card is None:
 readback_errors.append('instrument not found at its qualified port')
else:
 for name,value in preimage['controls'].items():
  try: proc=subprocess.run(['amixer','-c',str(card),'cset',f'name={{name}}',str(value)],capture_output=True,text=True)
  except OSError as exc: command_errors.append(f'{{name}}: {{type(exc).__name__}}: {{exc}}'); continue
  if proc.returncode: command_errors.append(f'{{name}}: {{proc.stderr.strip() or proc.stdout.strip()}}')
 try: contents=subprocess.run(['amixer','-c',str(card),'contents'],capture_output=True,text=True)
 except OSError as exc: contents=None; readback_errors.append(f'contents: {{type(exc).__name__}}: {{exc}}')
 if contents is not None:
  if contents.returncode: readback_errors.append(f'contents: {{contents.stderr.strip()}}')
  blocks=re.split(r'(?=^numid=)',contents.stdout,flags=re.MULTILINE)
  for name in preimage['controls']:
   for block in blocks:
    if not re.search(r"name='"+re.escape(name)+r"'(?:,|\\n)",block): continue
    match=re.search(r'^\\s*: values=(.*)$',block,flags=re.MULTILINE)
    if match: observed[name]=match.group(1).strip().lower()
    break
 for name,value in preimage['controls'].items():
  if observed.get(name)!=str(value).lower(): readback_errors.append(f'{{name}} readback {{observed.get(name)!r}} != {{value!r}}')
result={{'restored':not readback_errors,'card':card,'expected':preimage['controls'],'observed':observed,'command_errors':command_errors,'errors':readback_errors}}
(root/'mixer-recovery-result.json').write_text(json.dumps(result,sort_keys=True))
print(json.dumps(result,sort_keys=True)); raise SystemExit(0 if result['restored'] else 1)
PY"""


def _mixer_restore_script(recovery: str, instrument: InstrumentSpec) -> str:
    return (
        "#!/bin/bash\nset -euo pipefail\n"
        + _mixer_restore_command(recovery, instrument)
        + "\n"
    )


def start_mixer_guard(
    backend: Backend,
    instrument: Mapping[str, Any],
    spec: InstrumentSpec,
    *,
    seconds: int = 600,
) -> tuple[str, str]:
    guard_id = f"calibration-{stamp()}"
    recovery = f"{RECOVERY_ROOT}/{guard_id}"
    preimage = mixer_preimage_document(instrument, spec)
    encoded_preimage = base64.b64encode(canonical_json(preimage)).decode()
    script = _mixer_restore_script(recovery, spec).encode()
    encoded = base64.b64encode(script).decode()
    command = (
        f"set -euo pipefail; mkdir -p {shlex.quote(recovery)}; "
        f"echo {shlex.quote(encoded_preimage)} | base64 -d > "
        f"{shlex.quote(recovery + '/mixer-preimage.json')}; "
        f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(recovery + '/restore.sh')}; "
        f"chmod 700 {shlex.quote(recovery + '/restore.sh')}"
    )
    backend.pi(command, timeout=30).require("capture calibration mixer preimage")
    arm_deadman(backend, guard_id, recovery, seconds)
    return guard_id, recovery


def stop_mixer_guard(backend: Backend, guard_id: str, recovery: str) -> dict[str, Any]:
    result = backend.pi(
        f"/bin/bash {shlex.quote(recovery + '/restore.sh')}", timeout=45
    )
    report = parse_json_result(result, "restore calibration mixer preimage")
    if report.get("restored") is not True:
        raise SafetyFailure(f"calibration mixer preimage did not restore: {report}")
    cancel_deadman(backend, guard_id)
    return report


def finish_mixer_guard(
    backend: Backend,
    guard_id: str,
    recovery: str,
    primary_failure: BaseException | None,
) -> dict[str, Any]:
    """Restore first, then preserve the measurement error without hiding cleanup failure."""

    try:
        report = stop_mixer_guard(backend, guard_id, recovery)
    except BaseException as cleanup_failure:
        if primary_failure is not None:
            raise SafetyFailure(
                f"primary failure: {type(primary_failure).__name__}: {primary_failure}; "
                f"mixer cleanup failure: {type(cleanup_failure).__name__}: {cleanup_failure}"
            ) from primary_failure
        raise
    if primary_failure is not None:
        raise primary_failure.with_traceback(primary_failure.__traceback__)
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


def load_quick_calibration(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HardwareRequired(
            "the AUX wiring-session gate is missing; connect Pi AUX to the GeneralPlus "
            "input and run `rig transparent-audio quick-calibrate --hardware-ready --live`"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SafetyFailure(f"quick calibration file is malformed: {exc}") from exc
    if not isinstance(document, dict):
        raise SafetyFailure("quick calibration is not a JSON object")
    return document


def quick_calibration_failures(
    metrics: Mapping[str, Any], thresholds: Thresholds
) -> list[str]:
    failures: list[str] = []
    noise = float(metrics.get("noise_floor_dbfs", math.inf))
    margin = float(metrics.get("above_floor_db", -math.inf))
    clipped = float(metrics.get("clipped_pct", math.inf))
    if noise > thresholds.noise_floor_dbfs:
        failures.append(f"noise floor exceeds {thresholds.noise_floor_dbfs:.1f} dBFS")
    if margin < thresholds.quick_aux_above_floor_db:
        failures.append(
            "AUX wiring-continuity margin is below "
            f"{thresholds.quick_aux_above_floor_db:.1f} dB"
        )
    if clipped > thresholds.clipping_pct:
        failures.append(f"clipping exceeds {thresholds.clipping_pct:.3f}%")
    return failures


def validate_quick_calibration(
    document: Mapping[str, Any], fingerprint: str, thresholds: Thresholds
) -> None:
    fixture = document.get("instrument")
    cable_id = fixture.get("cable_id") if isinstance(fixture, dict) else None
    if (
        not isinstance(cable_id, str)
        or not cable_id.strip()
        or cable_id.strip().upper().startswith("REPLACE_")
    ):
        raise HardwareRequired(
            "the AUX wiring-session gate has no physical cable label; rerun quick-calibrate"
        )
    if document.get("instrument_fingerprint") != fingerprint:
        raise HardwareRequired(
            "the AUX wiring-session gate is stale (instrument, port, mixer map, cable, "
            "AUX volume, or fixture label changed); rerun quick-calibrate"
        )
    metrics = document.get("metrics")
    if not isinstance(metrics, dict):
        raise HardwareRequired(
            "the AUX wiring-session gate has no measurements; rerun quick-calibrate"
        )
    failures = quick_calibration_failures(metrics, thresholds)
    if failures:
        raise HardwareRequired(
            "the AUX wiring-session gate does not pass: " + ", ".join(failures)
        )
    capture_gain = document.get("capture_gain_request")
    if not isinstance(capture_gain, str) or not re.fullmatch(
        r"(?:100|[0-9]{1,2})%", capture_gain
    ):
        raise SafetyFailure(
            "the AUX wiring-session gate has no valid explicit capture gain"
        )


def validate_calibration(
    document: Mapping[str, Any], fingerprint: str, thresholds: Thresholds
) -> None:
    fixture = document.get("instrument")
    if not isinstance(fixture, dict) or not str(fixture.get("cable_id", "")).strip():
        raise HardwareRequired("GeneralPlus calibration has no physical cable label")
    if not str(fixture.get("speaker_position_id", "")).strip():
        raise HardwareRequired(
            "GeneralPlus calibration has no fixed speaker-position label"
        )
    if document.get("instrument_fingerprint") != fingerprint:
        raise HardwareRequired(
            "GeneralPlus calibration is stale (instrument, port, mixer, cable, AUX volume, or speaker position changed)"
        )
    stages = document.get("stages")
    if not isinstance(stages, dict):
        raise HardwareRequired("GeneralPlus calibration has no completed stages")
    missing = [stage for stage in REQUIRED_CALIBRATION_STAGES if stage not in stages]
    if missing:
        raise HardwareRequired(
            "GeneralPlus calibration stages missing: " + ", ".join(missing)
        )
    self_loop = stages["self-loop"]
    aux_loop = stages["aux-loop"]
    acoustic = stages["acoustic"]
    failures: list[str] = []
    if float(self_loop.get("noise_floor_dbfs", math.inf)) > thresholds.noise_floor_dbfs:
        failures.append("self-loop noise floor")
    if (
        float(self_loop.get("linearity_error_db", math.inf))
        > thresholds.linearity_error_db
    ):
        failures.append("self-loop linearity")
    if (
        float(self_loop.get("dynamic_range_db", -math.inf))
        < thresholds.dynamic_range_db
    ):
        failures.append("self-loop dynamic range")
    if float(aux_loop.get("above_floor_db", -math.inf)) < thresholds.aux_above_floor_db:
        failures.append("AUX signal margin")
    if float(acoustic.get("snr_db", -math.inf)) < thresholds.acoustic_snr_db:
        failures.append("acoustic SNR")
    for stage in REQUIRED_CALIBRATION_STAGES:
        if float(stages[stage].get("clipped_pct", math.inf)) > thresholds.clipping_pct:
            failures.append(f"{stage} clipping")
    if failures:
        raise HardwareRequired(
            "GeneralPlus calibration does not meet gates: " + ", ".join(failures)
        )


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
                path: sha256_bytes(content)
                for path, content in sorted(self.policy_files.items())
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
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix == ".pyc"
            ):
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
        (candidate_path / ".git").exists()
        or (candidate_path / "pi" / "bridged").is_dir()
    )
    if is_worktree:
        repo = candidate_path
        revision = str(_git(repo, "rev-parse", "HEAD")).strip()
        diff = _git(repo, "diff", "--binary", "--no-ext-diff", "HEAD", binary=True)
        assert isinstance(diff, bytes)
        changed = set(str(_git(repo, "diff", "--name-only", "HEAD")).splitlines())
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
    content_manifest = {
        path: sha256_bytes(content) for path, content in sorted(files.items())
    }
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
    if (
        old_content != candidate.content_sha256
        or previous.get("candidate_id") != candidate.candidate_id
    ):
        return "supervisor"
    return "none"


def _remote_manifest_script() -> str:
    return r"""python3 - <<'PY'
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
 try:
  p=subprocess.run(command,capture_output=True,text=True)
  return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
 except OSError as exc:
  return {'returncode':127,'stdout':'','stderr':f'{type(exc).__name__}: {exc}'}
result={
 'timestamp':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
 'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'deployed_hashes':hashes,
 'deployed_head':run(['git','-C','/home/admin/rpi-lark-bridge','rev-parse','HEAD']),
 'services':run(['systemctl','--user','show','pipewire.service','pipewire-pulse.service','wireplumber.service','bridge-supervisor.service','--property=Id','--property=ActiveState','--property=NRestarts','--property=ExecMainStartTimestampMonotonic','--property=Environment','--property=ExecStart']),
 'supervisor_process':run(['/bin/bash','-lc',"pid=$(systemctl --user show bridge-supervisor.service --property=MainPID --value); test \"$pid\" -gt 0; tr '\\0' '\\n' < /proc/$pid/environ; printf '\\n--CMDLINE--\\n'; tr '\\0' ' ' < /proc/$pid/cmdline"]),
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
PY"""


def capture_snapshot(backend: Backend, *, full: bool = False) -> dict[str, Any]:
    pi = parse_json_result(
        backend.pi(_remote_manifest_script(), timeout=45), "Pi snapshot"
    )
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
        records: list[dict[str, str]] = []
        record: dict[str, str] = {}
        for line in [*str(block.get("stdout", "")).splitlines(), ""]:
            if not line.strip():
                if record:
                    records.append(record)
                    record = {}
                continue
            key, separator, value = line.partition("=")
            if not separator:
                continue
            # systemctl does not promise property order. A repeated property marks
            # the next unit even when a test fixture or older systemctl omits blank
            # record separators.
            if key in record:
                records.append(record)
                record = {}
            record[key] = value
        for record in records:
            unit = record.get("Id")
            restarts = record.get("NRestarts")
            if unit and restarts is not None:
                with contextlib.suppress(ValueError):
                    result[unit] = int(restarts)
    return result


def changed_restarts(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, int]:
    first, second = service_restarts(before), service_restarts(after)
    return {
        unit: second.get(unit, 0) - first.get(unit, 0)
        for unit in sorted(first.keys() | second.keys())
        if second.get(unit, 0) != first.get(unit, 0)
    }


def restart_counter_failures(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[dict[str, int], list[str]]:
    """NRestarts counts crash recovery, not an explicit systemctl restart."""

    deltas = changed_restarts(before, after)
    failures = [f"service crash/recovery counters changed: {deltas}"] if deltas else []
    return deltas, failures


def required_snapshot_evidence_failures(snapshot: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    required_pi = (
        "services",
        "supervisor_process",
        "system_services",
        "bluetooth",
        "graph",
        "links",
        "usb",
        "usb_topology",
        "kernel_errors",
        "watchdog",
    )
    for name in required_pi:
        block = snapshot.get(name)
        if not isinstance(block, dict):
            failures.append(f"required {name} evidence is missing")
        elif block.get("returncode") != 0:
            failures.append(
                f"required {name} probe failed rc={block.get('returncode')}: "
                f"{block.get('stderr', '')}"
            )
    deployed_hashes = snapshot.get("deployed_hashes")
    if not isinstance(deployed_hashes, dict) or not deployed_hashes:
        failures.append("required deployed hash manifest is missing or empty")
    if not isinstance(snapshot.get("deployed_head"), dict):
        failures.append("required deployed revision probe result is missing")
    status = snapshot.get("status")
    if not isinstance(status, dict) or not status or snapshot.get("status_error"):
        failures.append("required supervisor status evidence is missing or malformed")
    # Full journals are useful evidence but are not a rapid-loop prerequisite: the
    # disposable Pi image does not grant the development account system-journal
    # access on every build. Preserve the probe result in the artifact and rely on
    # the independently collected service, watchdog, USB and HCI evidence here.
    android = snapshot.get("android")
    if not isinstance(android, dict):
        failures.append("required Android evidence is missing")
    else:
        for name in ("adb_devices", "audio", "bluetooth_manager"):
            block = android.get(name)
            if not isinstance(block, dict) or block.get("returncode") != 0:
                failures.append(f"required Android {name} probe failed")
    graph = snapshot.get("graph")
    if isinstance(graph, dict) and graph.get("returncode") == 0:
        try:
            parsed = json.loads(str(graph.get("stdout", "")))
            if not isinstance(parsed, list):
                raise TypeError("pw-dump root is not a list")
        except (json.JSONDecodeError, TypeError) as exc:
            failures.append(f"required PipeWire graph evidence is malformed: {exc}")
    expected_counters = {
        "pipewire.service",
        "pipewire-pulse.service",
        "wireplumber.service",
        "bridge-supervisor.service",
        "bluetooth.service",
    }
    missing_counters = sorted(expected_counters - set(service_restarts(snapshot)))
    if missing_counters:
        failures.append(
            "required service restart counters are missing: "
            + ", ".join(missing_counters)
        )
    watchdog = snapshot.get("watchdog")
    if isinstance(watchdog, dict) and watchdog.get("returncode") == 0:
        try:
            report = json.loads(str(watchdog.get("stdout", "")))
            recoveries = report.get("recoveries") if isinstance(report, dict) else None
            if isinstance(recoveries, bool) or int(recoveries) < 0:
                raise ValueError("recoveries is not a nonnegative integer")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            failures.append(f"required watchdog evidence is malformed: {exc}")
    return failures


def pipewire_node_graph(
    snapshot: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]]]:
    block = snapshot.get("graph")
    if not isinstance(block, dict) or block.get("returncode") != 0:
        raise RigFailure("PipeWire graph evidence is unavailable")
    try:
        document = json.loads(str(block.get("stdout", "")))
    except json.JSONDecodeError as exc:
        raise RigFailure(f"PipeWire graph evidence is malformed: {exc}") from exc
    if not isinstance(document, list):
        raise RigFailure("PipeWire graph evidence is not a JSON list")
    by_id: dict[int, str] = {}
    nodes: dict[str, dict[str, Any]] = {}
    raw_links: list[tuple[int, int]] = []
    for item in document:
        if not isinstance(item, dict):
            continue
        info = item.get("info")
        info = info if isinstance(info, dict) else {}
        props = info.get("props")
        props = props if isinstance(props, dict) else {}
        kind = str(item.get("type", ""))
        if kind.endswith(":Node"):
            name = props.get("node.name")
            identifier = item.get("id")
            if isinstance(name, str) and isinstance(identifier, int):
                by_id[identifier] = name
                nodes[name] = dict(props)
        elif kind.endswith(":Link"):
            output = props.get("link.output.node")
            target = props.get("link.input.node")
            try:
                raw_links.append((int(output), int(target)))
            except (TypeError, ValueError):
                continue
    links = [
        (by_id[output], by_id[target])
        for output, target in raw_links
        if output in by_id and target in by_id
    ]
    return nodes, links


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
        return {
            line.strip()
            for line in str(block.get("stdout", "")).splitlines()
            if line.strip()
        }

    return sorted(lines(after) - lines(before))


def status_phone(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    status = snapshot.get("status")
    if not isinstance(status, dict):
        return {}
    phone = status.get("phone")
    return phone if isinstance(phone, dict) else {}


CONDITION_PROBE = r"""python3 - <<'PY'
import json,pathlib,subprocess,time
def run(command):
 try:
  p=subprocess.run(command,capture_output=True,text=True)
  return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
 except OSError as exc:
  return {'returncode':127,'stdout':'','stderr':f'{type(exc).__name__}: {exc}'}
result={
 'condition_probe':True,
 'probe_epoch':time.time(),
 'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'services':run(['systemctl','--user','show','pipewire.service','pipewire-pulse.service','wireplumber.service','bridge-supervisor.service','--property=Id','--property=ActiveState','--property=Environment','--property=MainPID','--property=ExecStart']),
 'supervisor_process':run(['/bin/bash','-lc',"pid=$(systemctl --user show bridge-supervisor.service --property=MainPID --value); test \"$pid\" -gt 0; tr '\\0' '\\n' < /proc/$pid/environ; printf '\\n--CMDLINE--\\n'; tr '\\0' ' ' < /proc/$pid/cmdline"]),
 'bluetooth':run(['bluetoothctl','show']),
 'status':{},
}
try:
 p=pathlib.Path('/run/user/1000/bridge-status.json'); result['status']=json.loads(p.read_text()); result['status_mtime']=p.stat().st_mtime
except Exception as exc: result['status_error']=f'{type(exc).__name__}: {exc}'
print(json.dumps(result))
PY"""


def capture_condition_snapshot(backend: Backend) -> dict[str, Any]:
    """One small Pi-only poll; intentionally excludes pw-dump, journals, USB, and ADB."""

    return parse_json_result(
        backend.pi(CONDITION_PROBE, timeout=15), "lightweight runtime condition probe"
    )


def session_path(artifacts: Path) -> Path:
    return artifacts / SESSION_FILE


@contextlib.contextmanager
def session_lock(artifacts: Path) -> Iterator[None]:
    """A lightweight host lock prevents concurrent mutation/checkpoint writers."""

    path = artifacts / ".transparent-audio.lock"
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SafetyFailure(
                    "another transparent-audio command holds the session lock"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise SafetyFailure(
                    "another transparent-audio command holds the session lock"
                ) from exc
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            with contextlib.suppress(OSError):
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_session(artifacts: Path, *, allow_restoring: bool = False) -> dict[str, Any]:
    path = session_path(artifacts)
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RigFailure(
            "no transparent-audio session is active; run session-start"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SafetyFailure(f"session checkpoint is malformed: {exc}") from exc
    allowed = {"active", "restoring"} if allow_restoring else {"active"}
    if not isinstance(session, dict) or session.get("status") not in allowed:
        raise RigFailure("transparent-audio session is not active")
    return session


def require_no_open_session(artifacts: Path) -> None:
    path = session_path(artifacts)
    if not path.exists():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SafetyFailure(f"session checkpoint is malformed: {exc}") from exc
    if isinstance(document, dict) and document.get("status") in {
        "starting",
        "active",
        "restoring",
    }:
        raise SafetyFailure(
            "stop/reconcile the open development session before recalibrating"
        )


def _preimage_script(
    paths: Sequence[str], recovery: str, mixer_preimage: Mapping[str, Any]
) -> str:
    encoded_paths = base64.b64encode(canonical_json(list(paths))).decode()
    encoded_mixer = base64.b64encode(canonical_json(mixer_preimage)).decode()
    return f"""set -euo pipefail
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
pathlib.Path({(recovery + '/mixer-preimage.json')!r}).write_bytes(base64.b64decode({encoded_mixer!r}))
print(json.dumps(result,sort_keys=True))
PY
sha256sum {shlex.quote(recovery + '/preimages.json')} {shlex.quote(recovery + '/mixer-preimage.json')}
"""


def capture_preimages(
    backend: Backend,
    candidate: Candidate,
    session_id: str,
    instrument: Mapping[str, Any],
    spec: InstrumentSpec,
) -> tuple[dict[str, Any], str]:
    recovery = f"{RECOVERY_ROOT}/{session_id}"
    paths = [OVERRIDE_PATH]
    paths.extend(f"{WP_DEPLOYED_DIR}/{name}" for name in sorted(candidate.policy_files))
    mixer_preimage = mixer_preimage_document(instrument, spec)
    report = backend.pi(
        _preimage_script(paths, recovery, mixer_preimage),
        timeout=45,
    ).require("capture exact preimages")
    first_line = report.stdout.splitlines()[0] if report.stdout else ""
    try:
        preimages = json.loads(first_line)
    except json.JSONDecodeError as exc:
        raise SafetyFailure(
            f"preimage capture did not return its manifest: {exc}"
        ) from exc
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
    script = f"""python3 - <<'PY'
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
PY"""
    updated = parse_json_result(
        backend.pi(script, timeout=30), "extend exact policy preimages"
    )
    session["preimages"] = updated


def _recovery_script(session_id: str, recovery: str, instrument: InstrumentSpec) -> str:
    """Script stored on the Pi and invoked by both stop and the deadman."""

    mixer_restore = _mixer_restore_command(recovery, instrument, quiet=True)
    return f"""#!/bin/bash
set -euo pipefail
export XDG_RUNTIME_DIR=/run/user/1000
recovery={shlex.quote(recovery)}
mkdir -p {shlex.quote(RUNTIME_ROOT)}
exec 8>{shlex.quote(RUNTIME_ROOT + '/mutation.lock')}
flock -n 8 || {{ echo 'another candidate mutation/recovery is active' >&2; exit 75; }}
exec 9>"$recovery/restore.lock"
flock -n 9 || {{ echo 'recovery already running' >&2; exit 75; }}
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
set +e
{mixer_restore}
set -e
rm -rf {shlex.quote(f'{RUNTIME_ROOT}/{session_id}')}
systemctl --user daemon-reload
systemctl --user stop bridge-supervisor.service
systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
systemctl --user start bridge-supervisor.service
for i in $(seq 1 120); do
  all_active=true
  for unit in pipewire.service pipewire-pulse.service wireplumber.service bridge-supervisor.service; do
    systemctl --user is-active --quiet "$unit" || all_active=false
  done
  $all_active && break
  sleep 0.25
done
python3 - <<'PY'
import hashlib,json,pathlib,subprocess
root=pathlib.Path({recovery!r}); before=json.loads((root/'preimages.json').read_text()); after={{}}; ok=True
for raw,item in before.items():
 p=pathlib.Path(raw); exists=p.exists(); observed=hashlib.sha256(p.read_bytes()).hexdigest() if exists else None
 match=exists==bool(item.get('exists')) and (not exists or observed==item.get('sha256'))
 after[raw]={{'exists':exists,'sha256':observed,'matches':match}}; ok=ok and match
try: mixer=json.loads((root/'mixer-recovery-result.json').read_text())
except Exception as exc: mixer={{'restored':False,'errors':[f'{{type(exc).__name__}}: {{exc}}']}}
ok=ok and mixer.get('restored') is True
services={{}}
for unit in ('pipewire.service','pipewire-pulse.service','wireplumber.service','bridge-supervisor.service'):
 p=subprocess.run(['systemctl','--user','is-active',unit],capture_output=True,text=True)
 state=p.stdout.strip(); services[unit]={{'returncode':p.returncode,'state':state}}
 ok=ok and p.returncode==0 and state=='active'
document={{'session_id':{session_id!r},'restored':ok,'files':after,'mixer':mixer,'services':services}}
(root/'recovery-result.json').write_text(json.dumps(document,sort_keys=True))
print(json.dumps(document,sort_keys=True))
raise SystemExit(0 if ok else 1)
PY
"""


def install_recovery_script(
    backend: Backend,
    session_id: str,
    recovery: str,
    instrument: InstrumentSpec,
) -> None:
    content = _recovery_script(session_id, recovery, instrument).encode()
    encoded = base64.b64encode(content).decode()
    expected_sha256 = sha256_bytes(content)
    target = recovery + "/restore.sh"
    temporary = target + ".e19-new"
    backend.pi(
        "set -euo pipefail\n"
        f"rm -f -- {shlex.quote(temporary)}\n"
        f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(temporary)}\n"
        f"test \"$(sha256sum {shlex.quote(temporary)} | awk '{{print $1}}')\" = "
        f"{shlex.quote(expected_sha256)}\n"
        f"chmod 700 {shlex.quote(temporary)}\n"
        f"mv -f -- {shlex.quote(temporary)} {shlex.quote(target)}\n"
        f"test \"$(sha256sum {shlex.quote(target)} | awk '{{print $1}}')\" = "
        f"{shlex.quote(expected_sha256)}",
        timeout=20,
    ).require("install recovery script")


def deadman_unit(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id)
    return f"larkbridge-dev-deadman-{safe}"


def arm_deadman(
    backend: Backend, session_id: str, recovery: str, seconds: int = 900
) -> None:
    if seconds < 60 or seconds > 3600:
        raise SafetyFailure("deadman must be between 60 and 3600 seconds")
    unit = deadman_unit(session_id)
    quoted = shlex.quote(unit)
    script = f"""set -euo pipefail
mkdir -p {shlex.quote(RUNTIME_ROOT)}
exec 8>{shlex.quote(RUNTIME_ROOT + '/mutation.lock')}
flock -n 8 || {{ echo 'candidate mutation/recovery is active' >&2; exit 75; }}
timer={quoted}.timer
service={quoted}.service
load=$(systemctl --user show "$timer" --property=LoadState --value 2>/dev/null || true)
if [ "$load" != "not-found" ]; then
  test -n "$load"
  systemctl --user stop "$timer"
  test "$(systemctl --user show "$timer" --property=ActiveState --value)" = inactive
fi
load=$(systemctl --user show "$service" --property=LoadState --value 2>/dev/null || true)
if [ "$load" != "not-found" ]; then
  test -n "$load"
  state=$(systemctl --user show "$service" --property=ActiveState --value)
  case "$state" in
    inactive|failed) systemctl --user reset-failed "$service" 2>/dev/null || true ;;
    *) echo 'recovery started while renewing deadman' >&2; exit 75 ;;
  esac
fi
systemd-run --user --collect --unit={quoted} --on-active={seconds}s /bin/bash {shlex.quote(recovery + '/restore.sh')}
test "$(systemctl --user show {quoted}.timer --property=ActiveState --value)" = active
"""
    backend.pi(script, timeout=20).require("arm Pi-side recovery deadman")


def cancel_deadman(backend: Backend, session_id: str) -> None:
    unit = deadman_unit(session_id)
    quoted = shlex.quote(unit)
    script = f"""set -euo pipefail
timer={quoted}.timer
service={quoted}.service
load=$(systemctl --user show "$timer" --property=LoadState --value 2>/dev/null || true)
if [ "$load" != "not-found" ]; then
  test -n "$load"
  systemctl --user stop "$timer"
  test "$(systemctl --user show "$timer" --property=ActiveState --value)" = inactive
fi
for i in $(seq 1 480); do
  load=$(systemctl --user show "$service" --property=LoadState --value 2>/dev/null || true)
  [ "$load" = "not-found" ] && exit 0
  test -n "$load"
  state=$(systemctl --user show "$service" --property=ActiveState --value)
  [ "$state" = "inactive" ] && {{ systemctl --user reset-failed "$service" 2>/dev/null || true; exit 0; }}
  [ "$state" = "failed" ] && {{ systemctl --user reset-failed "$service"; exit 0; }}
  sleep 0.25
done
echo 'timed out waiting for recovery service to finish; it was not killed' >&2
exit 75
"""
    backend.pi(script, timeout=125).require("cancel and verify recovery deadman")


def _write_remote_file(path: str, content: bytes) -> str:
    encoded = base64.b64encode(content).decode()
    temporary = path + ".e19-new"
    return (
        f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}; "
        f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(temporary)}; "
        f"chmod 0644 {shlex.quote(temporary)}; mv -f {shlex.quote(temporary)} {shlex.quote(path)}"
    )


def stage_candidate_files(
    backend: Backend, candidate: Candidate, session_id: str
) -> str:
    candidate_root = f"{RUNTIME_ROOT}/{session_id}/candidates/{candidate.candidate_id}"
    backend.pi(
        f"set -euo pipefail\nmkdir -p {shlex.quote(candidate_root)}\n"
        f"tar -xzf - -C {shlex.quote(candidate_root)}",
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
        f"Environment=BRIDGE_CONFIG={CONFIG_PATH}\n"
        f"Environment=LARKBRIDGE_DEV_CANDIDATE={candidate.candidate_id}\n"
    ).encode()
    commands = [_write_remote_file(OVERRIDE_PATH, override)]
    if restart_class == "audio-stack":
        for name, content in sorted(candidate.policy_files.items()):
            commands.append(_write_remote_file(f"{WP_DEPLOYED_DIR}/{name}", content))
        for name in sorted(remove_policy_names):
            commands.append(f"rm -f {shlex.quote(f'{WP_DEPLOYED_DIR}/{name}')}")
    # A post-apply verify must never consume the previous supervisor's status file.
    commands.append(f"rm -f {shlex.quote(STATUS_PATH)}")
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
    transaction = (
        "set -euo pipefail\n"
        f"mkdir -p {shlex.quote(RUNTIME_ROOT)}\n"
        f"exec 8>{shlex.quote(RUNTIME_ROOT + '/mutation.lock')}\n"
        "flock -n 8 || { echo 'another candidate mutation/recovery is active' >&2; exit 75; }\n"
        + "\n".join(commands)
    )
    backend.pi(transaction, timeout=75).require(f"apply {restart_class} candidate")


def aux_volume_evidence(
    status: Mapping[str, Any],
    *,
    expected_target: str,
    expected_volume: float,
    allow_legacy_pre_candidate: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    phone = status.get("phone")
    phone = phone if isinstance(phone, dict) else {}
    block = phone.get("target_volume")
    target = phone.get("expected_target")
    source = "phone.target_volume"
    if not isinstance(block, dict) and allow_legacy_pre_candidate:
        block = status.get("wired_output_volume")
        target = block.get("target") if isinstance(block, dict) else None
        source = "legacy-pre-candidate wired_output_volume"
    if not isinstance(block, dict):
        return {}, ["phone.target_volume status is missing"]
    evidence = dict(block)
    evidence.update(target=target, source=source)
    failures: list[str] = []
    if block.get("required") is not True:
        failures.append("fixed AUX volume enforcement is not required")
    if target != expected_target:
        failures.append(
            f"volume target is {target!r}, expected fixed AUX {expected_target!r}"
        )
    for field in ("desired", "observed"):
        value = block.get(field)
        try:
            matches = value is not None and math.isclose(
                float(value), expected_volume, abs_tol=0.011
            )
        except (TypeError, ValueError):
            matches = False
        if not matches:
            failures.append(
                f"{field} volume is {value!r}, expected {expected_volume:.2f}"
            )
    if block.get("verified") is not True:
        failures.append("fixed AUX volume is not verified")
    if block.get("error") is not None:
        failures.append(f"fixed AUX volume reports error {block.get('error')!r}")
    return evidence, failures


def aux_volume_failures(
    status: Mapping[str, Any],
    *,
    expected_target: str,
    expected_volume: float,
    allow_legacy_pre_candidate: bool = False,
) -> list[str]:
    _evidence, failures = aux_volume_evidence(
        status,
        expected_target=expected_target,
        expected_volume=expected_volume,
        allow_legacy_pre_candidate=allow_legacy_pre_candidate,
    )
    return failures


def call_output_aux_volume_failures(
    status: Mapping[str, Any],
    *,
    expected_target: str,
    expected_volume: float,
    fixed_aux_evidence: Mapping[str, Any] | None = None,
) -> list[str]:
    block = status.get("wired_output_volume")
    if isinstance(block, dict) and block.get("target") == expected_target:
        synthetic = {
            "phone": {
                "expected_target": block.get("target"),
                "target_volume": block,
            }
        }
        return aux_volume_failures(
            synthetic,
            expected_target=expected_target,
            expected_volume=expected_volume,
        )
    if not isinstance(fixed_aux_evidence, Mapping):
        return [
            "call output is not fixed AUX and independent fixed-AUX volume evidence is missing"
        ]
    observed = fixed_aux_evidence.get("observed")
    try:
        matches = observed is not None and math.isclose(
            float(observed), expected_volume, abs_tol=0.011
        )
    except (TypeError, ValueError):
        matches = False
    failures: list[str] = []
    if fixed_aux_evidence.get("target") != expected_target:
        failures.append("independent volume probe resolved the wrong AUX target")
    if fixed_aux_evidence.get("verified") is not True:
        failures.append("independent fixed-AUX volume probe is not verified")
    if fixed_aux_evidence.get("error") is not None:
        failures.append(
            f"independent fixed-AUX volume probe reports {fixed_aux_evidence.get('error')!r}"
        )
    if not matches:
        failures.append(
            f"independent fixed-AUX volume is {observed!r}, expected {expected_volume:.2f}"
        )
    return failures


def probe_fixed_aux_volume(backend: Backend, target: str) -> dict[str, Any]:
    """Read one named node directly when CALL owns a different wired output."""

    script = f"""python3 - <<'PY'
import json,re,subprocess
target={target!r}
document={{'target':target,'observed':None,'verified':False,'error':None}}
try:
 graph=subprocess.run(['pw-dump'],capture_output=True,text=True,check=True)
 nodes=[]
 for item in json.loads(graph.stdout):
  if not isinstance(item,dict) or not str(item.get('type','')).endswith(':Node'): continue
  info=item.get('info') if isinstance(item.get('info'),dict) else {{}}
  props=info.get('props') if isinstance(info.get('props'),dict) else {{}}
  if props.get('node.name')==target: nodes.append(item.get('id'))
 if len(nodes)!=1: raise RuntimeError(f'expected one {{target}} node, found {{len(nodes)}}')
 got=subprocess.run(['wpctl','get-volume',str(nodes[0])],capture_output=True,text=True,check=True)
 match=re.search(r'Volume:\\s*([0-9.]+)',got.stdout)
 if not match: raise RuntimeError(f'unrecognized wpctl output: {{got.stdout!r}}')
 if '[MUTED]' in got.stdout: raise RuntimeError('fixed AUX node is muted')
 document['observed']=float(match.group(1)); document['verified']=True
except Exception as exc:
 document['error']=f'{{type(exc).__name__}}: {{exc}}'
print(json.dumps(document,sort_keys=True))
PY"""
    return parse_json_result(
        backend.pi(script, timeout=20), "independent fixed-AUX volume probe"
    )


def verify_runtime(
    backend: Backend,
    expected_volume: float,
    expected_target: str,
    expected_candidate_id: str,
    *,
    mode: str = "media",
) -> dict[str, Any]:
    started = time.monotonic()
    snapshot: dict[str, Any] = {}
    while time.monotonic() - started < 30:
        snapshot = capture_condition_snapshot(backend)
        services = snapshot.get("services")
        active = (
            str(services.get("stdout", "")).count("ActiveState=active")
            if isinstance(services, dict)
            else 0
        )
        if active >= 4 and isinstance(snapshot.get("status"), dict):
            break
        backend.wait(0.1)
    else:
        raise RigFailure(
            "timed out after 30s waiting for the audio stack using the lightweight condition probe"
        )
    status = snapshot.get("status")
    if not isinstance(status, dict):
        raise RigFailure("supervisor status is unavailable after candidate restart")
    expected_marker = f"LARKBRIDGE_DEV_CANDIDATE={expected_candidate_id}"
    process = snapshot.get("supervisor_process")
    process_output = str(process.get("stdout", "")) if isinstance(process, dict) else ""
    if not isinstance(process, dict) or process.get("returncode") != 0:
        raise RigFailure("running supervisor process identity could not be inspected")
    if expected_marker not in process_output:
        raise RigFailure(
            "running supervisor process does not expose the expected volatile candidate marker "
            f"{expected_marker!r}"
        )
    expected_root = f"/candidates/{expected_candidate_id}/bridge_supervisor.py"
    if expected_root not in process_output:
        raise RigFailure(
            "running supervisor command line does not point to the expected staged package "
            f"{expected_root!r}"
        )
    try:
        probe_epoch = float(snapshot["probe_epoch"])
        status_mtime = float(snapshot["status_mtime"])
        status_timestamp = float(status["timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RigFailure("supervisor status freshness evidence is missing") from exc
    if (
        probe_epoch - status_mtime > 5.0
        or probe_epoch - status_timestamp > 5.0
        or status_mtime > probe_epoch + 1.0
        or status_timestamp > probe_epoch + 1.0
    ):
        raise RigFailure("supervisor status is stale or has an invalid timestamp")
    fixed_aux_evidence: Mapping[str, Any] | None = None
    if mode == "call":
        wired = status.get("wired_output_volume")
        if not isinstance(wired, dict) or wired.get("target") != expected_target:
            fixed_aux_evidence = probe_fixed_aux_volume(backend, expected_target)
        volume_failures = call_output_aux_volume_failures(
            status,
            expected_target=expected_target,
            expected_volume=expected_volume,
            fixed_aux_evidence=fixed_aux_evidence,
        )
    else:
        volume_failures = aux_volume_failures(
            status,
            expected_target=expected_target,
            expected_volume=expected_volume,
        )
    if volume_failures:
        raise RigFailure(
            "AUX volume verification failed: " + "; ".join(volume_failures)
        )
    bluetooth = str(snapshot.get("bluetooth", {}).get("stdout", ""))
    if "Audio Sink" not in bluetooth and "0000110b" not in bluetooth.lower():
        raise RigFailure("adapter does not advertise the A2DP sink role after restart")
    return snapshot


def restore_remote_session(
    backend: Backend, session: Mapping[str, Any]
) -> dict[str, Any]:
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
    selected_channels = (
        channels if channels is not None else (2 if mode == "sine" else 1)
    )
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
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False
        )
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
    minimum_stable_s: float = 1.0,
) -> dict[str, Any]:
    _wav_level, glitch_detect = _load_analysis_modules()
    channels, rate = glitch_detect.read_wav(str(capture))
    if rate != GENERALPLUS_RATE or len(channels) != GENERALPLUS_CAPTURE_CHANNELS:
        raise RigFailure(
            f"capture format is {rate} Hz/{len(channels)}ch; expected 48000 Hz/1ch"
        )
    all_samples = channels[0]
    window = max(int(rate * 0.1), 1)
    threshold_dbfs = max(calibrated_noise_floor_dbfs + 15.0, -55.0)
    active: list[bool] = []
    for index in range(0, len(all_samples) - window + 1, window):
        segment = all_samples[index : index + window]
        mean = sum(segment) / len(segment)
        rms = math.sqrt(sum((sample - mean) ** 2 for sample in segment) / len(segment))
        dbfs = -200.0 if rms <= 0 else 20.0 * math.log10(rms)
        active.append(dbfs >= threshold_dbfs)
    try:
        first_active = active.index(True)
        last_active = len(active) - 1 - active[::-1].index(True)
    except ValueError as exc:
        raise RigFailure("media capture has no detected steady-tone window") from exc

    # Lead/trail silence is intentional, but silence after the first detected onset and
    # before the final detected tone is a real transport discontinuity. Analysing only
    # the longest active run would hide an interior dropout by relabelling the later
    # audio as trailing silence.
    inactive_gaps: list[dict[str, float]] = []
    gap_start: int | None = None
    for index in range(first_active, last_active + 2):
        present = index <= last_active and active[index]
        if not present and gap_start is None:
            gap_start = index
        elif present and gap_start is not None:
            inactive_gaps.append(
                {
                    "start_s": round(gap_start * window / rate, 3),
                    "end_s": round(index * window / rate, 3),
                }
            )
            gap_start = None
    trim = int(rate * 0.2)
    start = first_active * window + trim
    end = min((last_active + 1) * window - trim, len(all_samples))
    stable_s = max(0.0, (end - start) / rate)
    samples = all_samples[start:end]
    if not samples:
        raise RigFailure("media capture has no detected steady-tone window")
    bursts, _floor = glitch_detect.hp_burst(
        samples, rate, MEDIA_STIMULUS_FREQUENCY_HZ, 25.0, 5.0
    )
    steps = glitch_detect.step(samples, rate, MEDIA_STIMULUS_FREQUENCY_HZ, 5.0)
    mean = sum(samples) / len(samples)
    rms = math.sqrt(sum((sample - mean) ** 2 for sample in samples) / len(samples))
    signal_rms_dbfs = -200.0 if rms <= 0 else 20.0 * math.log10(rms)
    clipped_pct = (
        100.0 * sum(abs(sample) >= 32767 / 32768 for sample in samples) / len(samples)
    )
    signal_margin = signal_rms_dbfs - calibrated_noise_floor_dbfs
    signature_block = max(rate // 2, 1)
    basis_cos = [
        math.cos(2.0 * math.pi * MEDIA_STIMULUS_FREQUENCY_HZ * i / rate)
        for i in range(signature_block)
    ]
    basis_sin = [
        math.sin(2.0 * math.pi * MEDIA_STIMULUS_FREQUENCY_HZ * i / rate)
        for i in range(signature_block)
    ]
    signature_ratios: list[float] = []
    for offset in range(0, len(samples) - signature_block + 1, signature_block):
        block = samples[offset : offset + signature_block]
        block_mean = sum(block) / len(block)
        centered = [value - block_mean for value in block]
        total_power = sum(value * value for value in centered) / len(centered)
        in_phase = sum(
            value * basis for value, basis in zip(centered, basis_cos, strict=True)
        ) / len(centered)
        quadrature = sum(
            value * basis for value, basis in zip(centered, basis_sin, strict=True)
        ) / len(centered)
        tone_power = 2.0 * (in_phase * in_phase + quadrature * quadrature)
        residual_power = max(total_power - tone_power, 1e-12)
        signature_ratios.append(
            10.0 * math.log10(max(tone_power, 1e-12) / residual_power)
        )
    tone_to_residual_db = (
        statistics.median(signature_ratios) if signature_ratios else -math.inf
    )
    failures: list[str] = []
    if stable_s < minimum_stable_s:
        failures.append(
            f"steady media window is {stable_s:.2f}s, below required {minimum_stable_s:.2f}s"
        )
    if signal_margin < thresholds.aux_above_floor_db:
        failures.append(
            f"signal margin {signal_margin:.2f} dB is below {thresholds.aux_above_floor_db:.2f} dB"
        )
    if tone_to_residual_db < MIN_MEDIA_TONE_TO_RESIDUAL_DB:
        failures.append(
            f"captured signal does not match the {MEDIA_STIMULUS_FREQUENCY_HZ:.0f} Hz "
            f"media stimulus ({tone_to_residual_db:.2f} dB tone/residual)"
        )
    if clipped_pct > thresholds.clipping_pct:
        failures.append("capture clipped")
    if bursts or steps:
        failures.append(
            f"detected discontinuities after startup (hp={len(bursts)}, step={len(steps)})"
        )
    if inactive_gaps:
        failures.append(
            f"detected {len(inactive_gaps)} inactive media gap(s) after startup"
        )
    return {
        "verdict": "PASS" if not failures else "FAIL",
        "capture": str(capture),
        "capture_sha256": sha256_bytes(capture.read_bytes()),
        "steady_window": {
            "start_s": round(start / rate, 3),
            "end_s": round(end / rate, 3),
            "duration_s": round(stable_s, 3),
            "detection_threshold_dbfs": round(threshold_dbfs, 2),
            "clipped_pct": round(clipped_pct, 6),
        },
        "signal_ac_rms_dbfs": round(signal_rms_dbfs, 2),
        "signal_above_calibrated_floor_db": round(signal_margin, 2),
        "stimulus_signature": {
            "frequency_hz": MEDIA_STIMULUS_FREQUENCY_HZ,
            "tone_to_residual_db": round(tone_to_residual_db, 2),
            "minimum_db": MIN_MEDIA_TONE_TO_RESIDUAL_DB,
        },
        "discontinuities": {
            "hp_burst": bursts,
            "step": steps,
            "inactive_gaps": inactive_gaps,
        },
        "thresholds": asdict(thresholds),
        "failures": failures,
    }


def score_call(
    *,
    stimulus: Path,
    reference: Path,
    raw: Path,
    clean: Path,
    thresholds: Thresholds,
) -> dict[str, Any]:
    """Score near-end preservation; echo suppression belongs to the speaker-mode fixture."""

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from rig.analysis.aec_metrics import correlated_level, dbfs, load_energy_envelope

    stimulus_envelope, rate = load_energy_envelope(stimulus)
    raw_envelope, raw_rate = load_energy_envelope(raw)
    clean_envelope, clean_rate = load_energy_envelope(clean)
    if raw_rate != rate or clean_rate != rate:
        raise RigFailure("near-end stimulus/raw/post-AEC envelope rates differ")
    raw_level, raw_lag, raw_correlation = correlated_level(
        stimulus_envelope, raw_envelope, rate, max_lag_s=5.0
    )
    clean_level, clean_lag, clean_correlation = correlated_level(
        stimulus_envelope, clean_envelope, rate, max_lag_s=5.0
    )
    raw_dbfs, clean_dbfs = dbfs(raw_level), dbfs(clean_level)
    _wav_level, glitch_detect = _load_analysis_modules()

    def clipped_pct(path: Path) -> float:
        channels, _rate = glitch_detect.read_wav(str(path))
        samples = channels[0] if channels else []
        return (
            100.0
            * sum(abs(value) >= 32767 / 32768 for value in samples)
            / max(len(samples), 1)
        )

    raw_clipped, clean_clipped = clipped_pct(raw), clipped_pct(clean)
    preservation_loss = raw_dbfs - clean_dbfs
    failures: list[str] = []
    if raw_dbfs < thresholds.call_raw_dbfs or raw_correlation < 0.3:
        failures.append(
            "known near-end stimulus is not measurably correlated in the raw microphone"
        )
    if clean_correlation < 0.3 or preservation_loss > 6.0:
        failures.append(
            f"post-AEC path did not preserve the near-end stimulus (loss={preservation_loss:.2f} dB)"
        )
    if max(raw_clipped, clean_clipped) > thresholds.clipping_pct:
        failures.append("raw or post-AEC near-end capture clipped")
    return {
        "verdict": "PASS" if not failures else "FAIL",
        "raw_correlated_dbfs": round(raw_dbfs, 2),
        "clean_correlated_dbfs": round(clean_dbfs, 2),
        "raw_correlation": round(raw_correlation, 4),
        "clean_correlation": round(clean_correlation, 4),
        "raw_lag_ms": round(raw_lag * 1000.0 / rate, 2),
        "clean_lag_ms": round(clean_lag * 1000.0 / rate, 2),
        "near_end_preservation_loss_db": round(preservation_loss, 2),
        "clipped_pct": {"raw": raw_clipped, "clean": clean_clipped},
        "echo_suppression": "NOT_MEASURED_USE_SEPARATE_SPEAKER_MODE_FIXTURE",
        "files": {
            "stimulus": {
                "path": str(stimulus),
                "sha256": sha256_bytes(stimulus.read_bytes()),
            },
            "reference": {
                "path": str(reference),
                "sha256": sha256_bytes(reference.read_bytes()),
            },
            "raw": {"path": str(raw), "sha256": sha256_bytes(raw.read_bytes())},
            "clean": {"path": str(clean), "sha256": sha256_bytes(clean.read_bytes())},
        },
        "failures": failures,
    }


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
                    f"systemctl --user status {shlex.quote(unit)} --no-pager",
                    timeout=15,
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
        if (
            state.returncode
            and "not found" not in (state.stderr + state.stdout).lower()
        ):
            state.require(f"query {unit}")
        backend.wait(0.1)
    raise RigFailure(f"timed out waiting for {unit} to start")


def stop_and_verify_units(
    backend: Backend,
    units: Sequence[str],
    *,
    action: str,
    runtime_root: str | None = None,
) -> None:
    quoted = " ".join(shlex.quote(unit) for unit in units)
    remove = (
        f"\nrm -rf -- {shlex.quote(runtime_root)}" if runtime_root is not None else ""
    )
    script = f"""set -euo pipefail
for unit in {quoted}; do
  load=$(systemctl --user show "$unit" --property=LoadState --value 2>/dev/null || true)
  if [ "$load" = "not-found" ]; then continue; fi
  test -n "$load"
  systemctl --user stop "$unit"
  systemctl --user reset-failed "$unit" 2>/dev/null || true
  load=$(systemctl --user show "$unit" --property=LoadState --value)
  active=$(systemctl --user show "$unit" --property=ActiveState --value)
  test "$load" = "not-found" || test "$active" = "inactive"
done{remove}
"""
    backend.pi(script, timeout=30).require(action)


def wait_phone_transport(
    backend: Backend, expected: str, *, timeout: float
) -> tuple[dict[str, Any], float]:
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() - started < timeout:
        status = read_supervisor_status(backend)
        phone = status.get("phone") if isinstance(status, dict) else None
        last = phone if isinstance(phone, dict) else {}
        if last.get("transport") == expected:
            return last, time.monotonic() - started
        backend.wait(0.1)
    raise RigFailure(
        f"timed out after {timeout:g}s waiting for phone transport {expected}; last={last}"
    )


def read_supervisor_status(backend: Backend) -> dict[str, Any]:
    result = backend.pi(f"cat {STATUS_PATH}", timeout=10)
    return parse_json_result(result, "read supervisor status")


def get_android_music_volume(backend: Backend) -> dict[str, Any]:
    result = backend.adb(
        ("shell", "cmd", "media_session", "volume", "--stream", "3", "--get"),
        timeout=20,
    )
    result.require("read Android STREAM_MUSIC volume")
    match = re.search(
        r"volume is\s+(\d+)\s+in range\s+\[(\d+)\.\.(\d+)\]", result.stdout
    )
    if match is None:
        raise RigFailure(
            f"could not parse Android STREAM_MUSIC volume: {result.stdout!r}"
        )
    return {
        "value": int(match.group(1)),
        "minimum": int(match.group(2)),
        "maximum": int(match.group(3)),
        "evidence": asdict(result),
    }


def set_android_music_volume(backend: Backend, value: int) -> dict[str, Any]:
    result = backend.adb(
        (
            "shell",
            "cmd",
            "media_session",
            "volume",
            "--stream",
            "3",
            "--set",
            str(value),
        ),
        timeout=20,
    )
    result.require(f"set Android STREAM_MUSIC volume to {value}")
    observed = get_android_music_volume(backend)
    if observed["value"] != value:
        raise RigFailure(
            f"Android STREAM_MUSIC volume read back {observed['value']}, expected {value}"
        )
    return {"requested": value, "set_evidence": asdict(result), "observed": observed}


def _raise_primary_and_cleanup(
    primary: BaseException | None, cleanup_failures: Sequence[BaseException]
) -> None:
    if primary is not None and cleanup_failures:
        detail = "; ".join(
            f"{type(item).__name__}: {item}" for item in cleanup_failures
        )
        raise SafetyFailure(
            f"primary failure: {type(primary).__name__}: {primary}; cleanup failure: {detail}"
        ) from primary
    if cleanup_failures:
        raise cleanup_failures[0]
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)


def media_smoke(
    backend: Backend,
    inventory: Inventory,
    instrument: Mapping[str, Any],
    artifact: Path,
    *,
    seconds: float,
    quick_calibration: Mapping[str, Any],
) -> dict[str, Any]:
    artifact.mkdir(parents=True, exist_ok=True)
    stimulus_path = artifact / "stimulus.wav"
    stimulus = generate_stimulus(stimulus_path, mode="sine", seconds=seconds)
    package = backend.adb(("shell", "pm", "path", "org.videolan.vlc"), timeout=20)
    if package.returncode or "package:" not in package.stdout:
        raise HardwareRequired("VLC (org.videolan.vlc) is not installed on the Pixel")
    pushed = backend.adb(("push", str(stimulus_path), PHONE_MEDIA_REMOTE), timeout=90)
    pushed.require("push hashed media stimulus to Pixel")
    remote_root = f"{RUNTIME_ROOT}/media-{stamp()}"
    remote_capture = remote_root + "/capture.wav"
    remote_pw_top = remote_root + "/pw-top.txt"
    unit = "larkbridge-e19-media-capture"
    top_unit = "larkbridge-e19-media-pw-top"
    alsa_id = str(instrument["alsa_id"])
    capture_seconds = math.ceil(seconds + 5)
    volume_before = get_android_music_volume(backend)
    volume_during: dict[str, Any] | None = None
    volume_after: dict[str, Any] | None = None
    smoke: dict[str, Any] | None = None
    primary_failure: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    try:
        start = backend.pi(
            f"set -euo pipefail\nmkdir -p {shlex.quote(remote_root)}\n"
            f"systemctl --user reset-failed {unit}.service {top_unit}.service 2>/dev/null || true\n"
            f"systemd-run --user --unit={unit} --collect /usr/bin/arecord "
            f"-D {shlex.quote('plughw:CARD=' + alsa_id + ',DEV=0')} -q -t wav -f S16_LE "
            f"-r {inventory.instrument.rate} -c {inventory.instrument.capture_channels} "
            f"-d {capture_seconds} {shlex.quote(remote_capture)}\n"
            f"systemd-run --user --unit={top_unit} --collect /bin/bash -lc "
            f"{shlex.quote(f'timeout {capture_seconds + 3}s pw-top -b -n {capture_seconds} > {remote_pw_top} 2>&1')}",
            timeout=20,
        )
        start.require("start GeneralPlus AUX capture and pw-top observation")
        wait_unit_active(backend, unit + ".service")
        wait_unit_active(backend, top_unit + ".service")
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
        # Android applies safe-media and per-device volume policy. A pre-playback
        # `--set` addresses the speaker device and can be silently capped, so do not
        # mutate it. Record the active A2DP value; the fixed Pi AUX 0.95 gate is the
        # deterministic electrical level owned by this rig.
        volume_during = get_android_music_volume(backend)
        wait_unit_inactive(backend, unit + ".service", timeout=capture_seconds + 20)
        wait_unit_inactive(backend, top_unit + ".service", timeout=capture_seconds + 20)
        capture = artifact / "aux-capture.wav"
        pw_top = artifact / "pw-top.txt"
        backend.fetch(remote_capture, capture)
        backend.fetch(remote_pw_top, pw_top)
        metrics = score_media(
            capture,
            calibrated_noise_floor_dbfs=float(
                quick_calibration["metrics"]["noise_floor_dbfs"]
            ),
            thresholds=inventory.thresholds,
            minimum_stable_s=min(20.0, max(seconds - 2.0, 1.0)),
        )
        smoke = {
            "stimulus": stimulus,
            "vlc_launch": asdict(launched),
            "media_active_after_launch_s": round(active_elapsed, 3),
            "active_phone_status": active_status,
            "pw_top": {
                "path": str(pw_top),
                "sha256": sha256_bytes(pw_top.read_bytes()),
            },
            "metrics": metrics,
        }
    except BaseException as exc:  # noqa: BLE001 - cleanup must survive interruption
        primary_failure = exc
    try:
        stop_and_verify_units(
            backend,
            (unit + ".service", top_unit + ".service"),
            action="stop and verify media capture observation units",
            runtime_root=remote_root,
        )
    except BaseException as exc:  # noqa: BLE001 - preserve alongside primary failure
        cleanup_failures.append(exc)
    try:
        volume_after = get_android_music_volume(backend)
    except BaseException as exc:  # noqa: BLE001 - preserve alongside primary failure
        cleanup_failures.append(exc)
    _raise_primary_and_cleanup(primary_failure, cleanup_failures)
    assert smoke is not None and volume_during is not None and volume_after is not None
    smoke["android_music_volume"] = {
        "before": volume_before,
        "during": volume_during,
        "after": volume_after,
        "mutated_by_runner": False,
    }
    return smoke


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
    status = read_supervisor_status(backend)
    phone = status.get("phone") if isinstance(status.get("phone"), dict) else {}
    if status.get("state") != "ACTIVE" or phone.get("transport") != "CALL":
        raise HardwareRequired(
            "start a Discord call on the Pixel and select LarkBridge Bluetooth audio, then rerun"
        )
    android = backend.adb(("shell", "dumpsys", "audio"), timeout=30)
    android.require("confirm Android communication mode")
    android_audio = android.stdout
    if (
        "MODE_IN_COMMUNICATION" not in android_audio
        and "mode: 3" not in android_audio.lower()
    ):
        raise HardwareRequired(
            "Discord is not confirmed as owning Android communication mode"
        )
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
    smoke: dict[str, Any] | None = None
    primary_failure: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    try:
        backend.pi(
            f"systemd-run --user --unit={capture_unit} --collect /bin/bash -lc "
            f"{shlex.quote(capture_script)}",
            timeout=20,
        ).require("start post-AEC call capture")
        wait_unit_active(backend, capture_unit + ".service")
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
            stimulus=stimulus_path,
            reference=reference,
            raw=raw,
            clean=clean,
            thresholds=inventory.thresholds,
        )
        pw_top_files = [
            {"path": str(path), "sha256": sha256_bytes(path.read_bytes())}
            for path in sorted(local_capture.rglob("*pw*top*"))
            if path.is_file()
        ]
        if not pw_top_files:
            raise RigFailure("call capture did not retain its pw-top observer evidence")
        smoke = {
            "stimulus": stimulus,
            "pw_top_evidence": pw_top_files,
            "metrics": metrics,
        }
    except BaseException as exc:  # noqa: BLE001 - cleanup must survive interruption
        primary_failure = exc
    try:
        stop_and_verify_units(
            backend,
            (capture_unit + ".service", play_unit + ".service"),
            action="stop and verify call capture and stimulus units",
            runtime_root=remote_root,
        )
    except BaseException as exc:  # noqa: BLE001 - preserve alongside primary failure
        cleanup_failures.append(exc)
    _raise_primary_and_cleanup(primary_failure, cleanup_failures)
    assert smoke is not None
    return smoke


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
    result = dict(channels[0])
    result["ac_rms_dbfs"] = round(ac_rms_dbfs(path, skip_start=2.0, end_s=5.5), 2)
    return result


def _analyse_silence(path: Path) -> dict[str, Any]:
    wav_level, _glitch = _load_analysis_modules()
    report = wav_level.analyse(str(path), None, 0.5, 0.0)
    channels = report.get("per_channel") or []
    if len(channels) != 1:
        raise RigFailure(f"calibration capture {path} is not mono")
    result = dict(channels[0])
    result["ac_rms_dbfs"] = round(ac_rms_dbfs(path, skip_start=0.5), 2)
    return result


def ac_rms_dbfs(path: Path, *, skip_start: float, end_s: float | None = None) -> float:
    """RMS after DC removal; GeneralPlus has a stable offset that is not noise."""

    _wav_level, glitch_detect = _load_analysis_modules()
    channels, rate = glitch_detect.read_wav(str(path))
    if len(channels) != 1:
        raise RigFailure(f"level capture {path} is not mono")
    start = max(int(rate * skip_start), 0)
    end = (
        len(channels[0]) if end_s is None else min(int(rate * end_s), len(channels[0]))
    )
    samples = channels[0][start:end]
    if not samples:
        raise RigFailure(f"level capture {path} has no samples in the requested window")
    mean = sum(samples) / len(samples)
    rms = math.sqrt(sum((sample - mean) ** 2 for sample in samples) / len(samples))
    return -200.0 if rms <= 0 else 20.0 * math.log10(rms)


def quick_aux_metrics(silence: Path, tone: Path) -> dict[str, Any]:
    quiet = _analyse_silence(silence)
    measured = _analyse_tone(tone)
    floor = float(quiet["ac_rms_dbfs"])
    tone_dbfs = float(measured["tone_dbfs"])
    signal_rms_dbfs = float(measured["ac_rms_dbfs"])
    return {
        "noise_floor_dbfs": floor,
        "tone_dbfs": tone_dbfs,
        "signal_ac_rms_dbfs": signal_rms_dbfs,
        "above_floor_db": round(signal_rms_dbfs - floor, 3),
        "clipped_pct": max(
            float(quiet.get("clipped_pct", 0)),
            float(measured.get("clipped_pct", 0)),
        ),
        "silence": quiet,
        "capture": measured,
    }


def self_loop_metrics(silence: Path, tones: Mapping[float, Path]) -> dict[str, Any]:
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
    floor = float(quiet["ac_rms_dbfs"])
    return {
        "noise_floor_dbfs": floor,
        "linearity_error_db": round(linearity_error, 3),
        "dynamic_range_db": round(loudest - floor, 3),
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
        if (
            float(metrics.get("noise_floor_dbfs", math.inf))
            > thresholds.noise_floor_dbfs
        ):
            failures.append(
                f"noise floor exceeds {thresholds.noise_floor_dbfs:.1f} dBFS"
            )
        if (
            float(metrics.get("linearity_error_db", math.inf))
            > thresholds.linearity_error_db
        ):
            failures.append(
                f"linearity error exceeds {thresholds.linearity_error_db:.1f} dB"
            )
        if (
            float(metrics.get("dynamic_range_db", -math.inf))
            < thresholds.dynamic_range_db
        ):
            failures.append(
                f"dynamic range is below {thresholds.dynamic_range_db:.1f} dB"
            )
    elif stage == "aux-loop":
        if (
            float(metrics.get("above_floor_db", -math.inf))
            < thresholds.aux_above_floor_db
        ):
            failures.append(
                f"AUX signal margin is below {thresholds.aux_above_floor_db:.1f} dB"
            )
    elif float(metrics.get("snr_db", -math.inf)) < thresholds.acoustic_snr_db:
        failures.append(f"acoustic SNR is below {thresholds.acoustic_snr_db:.1f} dB")
    return failures


def _instrument_pcm(spec: InstrumentSpec, instrument: Mapping[str, Any]) -> str:
    return f"plughw:CARD={instrument['alsa_id']},DEV=0"


def perform_quick_calibration(
    backend: Backend,
    inventory: Inventory,
    instrument: Mapping[str, Any],
    artifact: Path,
) -> dict[str, Any]:
    """Measure the currently wired AUX loop once, without a full bench qualification."""

    artifact.mkdir(parents=True, exist_ok=False)
    remote_root = f"{RUNTIME_ROOT}/quick-calibration-{stamp()}"
    backend.pi(f"mkdir -p {shlex.quote(remote_root)}", timeout=15).require(
        "create quick calibration runtime directory"
    )
    status = read_supervisor_status(backend)
    volume, volume_failures = aux_volume_evidence(
        status,
        expected_target=inventory.aux_target,
        expected_volume=inventory.aux_volume,
        allow_legacy_pre_candidate=True,
    )
    if volume_failures:
        raise HardwareRequired(
            "verify Pi AUX before calibration: " + "; ".join(volume_failures)
        )
    target = str(volume["target"])
    observed_volume = float(volume["observed"])

    pcm = _instrument_pcm(inventory.instrument, instrument)
    capture_duration = 7
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

    stimulus_path = artifact / "stimulus.wav"
    stimulus = generate_stimulus(
        stimulus_path, mode="sine", seconds=4, dbfs=-12, channels=2
    )
    remote_stimulus = remote_root + "/stimulus.wav"
    remote_capture = remote_root + "/capture.wav"
    capture = artifact / "capture.wav"
    _upload(backend, stimulus_path, remote_stimulus)
    _calibration_capture(
        backend,
        capture_command=(
            f"arecord -D {shlex.quote(pcm)} -q -t wav -f S16_LE -r 48000 -c 1 "
            f"-d {capture_duration} {shlex.quote(remote_capture)}"
        ),
        playback_command=(
            f"pw-play --target {shlex.quote(str(target))} {shlex.quote(remote_stimulus)}"
        ),
        remote_capture=remote_capture,
        local_capture=capture,
        duration=capture_duration,
    )
    metrics = quick_aux_metrics(silence, capture)
    metrics.update(
        target=str(target),
        aux_volume=float(observed_volume),
        aux_volume_evidence_source=str(volume["source"]),
        stimulus=stimulus,
    )
    return metrics


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
            generate_stimulus(stimulus, mode="sine", seconds=4, dbfs=level, channels=2)
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
        raise HardwareRequired(
            "record a passing self-loop calibration before this stage"
        )
    floor = float(previous_stages["self-loop"]["noise_floor_dbfs"])
    stimulus = artifact / "stimulus.wav"
    generate_stimulus(stimulus, mode="sine", seconds=4, dbfs=-12, channels=2)
    remote_stimulus = remote_root + "/stimulus.wav"
    remote_capture = remote_root + "/capture.wav"
    local_capture = artifact / "capture.wav"
    _upload(backend, stimulus, remote_stimulus)
    if stage == "aux-loop":
        current = capture_snapshot(backend)
        status = (
            current.get("status") if isinstance(current.get("status"), dict) else {}
        )
        volume, volume_failures = aux_volume_evidence(
            status,
            expected_target=inventory.aux_target,
            expected_volume=inventory.aux_volume,
            allow_legacy_pre_candidate=True,
        )
        if volume_failures:
            raise HardwareRequired(
                "verify Pi AUX before calibration: " + "; ".join(volume_failures)
            )
        target = str(volume["target"])
        capture_command = (
            f"arecord -D {shlex.quote(pcm)} -q -t wav -f S16_LE -r 48000 -c 1 "
            f"-d {capture_duration} {shlex.quote(remote_capture)}"
        )
        playback_command = f"pw-play --target {shlex.quote(str(target))} {shlex.quote(remote_stimulus)}"
    else:
        current = capture_snapshot(backend)
        status = (
            current.get("status") if isinstance(current.get("status"), dict) else {}
        )
        endpoints = (
            status.get("endpoints") if isinstance(status.get("endpoints"), dict) else {}
        )
        microphone = endpoints.get("microphone")
        if not microphone:
            raise HardwareRequired(
                "no selected Lark/FIFINE microphone is ready for acoustic calibration"
            )
        capture_command = (
            f"timeout --signal=INT {capture_duration}s pw-record --target {shlex.quote(str(microphone))} "
            f"--rate 48000 --channels 1 --channel-map mono --format s16 {shlex.quote(remote_capture)}"
        )
        playback_command = (
            f"aplay -q -D {shlex.quote(pcm)} {shlex.quote(remote_stimulus)}"
        )
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
        raise SafetyFailure(
            f"cached last-good candidate {candidate_id} is unavailable: {exc}"
        ) from exc
    if manifest.get("candidate_id") != candidate_id:
        raise SafetyFailure(
            f"cached candidate identity is {manifest.get('candidate_id')!r}, expected {candidate_id!r}"
        )
    policies: dict[str, bytes] = {}
    policy_root = root / "policies"
    if policy_root.is_dir():
        for path in policy_root.iterdir():
            if path.is_file():
                policies[path.name] = path.read_bytes()
    if sha256_bytes(package) != manifest.get("package_tar_sha256"):
        raise SafetyFailure(f"cached candidate {candidate_id} package hash changed")
    expected_policies = manifest.get("policy_files")
    if not isinstance(expected_policies, dict):
        raise SafetyFailure(f"cached candidate {candidate_id} has no policy manifest")
    if set(policies) != set(expected_policies):
        raise SafetyFailure(
            f"cached candidate {candidate_id} policy set changed: "
            f"expected {sorted(expected_policies)}, observed {sorted(policies)}"
        )
    for name, content in policies.items():
        if "/" in name or name in {".", ".."}:
            raise SafetyFailure(
                f"cached candidate {candidate_id} has unsafe policy filename {name!r}"
            )
        if sha256_bytes(content) != expected_policies.get(name):
            raise SafetyFailure(
                f"cached candidate {candidate_id} policy {name!r} hash changed"
            )
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


def policy_restart_from_snapshot(
    snapshot: Mapping[str, Any], candidate: Candidate
) -> str:
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


def validate_evidence_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / "evidence-manifest.json"
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyFailure(
            f"evidence manifest is missing or malformed at {root}: {exc}"
        ) from exc
    entries = document.get("files") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise SafetyFailure(f"evidence manifest at {root} has no file list")
    expected: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise SafetyFailure(f"evidence manifest at {root} has a malformed entry")
        raw = str(entry["path"])
        relative = PurePosixPath(raw)
        if relative.is_absolute() or ".." in relative.parts or raw in expected:
            raise SafetyFailure(
                f"evidence manifest at {root} has unsafe/duplicate path {raw!r}"
            )
        expected.add(raw)
        path = root.joinpath(*relative.parts)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SafetyFailure(
                f"manifest-listed evidence {raw!r} is missing: {exc}"
            ) from exc
        if len(content) != entry.get("bytes") or sha256_bytes(content) != entry.get(
            "sha256"
        ):
            raise SafetyFailure(f"manifest-listed evidence {raw!r} was modified")
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "evidence-manifest.json"
    }
    if observed != expected:
        raise SafetyFailure(
            f"evidence file set changed at {root}: expected {sorted(expected)}, observed {sorted(observed)}"
        )
    return {
        "document": document,
        "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
    }


def _route_failures(
    mode: str, snapshot: Mapping[str, Any], inventory: Inventory
) -> list[str]:
    failures: list[str] = []
    status = snapshot.get("status")
    status = status if isinstance(status, dict) else {}
    phone = status_phone(snapshot)
    try:
        nodes, links = pipewire_node_graph(snapshot)
    except RigFailure as exc:
        return [str(exc)]
    if mode == "media":
        if phone.get("transport") != "MEDIA_ACTIVE":
            failures.append(
                f"phone transport is {phone.get('transport')!r}, not MEDIA_ACTIVE"
            )
        if phone.get("route_verified") is not True:
            failures.append("phone media route is not verified")
        media_node = phone.get("media_node")
        expected_target = phone.get("expected_target")
        if expected_target != inventory.aux_target:
            failures.append(
                f"phone expected target is {expected_target!r}, not fixed AUX {inventory.aux_target!r}"
            )
        props = nodes.get(str(media_node), {}) if isinstance(media_node, str) else {}
        prefix = f"bluez_input.{inventory.pixel_bt_mac.replace(':', '_')}.".lower()
        if (
            not isinstance(media_node, str)
            or not media_node.lower().startswith(prefix)
            or props.get("api.bluez5.profile") != "a2dp-source"
            or props.get("media.class") != "Stream/Output/Audio"
        ):
            failures.append(
                "status media node is not the qualified configured-phone A2DP stream"
            )
        # PipeWire represents a healthy stereo route with one Link object per channel.
        # The supervisor verifies the logical route; this independent evidence must
        # reject fan-out to another node without treating channel repetition as a
        # second logical route.
        media_targets = {target for source, target in links if source == media_node}
        if media_targets != {inventory.aux_target}:
            failures.append(
                "qualified phone media must target only the configured AUX node; "
                f"observed {sorted(media_targets)}"
            )
    else:
        if status.get("state") != "ACTIVE":
            failures.append(f"supervisor state is {status.get('state')!r}, not ACTIVE")
        if phone.get("transport") != "CALL":
            failures.append(f"phone transport is {phone.get('transport')!r}, not CALL")
        if phone.get("android_microphone_transport") is not True:
            failures.append("Android microphone transport is not verified open")
        endpoints = status.get("endpoints")
        endpoints = endpoints if isinstance(endpoints, dict) else {}
        hfp_sink = endpoints.get("hfp_sink")
        if not isinstance(hfp_sink, str) or hfp_sink not in nodes:
            failures.append(
                "verified HFP sink endpoint is absent from the PipeWire graph"
            )
        else:
            uplink_sources = [source for source, target in links if target == hfp_sink]
            if uplink_sources != ["output.bridge.mic"]:
                failures.append(
                    "HFP sink must have exactly one post-AEC output.bridge.mic uplink and "
                    f"no physical-microphone bypass; observed {sorted(uplink_sources)}"
                )
        aec = status.get("aec")
        if not isinstance(aec, dict) or aec.get("verified") is not True:
            failures.append("AEC graph is not verified for the call uplink")
    return failures


def _current_session_guard(
    session: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> None:
    if snapshot.get("boot_id") != session.get("baseline", {}).get("boot_id"):
        raise SafetyFailure(
            "Pi rebooted during the development session. Reboot alone cannot undo persistent "
            "WirePlumber policy; run session-stop to use the persistent exact preimages, "
            "and reflash this disposable development Pi if recovery cannot verify."
        )
    current = session.get("current_candidate")
    if isinstance(current, dict) and current.get("candidate_id"):
        candidate_id = str(current["candidate_id"])
        expected = f"LARKBRIDGE_DEV_CANDIDATE={candidate_id}"
        process = snapshot.get("supervisor_process")
        process_output = (
            str(process.get("stdout", "")) if isinstance(process, dict) else ""
        )
        if (
            not isinstance(process, dict)
            or process.get("returncode") != 0
            or expected not in process_output
            or f"/candidates/{candidate_id}/bridge_supervisor.py" not in process_output
        ):
            raise SafetyFailure(
                "running volatile candidate process is absent or stale; the deadman may have restored the deployed baseline"
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
        "tests",
        "-p",
        "test_*.py",
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
                        raise SafetyFailure(
                            f"unsafe path in candidate archive: {member.name}"
                        )
                    target = test_root.joinpath(*relative.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        stream = handle.extractfile(member)
                        if stream is None:
                            raise RigFailure(
                                f"could not read {member.name} from candidate archive"
                            )
                        target.write_bytes(stream.read())
            result = backend.local(
                command, cwd=test_root / "pi" / "bridged", timeout=180
            )
        else:
            result = backend.local(
                command, cwd=test_root / "pi" / "bridged", timeout=180
            )
    finally:
        if temporary is not None:
            temporary.cleanup()
    document = {"command": command, **asdict(result)}
    if result.returncode:
        raise RigFailure(
            "focused phone transport tests failed; candidate was not staged"
        )
    discovered = re.search(r"Ran\s+(\d+)\s+tests?\b", result.stdout + result.stderr)
    if discovered is None or int(discovered.group(1)) <= 0:
        raise RigFailure(
            "focused bridged test discovery did not run any tests; candidate was not staged"
        )
    document["tests_run"] = int(discovered.group(1))
    return document


def command_baseline(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    root = arguments.artifacts / "baselines" / stamp()
    root.mkdir(parents=True, exist_ok=False)
    snapshot = capture_snapshot(backend, full=True)
    evidence_failures = required_snapshot_evidence_failures(snapshot)
    if evidence_failures:
        raise RigFailure(
            "baseline evidence is incomplete: " + "; ".join(evidence_failures)
        )
    instrument = probe_instrument(backend, inventory.instrument)
    validate_mixer_map(instrument, inventory.instrument)
    fingerprint = instrument_fingerprint(inventory, instrument)
    quick_status: dict[str, Any]
    try:
        quick_calibration = load_quick_calibration(
            arguments.artifacts / QUICK_CALIBRATION_FILE
        )
        validate_quick_calibration(quick_calibration, fingerprint, inventory.thresholds)
        quick_status = {
            "valid": True,
            "sha256": sha256_bytes(canonical_json(quick_calibration)),
        }
    except HardwareRequired as exc:
        quick_status = {"valid": False, "reason": str(exc)}
    promotion_status: dict[str, Any]
    try:
        calibration = load_calibration(arguments.artifacts / CALIBRATION_FILE)
        validate_calibration(calibration, fingerprint, inventory.thresholds)
        promotion_status = {
            "valid": True,
            "sha256": sha256_bytes(canonical_json(calibration)),
        }
    except HardwareRequired as exc:
        promotion_status = {"valid": False, "reason": str(exc)}
    document = {
        "schema_version": 1,
        "created": utc_now(),
        "snapshot": snapshot,
        "instrument": instrument,
        "instrument_fingerprint": fingerprint,
        "quick_calibration": quick_status,
        "promotion_calibration": promotion_status,
        "aux_volume_required": inventory.aux_volume,
    }
    atomic_json(root / "baseline.json", document)
    _write_artifact_manifest(root)
    atomic_json(arguments.artifacts / "latest-baseline.json", document)
    print(
        json.dumps(
            {
                "baseline": str(root),
                "quick_calibration": quick_status,
                "promotion_calibration": promotion_status,
            },
            indent=2,
        )
    )
    return 0


def command_session_start(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    active = session_path(arguments.artifacts)
    if active.exists():
        try:
            existing = json.loads(active.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SafetyFailure(
                f"existing session checkpoint is malformed: {exc}"
            ) from exc
        if existing.get("status") in {"active", "restoring", "starting"}:
            raise SafetyFailure("a transparent-audio session is already active")
    candidate = resolve_candidate(arguments.candidate)
    focused = run_focused_tests(backend, candidate)
    confirm_candidate_still_current(candidate)
    cache_candidate(arguments.artifacts, candidate)
    baseline = capture_snapshot(backend, full=True)
    evidence_failures = required_snapshot_evidence_failures(baseline)
    if evidence_failures:
        raise RigFailure(
            "session baseline evidence is incomplete: " + "; ".join(evidence_failures)
        )
    instrument = probe_instrument(backend, inventory.instrument)
    validate_mixer_map(instrument, inventory.instrument)
    fingerprint = instrument_fingerprint(inventory, instrument)
    quick_calibration = load_quick_calibration(
        arguments.artifacts / QUICK_CALIBRATION_FILE
    )
    validate_quick_calibration(quick_calibration, fingerprint, inventory.thresholds)
    session_id = f"{stamp()}-{candidate.candidate_id}"
    preimages, recovery = capture_preimages(
        backend, candidate, session_id, instrument, inventory.instrument
    )
    session: dict[str, Any] = {
        "schema_version": 1,
        "status": "starting",
        "session_id": session_id,
        "started": utc_now(),
        "baseline": baseline,
        "instrument": instrument,
        "instrument_fingerprint": fingerprint,
        "quick_calibration_sha256": sha256_bytes(canonical_json(quick_calibration)),
        "focused_tests": focused,
        "recovery_root": recovery,
        "preimages": preimages,
        "current_candidate": candidate.manifest(),
        # Focused tests permit staging, but only a passing electrical smoke earns
        # last-good status. A first failed iteration restores the deployed baseline.
        "last_good_candidate": None,
        "iterations": [],
    }
    atomic_json(active, session)
    try:
        install_recovery_script(backend, session_id, recovery, inventory.instrument)
        arm_deadman(backend, session_id, recovery, arguments.deadman)
        restart_class = policy_restart_from_snapshot(baseline, candidate)
        apply_candidate(backend, candidate, session_id, restart_class)
        verified = verify_runtime(
            backend,
            inventory.aux_volume,
            inventory.aux_target,
            candidate.candidate_id,
        )
        # Rebuilding PipeWire may restore an old ALSA hardware value. Prepare and
        # read back the measurement fixture only after the final session-start
        # restart so every later code-only iteration inherits the calibrated state.
        prepared = prepare_mixer(
            backend,
            inventory,
            instrument,
            capture_gain=str(quick_calibration["capture_gain_request"]),
        )
        session["instrument"] = prepared
        session["start_restart_class"] = restart_class
        session["start_snapshot"] = verified
        session["status"] = "active"
        atomic_json(active, session)
    except BaseException as primary_failure:  # noqa: BLE001 - restore on interruption
        session["status"] = "restoring"
        session["start_failure"] = (
            f"{type(primary_failure).__name__}: {primary_failure}"
        )
        atomic_json(active, session)
        cleanup_failures: list[BaseException] = []
        try:
            report = restore_remote_session(backend, session)
        except BaseException as restore_failure:  # noqa: BLE001
            session["restore_failure"] = (
                f"{type(restore_failure).__name__}: {restore_failure}"
            )
            atomic_json(active, session)
            cleanup_failures.append(restore_failure)
        else:
            session["status"] = "start-failed-restored"
            session["restore"] = report
            session["restored"] = utc_now()
            atomic_json(active, session)
        _raise_primary_and_cleanup(primary_failure, cleanup_failures)
    print(
        json.dumps(
            {"session": session_id, "candidate": candidate.candidate_id}, indent=2
        )
    )
    return 0


def _restore_iteration_baseline(
    backend: Backend,
    session: dict[str, Any],
    checkpoint: Path,
    iteration: Mapping[str, Any],
    primary_failure: BaseException,
    rollback_failures: Sequence[BaseException] = (),
) -> None:
    """Restore deployed preimages or leave an honest, retryable restoring checkpoint."""

    session["status"] = "restoring"
    session["iteration_failure"] = (
        f"{type(primary_failure).__name__}: {primary_failure}"
    )
    if rollback_failures:
        session["rollback_failures"] = [
            f"{type(item).__name__}: {item}" for item in rollback_failures
        ]
    session["iterations"].append(dict(iteration, status="failed-restoring"))
    atomic_json(checkpoint, session)
    restore_failure: BaseException | None = None
    try:
        # Move the timer safely away from expiry before invoking the same flocked
        # recovery script manually.
        arm_deadman(
            backend,
            str(session["session_id"]),
            str(session["recovery_root"]),
            300,
        )
        report = restore_remote_session(backend, session)
    except BaseException as exc:  # noqa: BLE001 - exact recovery must remain retryable
        restore_failure = exc
        session["restore_failure"] = f"{type(exc).__name__}: {exc}"
        atomic_json(checkpoint, session)
    if restore_failure is not None:
        _raise_primary_and_cleanup(
            primary_failure, [*rollback_failures, restore_failure]
        )
    session["status"] = "iteration-failed-baseline-restored"
    session["current_candidate"] = None
    session["last_good_candidate"] = None
    session["restore"] = report
    session["restored"] = utc_now()
    session["iterations"][-1]["status"] = "failed-baseline-restored"
    atomic_json(checkpoint, session)
    _raise_primary_and_cleanup(primary_failure, rollback_failures)


def command_iterate(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    session = load_session(arguments.artifacts)
    candidate = resolve_candidate(arguments.candidate)
    focused = run_focused_tests(backend, candidate)
    confirm_candidate_still_current(candidate)
    cache_candidate(arguments.artifacts, candidate)
    before = capture_snapshot(backend, full=True)
    _current_session_guard(session, before)
    before_evidence_failures = required_snapshot_evidence_failures(before)
    if before_evidence_failures:
        raise RigFailure(
            "pre-iteration evidence is incomplete: "
            + "; ".join(before_evidence_failures)
        )
    if arguments.mode == "call":
        phone_before = status_phone(before)
        android_audio = str(
            before.get("android", {}).get("audio", {}).get("stdout", "")
        )
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
    quick_calibration = load_quick_calibration(
        arguments.artifacts / QUICK_CALIBRATION_FILE
    )
    validate_quick_calibration(
        quick_calibration,
        str(session["instrument_fingerprint"]),
        inventory.thresholds,
    )
    if sha256_bytes(canonical_json(quick_calibration)) != session.get(
        "quick_calibration_sha256"
    ):
        raise HardwareRequired(
            "the AUX wiring-session gate changed after session-start; stop and start a new session"
        )
    current_instrument = probe_instrument(backend, inventory.instrument)
    if (
        instrument_fingerprint(inventory, current_instrument)
        != session["instrument_fingerprint"]
    ):
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
    iteration_root = (
        arguments.artifacts
        / "iterations"
        / f"{stamp()}-{arguments.mode}-{candidate.candidate_id}"
    )
    iteration_root.mkdir(parents=True, exist_ok=False)
    record: dict[str, Any] = {
        "started": utc_now(),
        "session_id": session["session_id"],
        "mode": arguments.mode,
        "candidate": candidate.manifest(),
        "restart_class": restart_class,
        "focused_tests": focused,
        "before": before,
        "status": "running",
    }
    atomic_json(iteration_root / "iteration.json", record)
    try:
        arm_deadman(
            backend,
            str(session["session_id"]),
            str(session["recovery_root"]),
            arguments.deadman,
        )
        fresh = capture_condition_snapshot(backend)
        _current_session_guard(session, fresh)
        fresh_instrument = probe_instrument(backend, inventory.instrument)
        if instrument_fingerprint(inventory, fresh_instrument) != session.get(
            "instrument_fingerprint"
        ):
            raise HardwareRequired(
                "GeneralPlus fixture changed immediately before mutation"
            )
        validate_prepared_mixer_state(
            fresh_instrument,
            inventory.instrument,
            expected_capture_value=str(
                session["instrument"].get("prepared_capture_gain_value", "0")
            ),
        )
        apply_candidate(
            backend,
            candidate,
            str(session["session_id"]),
            restart_class,
            remove_policy_names=sorted(previous_policy_names - candidate_policy_names),
        )
        verify_runtime(
            backend,
            inventory.aux_volume,
            inventory.aux_target,
            candidate.candidate_id,
            mode=arguments.mode,
        )
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
                quick_calibration=quick_calibration,
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
        failures = required_snapshot_evidence_failures(after)
        failures.extend(_route_failures(arguments.mode, after, inventory))
        restarts, restart_failures = restart_counter_failures(before, after)
        failures.extend(restart_failures)
        recovery_delta = watchdog_recoveries(after) - watchdog_recoveries(before)
        if recovery_delta:
            failures.append(f"Bluetooth watchdog performed {recovery_delta} recoveries")
        kernel_errors = new_kernel_errors(before, after)
        if kernel_errors:
            failures.append(f"new USB/HCI errors: {kernel_errors}")
        metrics = smoke.get("metrics") if isinstance(smoke, dict) else {}
        if isinstance(metrics, dict) and metrics.get("verdict") != "PASS":
            failures.extend(
                str(item) for item in metrics.get("failures", ["measurement failed"])
            )
        record.update(
            finished=utc_now(),
            smoke=smoke,
            after=after,
            restart_deltas=restarts,
            watchdog_recovery_delta=recovery_delta,
            new_kernel_errors=kernel_errors,
            failures=failures,
            status="passed" if not failures else "failed",
            verdict="PASS" if not failures else "FAIL",
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
        print(
            json.dumps({"verdict": "PASS", "artifact": str(iteration_root)}, indent=2)
        )
        return 0
    except BaseException as exc:
        record.update(
            finished=utc_now(),
            status="failed",
            verdict="FAIL",
            error=f"{type(exc).__name__}: {exc}",
        )
        atomic_json(iteration_root / "iteration.json", record)
        _write_artifact_manifest(iteration_root)
        failed_iteration = {
            "mode": arguments.mode,
            "candidate_id": candidate.candidate_id,
            "artifact": str(iteration_root),
        }
        last_good_id = session.get("last_good_candidate")
        if not isinstance(last_good_id, str) or not last_good_id:
            _restore_iteration_baseline(
                backend,
                session,
                session_path(arguments.artifacts),
                failed_iteration,
                exc,
            )
        try:
            last_good = load_cached_candidate(arguments.artifacts, last_good_id)
            rollback_class = classify_restart(candidate.manifest(), last_good)
            apply_candidate(
                backend,
                last_good,
                str(session["session_id"]),
                rollback_class,
                remove_policy_names=sorted(
                    set(candidate.policy_files) - set(last_good.policy_files)
                ),
            )
            verify_runtime(
                backend,
                inventory.aux_volume,
                inventory.aux_target,
                last_good.candidate_id,
                mode=arguments.mode,
            )
        except BaseException as rollback_exc:  # noqa: BLE001
            _restore_iteration_baseline(
                backend,
                session,
                session_path(arguments.artifacts),
                failed_iteration,
                exc,
                (rollback_exc,),
            )
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


def command_transition(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    session = load_session(arguments.artifacts)
    arm_deadman(
        backend,
        str(session["session_id"]),
        str(session["recovery_root"]),
        900,
    )
    before = capture_snapshot(backend, full=True)
    _current_session_guard(session, before)
    evidence_failures = required_snapshot_evidence_failures(before)
    if evidence_failures:
        raise RigFailure(
            "pre-transition evidence is incomplete: " + "; ".join(evidence_failures)
        )
    before_transport = status_phone(before).get("transport")
    if arguments.expect == "call":
        if before_transport not in {"MEDIA_ACTIVE", "MEDIA_READY"}:
            raise HardwareRequired(
                "prepare the Pixel in MEDIA_ACTIVE or MEDIA_READY, start this transition timer, "
                "then launch Discord and select LarkBridge Bluetooth audio"
            )
        print(
            "Transition timer armed: start the Discord call and select LarkBridge Bluetooth audio now.",
            file=sys.stderr,
        )
    elif arguments.expect == "paused" and before_transport != "CALL":
        raise HardwareRequired(
            "prepare an active Discord CALL, then end it while this transition timer waits"
        )
    elif arguments.expect == "media" and before_transport not in {
        "MEDIA_READY",
        "MEDIA_RESTORED_APP_PAUSED",
    }:
        raise HardwareRequired(
            "prepare MEDIA_READY or MEDIA_RESTORED_APP_PAUSED, then start playback while this timer waits"
        )
    expected = {
        "media": "MEDIA_ACTIVE",
        "call": "CALL",
        "paused": "MEDIA_RESTORED_APP_PAUSED",
    }[arguments.expect]
    _phone, elapsed = wait_phone_transport(backend, expected, timeout=arguments.timeout)
    # Preserve a complete evidence snapshot only once the cheap condition poll has
    # observed the transition. Full pw-dump/ADB sampling is deliberately not a timer.
    after = capture_snapshot(backend, full=True)
    _current_session_guard(session, after)
    evidence_failures = required_snapshot_evidence_failures(after)
    if evidence_failures:
        raise RigFailure(
            "post-transition evidence is incomplete: " + "; ".join(evidence_failures)
        )
    if status_phone(after).get("transport") != expected:
        raise RigFailure(
            f"phone transport changed again before evidence capture; expected {expected}, "
            f"observed {status_phone(after).get('transport')!r}"
        )
    root = arguments.artifacts / "transitions" / f"{stamp()}-{arguments.expect}"
    root.mkdir(parents=True, exist_ok=False)
    document = {
        "created": utc_now(),
        "session_id": session["session_id"],
        "expect": arguments.expect,
        "expected_transport": expected,
        "elapsed_s": round(elapsed, 3),
        "before": before,
        "after": after,
    }
    atomic_json(root / "transition.json", document)
    _write_artifact_manifest(root)
    print(
        json.dumps({"transition": expected, "elapsed_s": round(elapsed, 3)}, indent=2)
    )
    return 0


def command_session_stop(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    session = load_session(arguments.artifacts, allow_restoring=True)
    session["status"] = "restoring"
    atomic_json(session_path(arguments.artifacts), session)
    install_recovery_script(
        backend,
        str(session["session_id"]),
        str(session["recovery_root"]),
        inventory.instrument,
    )
    arm_deadman(
        backend,
        str(session["session_id"]),
        str(session["recovery_root"]),
        300,
    )
    report = restore_remote_session(backend, session)
    session["status"] = "stopped"
    session["stopped"] = utc_now()
    session["restore"] = report
    atomic_json(session_path(arguments.artifacts), session)
    print(json.dumps(report, indent=2))
    return 0


def command_quick_calibrate(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    require_no_open_session(arguments.artifacts)
    if not arguments.hardware_ready:
        raise HardwareRequired(
            "pause phone/media playback, connect Pi AUX directly to the GeneralPlus input, "
            f"and leave AUX volume at {inventory.aux_volume:.2f}; then repeat with --hardware-ready"
        )
    require_fixture_label(inventory.cable_id, "GeneralPlus AUX cable")
    requested_gain = arguments.capture_gain or "0%"
    if not re.fullmatch(r"(?:100|[0-9]{1,2})%", requested_gain):
        raise RigFailure("--capture-gain must be a percentage from 0% through 100%")
    report = probe_instrument(backend, inventory.instrument)
    validate_mixer_map(report, inventory.instrument)
    fingerprint = instrument_fingerprint(inventory, report)
    artifact = arguments.artifacts / "calibration" / f"{stamp()}-quick-aux"
    guard_id, recovery = start_mixer_guard(
        backend, report, inventory.instrument, seconds=600
    )
    primary_failure: BaseException | None = None
    try:
        # Never inherit the instrument's previous gain. Touch the qualified minimum
        # first, then apply an explicitly recorded higher gain only when requested.
        prepared = prepare_mixer(backend, inventory, report, capture_gain="0%")
        if requested_gain != "0%":
            prepared = prepare_mixer(
                backend, inventory, prepared, capture_gain=requested_gain
            )
        metrics = perform_quick_calibration(
            backend,
            inventory,
            prepared,
            artifact,
        )
    except BaseException as exc:  # noqa: BLE001 - cleanup must survive interruption
        primary_failure = exc
    restore = finish_mixer_guard(backend, guard_id, recovery, primary_failure)

    failures = quick_calibration_failures(metrics, inventory.thresholds)
    metrics["recorded"] = utc_now()
    atomic_json(artifact / "metrics.json", metrics)
    evidence = _write_artifact_manifest(artifact)
    evidence_path = artifact / "evidence-manifest.json"
    document = {
        "schema_version": 1,
        "kind": "aux-wiring-session",
        "recorded": utc_now(),
        "instrument_fingerprint": fingerprint,
        "capture_gain_request": requested_gain,
        "prepared_capture_gain_value": prepared.get("prepared_capture_gain_value"),
        "instrument": {
            "usb_id": inventory.instrument.usb_id,
            "port_path": inventory.instrument.port_path,
            "alsa_id_at_measurement": report.get("alsa_id"),
            "ephemeral_card_number": report.get("ephemeral_card_number"),
            "mixer_map_sha256": mixer_map_sha256(str(report.get("mixer_contents", ""))),
            "cable_id": inventory.cable_id,
            "aux_target": inventory.aux_target,
            "aux_volume": inventory.aux_volume,
        },
        "metrics": metrics,
        "thresholds": {
            "max_noise_floor_dbfs": inventory.thresholds.noise_floor_dbfs,
            "min_wiring_continuity_margin_db": (
                inventory.thresholds.quick_aux_above_floor_db
            ),
            "max_clipping_pct": inventory.thresholds.clipping_pct,
            "strict_media_margin_db": inventory.thresholds.aux_above_floor_db,
            "scope": "quick wiring continuity only; not promotion acceptance",
        },
        "artifact": str(artifact),
        "evidence": evidence,
        "evidence_manifest_sha256": sha256_bytes(evidence_path.read_bytes()),
        "last_mixer_restore": restore,
        "verdict": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    path = arguments.artifacts / QUICK_CALIBRATION_FILE
    atomic_json(path, document)
    print(
        json.dumps(
            {
                "verdict": document["verdict"],
                "quick_calibration": str(path),
                "artifact": str(artifact),
                "capture_gain": requested_gain,
                "failures": failures,
                "next": (
                    "run session-start; this gate is reused without recapturing on each iteration"
                    if not failures
                    else "correct the AUX wiring/gain and rerun quick-calibrate"
                ),
            },
            indent=2,
        )
    )
    return 0 if not failures else 1


def command_calibrate(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    require_no_open_session(arguments.artifacts)
    if not arguments.hardware_ready:
        instructions = {
            "self-loop": "connect GeneralPlus output directly to its input",
            "aux-loop": "connect Pi AUX output to the GeneralPlus input",
            "acoustic": "connect GeneralPlus output to the fixed speaker and keep the speaker/microphone position fixed",
        }[arguments.stage]
        raise HardwareRequired(f"{instructions}; then repeat with --hardware-ready")
    if arguments.stage in {"aux-loop", "acoustic"}:
        require_fixture_label(inventory.cable_id, "GeneralPlus cable")
    if arguments.stage == "acoustic":
        require_fixture_label(inventory.speaker_position_id, "speaker-position")
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
            raise HardwareRequired(
                "record self-loop calibration to select capture gain first"
            )
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
    primary_failure: BaseException | None = None
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
    except BaseException as exc:  # noqa: BLE001 - cleanup must survive interruption
        primary_failure = exc
    restore = finish_mixer_guard(backend, guard_id, recovery, primary_failure)
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


def command_accept(
    arguments: argparse.Namespace, _inventory: Inventory, _backend: Backend | None
) -> int:
    session = load_session(arguments.artifacts)
    passed: dict[str, Mapping[str, Any]] = {}
    for item in session.get("iterations", []):
        if isinstance(item, dict) and item.get("status") == "passed":
            passed[str(item.get("mode"))] = item
    missing = [mode for mode in ("media", "call") if mode not in passed]
    if missing:
        raise RigFailure(
            "cannot accept: no passing " + " and ".join(missing) + " iteration"
        )
    candidate_ids = {str(item.get("candidate_id")) for item in passed.values()}
    if len(candidate_ids) != 1:
        raise RigFailure(
            "cannot accept: latest media and call passes used different candidates"
        )
    candidate_id = candidate_ids.pop()
    current = session.get("current_candidate")
    if not isinstance(current, dict) or current.get("candidate_id") != candidate_id:
        raise RigFailure(
            "cannot accept evidence for a candidate that is not currently active"
        )
    files: list[dict[str, Any]] = []
    for mode in ("media", "call"):
        artifact = Path(str(passed[mode]["artifact"]))
        integrity = validate_evidence_manifest(artifact)
        try:
            iteration = json.loads(
                (artifact / "iteration.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise SafetyFailure(
                f"accepted {mode} iteration record is unavailable: {exc}"
            ) from exc
        candidate = iteration.get("candidate") if isinstance(iteration, dict) else None
        if (
            not isinstance(iteration, dict)
            or iteration.get("mode") != mode
            or iteration.get("status") != "passed"
            or iteration.get("verdict") != "PASS"
            or iteration.get("session_id") != session.get("session_id")
            or not isinstance(candidate, dict)
            or candidate.get("candidate_id") != candidate_id
        ):
            raise SafetyFailure(
                f"accepted {mode} iteration evidence does not match its session/candidate/verdict"
            )
        files.append(
            {
                "mode": mode,
                "artifact": str(artifact),
                "manifest_sha256": integrity["manifest_sha256"],
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
    for latest in reversed(milestones):
        try:
            document = json.loads(latest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SafetyFailure(
                f"milestone record is malformed: {latest}: {exc}"
            ) from exc
        if (
            document.get("candidate_id") != candidate_id
            or document.get("verdict") != "PASS"
        ):
            continue
        if document.get("session_id") != session.get("session_id"):
            raise SafetyFailure(
                "passing Discord far-end evidence for this candidate belongs to a stale session"
            )
        integrity = validate_evidence_manifest(latest.parent)
        if document.get("kind") != "discord-far-end":
            raise SafetyFailure("selected milestone evidence has the wrong kind")
        acceptance["discord_far_end"] = {
            "artifact": str(latest.parent),
            "manifest_sha256": integrity["manifest_sha256"],
        }
        break
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
    level, lag, correlation = correlated_level(
        reference, observed, reference_rate, max_lag_s=5.0
    )
    return {
        "correlated_level": level,
        "lag_ms": round(lag * 1000.0 / reference_rate, 2),
        "correlation": round(correlation, 4),
        "clean_sha256": sha256_bytes(clean.read_bytes()),
        "far_end_sha256": sha256_bytes(far_end.read_bytes()),
    }


def command_milestone(
    arguments: argparse.Namespace, inventory: Inventory, backend: Backend
) -> int:
    session = load_session(arguments.artifacts)
    arm_deadman(
        backend,
        str(session["session_id"]),
        str(session["recovery_root"]),
        900,
    )
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
        token.replace("{out}", str(far_end)).replace(
            "{seconds}", str(arguments.seconds)
        )
        for token in command_template
    ]
    stimulus = generate_stimulus(
        root / "near-end-stimulus.wav", mode="speech", seconds=arguments.seconds
    )
    remote_root = f"{RUNTIME_ROOT}/milestone-{stamp()}"
    remote_stimulus = remote_root + "/stimulus.wav"
    _upload(backend, root / "near-end-stimulus.wav", remote_stimulus)
    instrument = probe_instrument(backend, inventory.instrument)
    if instrument_fingerprint(inventory, instrument) != session.get(
        "instrument_fingerprint"
    ):
        raise HardwareRequired(
            "GeneralPlus fixture identity changed before milestone capture"
        )
    validate_prepared_mixer_state(
        instrument,
        inventory.instrument,
        expected_capture_value=str(
            session["instrument"].get("prepared_capture_gain_value", "0")
        ),
    )
    capture_unit = "larkbridge-e19-milestone-capture"
    play_unit = "larkbridge-e19-milestone-stimulus"
    primary_failure: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    correlation: dict[str, Any] | None = None
    observer_files: list[dict[str, Any]] = []
    try:
        backend.pi(
            f"mkdir -p {shlex.quote(remote_root)}; systemd-run --user --unit={capture_unit} --collect "
            f"python3 /home/admin/rpi-lark-bridge/rig/pi/measure/call_capture.py --label milestone "
            f"--seconds {arguments.seconds:g} --mode echo --outdir {shlex.quote(remote_root)}",
            timeout=20,
        ).require("start Pi milestone taps")
        wait_unit_active(backend, capture_unit + ".service")
        backend.pi(
            f"systemd-run --user --unit={play_unit} --collect /usr/bin/aplay -q "
            f"-D {shlex.quote('plughw:CARD=' + str(instrument['alsa_id']) + ',DEV=0')} "
            f"{shlex.quote(remote_stimulus)}",
            timeout=20,
        ).require("start milestone near-end stimulus")
        host_capture = backend.local(command, cwd=REPO, timeout=arguments.seconds + 30)
        host_capture.require("Windows Discord far-end loopback capture")
        wait_unit_inactive(
            backend, capture_unit + ".service", timeout=arguments.seconds + 30
        )
        pi_capture = root / "pi-call-capture"
        backend.fetch(remote_root, pi_capture, recursive=True)
        _reference, _raw, clean = _find_call_wavs(pi_capture)
        correlation = _correlate_far_end(clean, far_end)
        observer_files = [
            {
                "path": str(path),
                "sha256": sha256_bytes(path.read_bytes()),
            }
            for path in sorted(pi_capture.rglob("*pw*top*"))
            if path.is_file()
        ]
    except BaseException as exc:  # noqa: BLE001 - cleanup must survive interruption
        primary_failure = exc
    try:
        stop_and_verify_units(
            backend,
            (capture_unit + ".service", play_unit + ".service"),
            action="stop and verify milestone units",
            runtime_root=remote_root,
        )
    except BaseException as exc:  # noqa: BLE001 - preserve with primary failure
        cleanup_failures.append(exc)
    _raise_primary_and_cleanup(primary_failure, cleanup_failures)
    assert correlation is not None
    failures = (
        []
        if correlation["correlation"] >= 0.3
        else [f"far-end correlation {correlation['correlation']:.4f} is below 0.3"]
    )
    document = {
        "schema_version": 1,
        "created": utc_now(),
        "session_id": session["session_id"],
        "candidate_id": session["current_candidate"]["candidate_id"],
        "kind": "discord-far-end",
        "separate_from_inner_loop": True,
        "operator_confirmed_windows_input_muted": True,
        "capture_command": command,
        "stimulus": stimulus,
        "instrument": instrument,
        "pw_top_evidence": observer_files,
        "correlation": correlation,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }
    atomic_json(root / "milestone.json", document)
    _write_artifact_manifest(root)
    print(json.dumps({"verdict": document["verdict"], "artifact": str(root)}, indent=2))
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    commands = parser.add_subparsers(dest="command", required=True)

    def live(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--live",
            action="store_true",
            help="authorize this invocation to contact and, where documented, mutate the bench",
        )

    baseline = commands.add_parser(
        "baseline", help="capture a read-only dynamic baseline"
    )
    live(baseline)

    start = commands.add_parser(
        "session-start",
        help="apply policy once, stage the RAM-only candidate, and start the rapid session",
    )
    start.add_argument("--candidate", required=True)
    start.add_argument("--deadman", type=int, default=900)
    live(start)

    iterate = commands.add_parser(
        "iterate",
        help="run the fast RAM-only candidate/restart/capture/score loop",
    )
    iterate.add_argument("--mode", choices=("media", "call"), required=True)
    iterate.add_argument("--candidate", required=True)
    iterate.add_argument("--seconds", type=float, default=25.0)
    iterate.add_argument("--deadman", type=int, default=900)
    live(iterate)

    transition = commands.add_parser(
        "transition", help="wait for a measured phone transport state"
    )
    transition.add_argument(
        "--expect", choices=("media", "call", "paused"), required=True
    )
    transition.add_argument("--timeout", type=float, default=12.0)
    live(transition)

    stop = commands.add_parser("session-stop", help="restore exact deployed preimages")
    live(stop)

    commands.add_parser("accept", help="write a compact accepted-candidate manifest")

    quick = commands.add_parser(
        "quick-calibrate",
        help="once per AUX wiring session: capture the floor and direct AUX tone gate",
    )
    quick.add_argument("--hardware-ready", action="store_true")
    quick.add_argument(
        "--capture-gain",
        help="explicit GeneralPlus capture gain; always touches 0% first (default: 0%)",
    )
    live(quick)

    calibrate = commands.add_parser(
        "calibrate",
        help="promotion only: record one full three-stage fixture qualification",
    )
    calibrate.add_argument(
        "--stage", choices=REQUIRED_CALIBRATION_STAGES, required=True
    )
    calibrate.add_argument("--hardware-ready", action="store_true")
    calibrate.add_argument(
        "--capture-gain",
        help="explicit GeneralPlus capture gain; self-loop begins at 0% by default",
    )
    live(calibrate)

    milestone = commands.add_parser(
        "milestone",
        help="record a real Discord far end separately from the synthetic loop",
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
            with session_lock(arguments.artifacts):
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
            "quick-calibrate": command_quick_calibrate,
            "calibrate": command_calibrate,
            "milestone": command_milestone,
        }
        handler = handlers[arguments.command]
        if arguments.command in {
            "session-start",
            "iterate",
            "transition",
            "session-stop",
            "milestone",
            "quick-calibrate",
            "calibrate",
        }:
            with session_lock(arguments.artifacts):
                return handler(arguments, inventory, selected_backend)
        return handler(arguments, inventory, selected_backend)
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
        print(
            json.dumps({"verdict": "FAIL", "error": str(exc)}, indent=2),
            file=sys.stderr,
        )
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print(json.dumps({"verdict": "INTERRUPTED"}), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
