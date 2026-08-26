#!/usr/bin/env python3
"""Own the transient Mode 1W call graph, including optional WebRTC AEC.

The supervisor never processes PCM in Python. It owns policy and process lifetime while
PipeWire's compiled WebRTC module performs the real-time DSP. HFP endpoints exist only
during a call, so every call-specific stream is built transactionally and torn down when
an endpoint disappears. There is deliberately no default-device fallback.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import controller_roles

POLL_SECONDS = 2.0
OUTPUT_EVENT_POLL_SECONDS = 0.05
BUILD_TIMEOUT_SECONDS = 10.0
ATTACH_GRACE_SECONDS = 4.0
MAX_BUILD_ATTEMPTS = 5
LARK_PCM_WINDOW_SECONDS = 0.020
LARK_PCM_ACTIVE_WINDOWS = 18
LARK_PCM_INACTIVE_WINDOWS = 88
LARK_PCM_RETRY_SECONDS = 2.0
LARK_PCM_MONITOR_NODE = "bridge.lark.liveness"
# The attached A1 receiver produced exact-zero PCM with both transmitters off and continuous
# nonzero PCM with TX1, TX2, or both linked. A linked-but-muted transmitter was not available
# to qualify, so this heuristic is deliberately confined to the known Lark/FIFINE policy.
# FAILED used to be terminal: tick() returned immediately and update_signature() only
# resets `attempts` when (call_up, lark, output_up) CHANGES. Measured in E13 -- five AEC
# host deaths in a burst left the unit permanently dead with the call still up, the far
# end still arriving on the downlink at -12 dBFS, and the speaker at -200 dBFS. Nothing
# was unplugged, so nothing changed the signature, so nothing ever retried.
#
# MAX_BUILD_ATTEMPTS exists to stop a hot rebuild loop, which is worth keeping. Dying
# forever is not. So FAILED now retries on a long interval: rare enough not to hammer a
# genuinely broken graph, frequent enough that a user mid-call gets their audio back
# without knowing this component exists.
FAILED_RETRY_SECONDS = 60.0

DEFAULT_LARK = (
    "alsa_input.usb-Shenzhen_Hollyland_Technology_Co._Ltd_Wireless_Microphone"
    "_Wireless_Microphone-01.analog-stereo"
)
DEFAULT_LARK_COMPONENT = "USB3547:0407"
DEFAULT_WIRED_OUT = "alsa_output.platform-3f00b840.mailbox.stereo-fallback"
DEFAULT_PHONE_MAC = "5C:33:7B:CB:BF:C5"

# Sentinel for tick()'s output argument, distinct from None. None means "nothing is
# playable"; this means "the caller does not participate in output selection, apply the
# pre-selection rule". Overloading None for both meanings was ambiguous enough to produce a
# wrong test within minutes of being written, so the two cases are now separate values. A NUL
# byte cannot occur in a PipeWire node name, so this can never collide with a real one.
DERIVE_OUTPUT = chr(0) + "derive-output"

AEC_SOURCE = "bridge.aec.source"
AEC_SINK = "bridge.aec.sink"
AEC_CAPTURE = "echo-cancel-capture"
AEC_PLAYBACK = "echo-cancel-playback"
VOLUME_PERCENT_RE = re.compile(r"/\s*([0-9]+(?:\.[0-9]+)?)%\s*/")
WPCTL_VOLUME_RE = re.compile(r"Volume:\s+([0-9]+(?:\.[0-9]+)?)")

log = logging.getLogger("bridge-supervisor")


class State(str, Enum):
    CALL_DOWN = "CALL_DOWN"
    WAITING_MIC = "WAITING_MIC"
    DISCOVERING = "DISCOVERING"
    BUILDING = "BUILDING"
    SWITCHING = "SWITCHING"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    SAFE = "SAFE"


@dataclass(frozen=True)
class AecSettings:
    enabled: bool = False
    method: str = "webrtc"
    rate: int = 48_000
    channels: int = 1
    failure_policy: str = "fail_closed"
    high_pass_filter: bool = True
    noise_suppression: bool = False
    gain_control: bool = False
    voice_detection: bool = False
    transient_suppression: bool = True
    # The WebRTC module processes in 10 ms blocks and, left alone, asks the graph for a
    # 480-frame quantum. That drags the whole graph down with it: the onboard sink drops
    # from 2048 to min-quantum 256, and the bcm2835 output cannot hold a 256-frame buffer
    # under call load -- it underruns, and every underrun is an audible click in the
    # far-end audio. E10 measured 1920 as the only timing that stayed clean across ten
    # trials, so that is the default here rather than a bench-only override.
    node_latency_frames: int | None = 1920
    play_delay_frames: int | None = None


@dataclass(frozen=True)
class Settings:
    aec: AecSettings
    lark_node: str = DEFAULT_LARK
    lark_component: str = DEFAULT_LARK_COMPONENT
    wired_output: str = DEFAULT_WIRED_OUT
    phone_mac: str = DEFAULT_PHONE_MAC
    status_path: Path = Path("/tmp/bridge-status.json")
    config_path: Path | None = None
    # Where call audio goes. `wired_output` above remains the Mode 1W default and the
    # terminal fallback; these three describe how a different output can be chosen.
    #
    # `desired_output` is only the BOOT-TIME default. Runtime selection cannot live in
    # config: load_settings() runs exactly once in main() and there is no reload path, so a
    # config-driven selector would need a restart, and restarting the supervisor during
    # active SCO is the suspected trigger for the E08 controller wedge.
    mode: str = "bluetooth-wired"
    desired_output: str | None = None
    speaker_adapter: str | None = None
    fallback_to_wired: bool = True
    controller_roles: controller_roles.ControllerRoles | None = None
    # ``None`` keeps deliberately minimal test/bench Settings free from host mixer I/O.
    # load_settings() supplies the production default and validates the durable value.
    wired_output_volume: float | None = None
    # ``None`` keeps direct test/bench construction free from mixer I/O. Production
    # settings always provide explicit software controls for output.bridge.mic.
    microphone_candidates: tuple[Any, ...] = ()
    mic_gain_db: float | None = None
    mic_muted: bool | None = None

    @property
    def hfp_sink(self) -> str:
        return f"bluez_output.{self.phone_mac.replace(':', '_')}.1"

    @property
    def hfp_source(self) -> str:
        return f"bluez_input.{self.phone_mac.replace(':', '_')}.0"


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def toml_opt_int(data: dict[str, Any], key: str, default: int | None) -> int | None:
    """Absent key keeps the default. TOML has no null, so omitting is how you opt out."""
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"audio.aec.{key} must be an integer or null")
    return value


def toml_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise TypeError(f"audio.aec.{key} must be a boolean")
    return value


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "bridge.toml"


def desire_path() -> Path:
    """Where a front-end writes the user's chosen output.

    A FILE, not a socket, and that is a deliberate simplification. The supervisor's unit is
    sandboxed with ProtectSystem=strict and ReadWritePaths=%t, so $XDG_RUNTIME_DIR is the one
    place it may touch. The main loop watches this file's timestamp cheaply between full graph
    polls, so selection wakes in tens of milliseconds without a server loop, protocol version,
    or second address family. Any front-end -- CLI or phone bridge -- writes this one file.

    It lives on tmpfs on purpose. The DURABLE default is config's [devices.output]; this is
    the runtime override, so selecting an output costs zero LARKDATA writes and cannot
    threaten the ~65 KB/120 s idle-write bar E14 set.
    """
    return default_status_path().parent / "bridge-output.json"


def read_desire(path: Path | None = None) -> str | None:
    """The currently requested output id, or None to mean 'use the configured default'."""
    target = path or desire_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("desired_id")
    return str(value) if value else None


def write_desire(output_id: str | None, source: str = "unknown", path: Path | None = None) -> None:
    """Record a selection. Atomic, so a reader never sees a half-written file."""
    target = path or desire_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"desired_id": output_id, "source": source, "timestamp": time.time()}
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=".bridge-output-", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.write(chr(10))
        temporary = Path(handle.name)
    os.replace(temporary, target)


def resolve_output(
    nodes: NodeMap,
    settings: Settings,
    desired_id: str | None,
    *,
    bluez_objects: dict[str, dict] | None = None,
    output_controller_ready: bool = True,
):
    """Decide which node call audio should go to. Returns (node_name, report_or_None).

    The import is deliberately lazy. rig/pi/measure/aec_bench.py loads this file by path with
    importlib, so pi/bridged is NOT on sys.path in that process; a module-level `import
    outputs` would break the AEC bench. When outputs is unavailable the behaviour collapses
    to exactly what shipped before -- the configured wired output if present, else nothing.
    """
    try:
        import outputs as outputs_module
    except ImportError:
        node = settings.wired_output if settings.wired_output in nodes else None
        return node, None

    candidates = outputs_module.candidates(
        nodes,
        objects=bluez_objects,
        speaker_adapter=settings.speaker_adapter,
    )
    if not output_controller_ready:
        candidates = [candidate for candidate in candidates if candidate.kind != "a2dp"]
    resolution = outputs_module.resolve(
        desired_id or settings.desired_output,
        candidates,
        fallback=settings.fallback_to_wired,
        prefer_speaker=settings.mode == "bluetooth",
    )
    report = {
        "candidates": [c.as_dict() for c in candidates],
        **resolution.as_dict(),
    }
    return resolution.node, report


def default_status_path() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
    return runtime / "bridge-status.json"


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or Path(os.environ.get("BRIDGE_CONFIG", default_config_path()))
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    else:
        log.warning("config %s is absent; retaining safe AEC-off defaults", config_path)

    # Keep the AEC bench importable by loading the role module only when settings are read.
    import controller_roles as controller_roles_module

    roles = None
    if controller_roles_module.has_controller_role_fields(data):
        roles = controller_roles_module.parse_controller_roles(data)

    audio_data = data.get("audio") or {}
    if not isinstance(audio_data, dict):
        raise TypeError("audio must be a TOML table")
    raw_wired_volume = audio_data.get("wired_output_volume", 0.85)
    if isinstance(raw_wired_volume, bool) or not isinstance(raw_wired_volume, (int, float)):
        raise TypeError("audio.wired_output_volume must be a number")
    wired_output_volume = float(raw_wired_volume)
    if not 0.0 <= wired_output_volume <= 1.0:
        raise ValueError("audio.wired_output_volume must be between 0.0 and 1.0")

    raw_mic_gain = audio_data.get("mic_gain_db", 0.0)
    if isinstance(raw_mic_gain, bool) or not isinstance(raw_mic_gain, (int, float)):
        raise TypeError("audio.mic_gain_db must be a finite number")
    mic_gain_db = float(raw_mic_gain)
    if not math.isfinite(mic_gain_db):
        raise ValueError("audio.mic_gain_db must be finite")
    mic_muted = toml_bool(audio_data, "mic_muted", False)

    # The resolver owns microphone schema/precedence. Keep this import local because a few
    # measurement tools load this module by path only to reuse NativeAecHost.
    import microphones as microphones_module

    microphone_candidates = microphones_module.parse_microphone_candidates(
        data,
        environ=os.environ,
        default_lark_node=DEFAULT_LARK,
        default_lark_component=DEFAULT_LARK_COMPONENT,
    )
    devices_data = data.get("devices") or {}
    if (
        isinstance(devices_data, dict)
        and "microphones" in devices_data
        and devices_data.get("lark")
    ):
        log.warning(
            "devices.lark is ignored because the explicit devices.microphones list is authoritative"
        )
    configured_lark = next(
        (candidate for candidate in microphone_candidates if candidate.id == "lark-a1"),
        None,
    )

    aec_data = audio_data.get("aec") or {}
    enabled = toml_bool(aec_data, "enabled", False)
    if "BRIDGE_AEC_ENABLED" in os.environ:
        enabled = parse_bool(os.environ["BRIDGE_AEC_ENABLED"])
    method = str(aec_data.get("method", "webrtc"))
    rate = int(aec_data.get("rate", 48_000))
    channels = int(aec_data.get("channels", 1))
    failure_policy = str(aec_data.get("failure_policy", "fail_closed"))
    high_pass_filter = toml_bool(aec_data, "high_pass_filter", True)
    noise_suppression = toml_bool(aec_data, "noise_suppression", False)
    gain_control = toml_bool(aec_data, "gain_control", False)
    voice_detection = toml_bool(aec_data, "voice_detection", False)
    transient_suppression = toml_bool(aec_data, "transient_suppression", True)
    node_latency_frames = toml_opt_int(aec_data, "node_latency_frames", 1920)
    play_delay_frames = toml_opt_int(aec_data, "play_delay_frames", None)

    if node_latency_frames is not None and node_latency_frames <= 0:
        raise ValueError("audio.aec.node_latency_frames must be positive")
    if play_delay_frames is not None and play_delay_frames < 0:
        raise ValueError("audio.aec.play_delay_frames cannot be negative")
    if method != "webrtc":
        raise ValueError("audio.aec.method must be 'webrtc'")
    if rate != 48_000:
        raise ValueError("audio.aec.rate must remain 48000 on this fixed-rate graph")
    if channels != 1:
        raise ValueError("audio.aec.channels must be 1 on the Pi 3 optimized graph")
    if failure_policy != "fail_closed":
        raise ValueError("audio.aec.failure_policy must be 'fail_closed'")

    phone = (
        roles.phone_address
        if roles is not None
        else str(((data.get("devices") or {}).get("phone") or {}).get("address", ""))
    )
    if not phone or phone == "AA:BB:CC:DD:EE:FF":
        phone = DEFAULT_PHONE_MAC

    bridge_data = data.get("bridge") or {}
    mode = str(bridge_data.get("mode", "bluetooth-wired"))
    fallback_to_wired = toml_bool(bridge_data, "fallback_to_wired", True)

    # [devices.output] accepts either an explicit output id ("wired:<node>" / "a2dp:<MAC>")
    # or the plain `address` the example file has always documented. The example's
    # placeholder is treated as unset so a copied-but-unedited config does not point the
    # bridge at a MAC that does not exist.
    output_data = (data.get("devices") or {}).get("output") or {}
    desired_output = str(output_data.get("id", "") or "").strip() or None
    if desired_output is None:
        address = str(output_data.get("address", "") or "").strip().upper()
        if address and address != "11:22:33:44:55:66":
            desired_output = f"a2dp:{address}"
    speaker_adapter = (
        roles.output.address
        if roles is not None and roles.output is not None
        else str(output_data.get("adapter", "") or "").strip() or None
    )

    phone_override = os.environ.get("BRIDGE_PHONE_MAC")
    if roles is not None and phone_override and phone_override.upper() != roles.phone_address:
        raise ValueError("BRIDGE_PHONE_MAC cannot override the configured call role")
    speaker_override = os.environ.get("BRIDGE_SPEAKER_ADAPTER")
    if roles is not None and speaker_override:
        if roles.output is None:
            raise ValueError("BRIDGE_SPEAKER_ADAPTER requires a configured output role")
        if speaker_override != roles.output.address:
            raise ValueError("BRIDGE_SPEAKER_ADAPTER cannot override the configured output role")

    selected_mode = os.environ.get("BRIDGE_MODE", mode)
    selected_output = os.environ.get("BRIDGE_OUTPUT", desired_output) or None
    if (
        roles is not None
        and roles.output is None
        and (selected_mode == "bluetooth" or str(selected_output or "").lower().startswith("a2dp:"))
    ):
        raise ValueError("Bluetooth/A2DP output requires a configured output role")

    status_value = os.environ.get("BRIDGE_STATUS")
    return Settings(
        aec=AecSettings(
            enabled=enabled,
            method=method,
            rate=rate,
            channels=channels,
            failure_policy=failure_policy,
            high_pass_filter=high_pass_filter,
            noise_suppression=noise_suppression,
            gain_control=gain_control,
            voice_detection=voice_detection,
            transient_suppression=transient_suppression,
            node_latency_frames=node_latency_frames,
            play_delay_frames=play_delay_frames,
        ),
        lark_node=(
            configured_lark.node_name
            if configured_lark is not None and configured_lark.node_name
            else os.environ.get("BRIDGE_LARK", DEFAULT_LARK)
        ),
        lark_component=(
            configured_lark.alsa_component
            if configured_lark is not None and configured_lark.alsa_component
            else os.environ.get("BRIDGE_LARK_COMPONENT", DEFAULT_LARK_COMPONENT)
        ),
        wired_output=os.environ.get("BRIDGE_WIRED_OUT", DEFAULT_WIRED_OUT),
        phone_mac=(phone_override or phone).upper(),
        status_path=Path(status_value) if status_value else default_status_path(),
        config_path=config_path,
        mode=selected_mode,
        desired_output=selected_output,
        speaker_adapter=speaker_override or speaker_adapter,
        fallback_to_wired=fallback_to_wired,
        controller_roles=roles,
        wired_output_volume=wired_output_volume,
        microphone_candidates=microphone_candidates,
        mic_gain_db=mic_gain_db,
        mic_muted=mic_muted,
    )


def inspect_controller_roles(
    settings: Settings,
    *,
    policy: Any = None,
    objects: dict[str, dict] | None = None,
    inventory: list[Any] | None = None,
) -> tuple[dict[str, Any], dict[str, dict], list[Any]]:
    """Resolve the configured roles from one BlueZ/sysfs snapshot."""
    import btadapters
    import controller_roles

    selected_policy = policy or controller_roles.ReadinessPolicy.TRANSITIONAL
    if settings.controller_roles is None:
        return (
            {
                "policy": selected_policy.value,
                "transitional_uart_call": False,
                "ready": False,
                "error": "controller role configuration is absent",
                "call": {"required": True, "configured": False, "ready": False},
                "output": {
                    "required": False,
                    "configured": False,
                    "ready": True,
                    "reason": "wired-output",
                },
            },
            objects or {},
            inventory or [],
        )
    tree = objects if objects is not None else btadapters.managed_objects()
    observed = inventory if inventory is not None else btadapters.adapters(tree)
    return (
        controller_roles.controllers_status(
            settings.controller_roles, observed, policy=selected_policy
        ),
        tree,
        observed,
    )


def call_role_acceptance(
    settings: Settings,
    objects: dict[str, dict],
    inventory: list[Any],
) -> tuple[bool, str | None]:
    """Accept HFP only when the Pixel is connected exclusively through BT500.

    A stale or unpaired Device1 object on another adapter is harmless.  A second
    *connected* object is not: PipeWire's phone-derived node names do not identify
    which controller owns them, so ambiguity must fail closed.
    """
    import btadapters
    import controller_roles

    if settings.controller_roles is None:
        return False, "controller role configuration is absent"
    try:
        call = controller_roles.resolve_controller(
            settings.controller_roles.call,
            inventory,
            policy=controller_roles.ReadinessPolicy.TRANSITIONAL,
        )
    except controller_roles.ControllerRoleError as exc:
        return False, f"{exc.code}: {exc.detail}"

    connected = [
        adapter
        for adapter in inventory
        if btadapters.connected_on(adapter, settings.controller_roles.phone_address, objects)
    ]
    if not any(adapter.address == call.address for adapter in connected):
        return False, "Pixel is not connected on the call controller"
    other = sorted({adapter.address for adapter in connected if adapter.address != call.address})
    if other:
        return False, "Pixel is also connected on another controller: " + ", ".join(other)
    return True, None


def accepted_call_nodes(
    nodes: NodeMap,
    settings: Settings,
    accepted: bool,
) -> NodeMap:
    """Hide untrusted phone endpoints from the graph without altering diagnostics."""
    filtered = dict(nodes)
    if not accepted:
        filtered.pop(settings.hfp_source, None)
        filtered.pop(settings.hfp_sink, None)
    return filtered


NodeMap = dict[str, dict[str, Any]]
LinkList = list[tuple[str, str]]


def pw_snapshot() -> tuple[NodeMap, list[dict[str, Any]]] | None:
    """Read PipeWire once and retain both graph nodes and Device join evidence."""
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode != 0:
            return None
        objects = json.loads(result.stdout)
        if not isinstance(objects, list):
            return None
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("pw-dump failed: %s", exc)
        return None

    nodes: NodeMap = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name")
        if name:
            # The numeric global id is needed for verified wpctl controls but is not present
            # in info.props. Copy rather than mutating the pw-dump object consumed by the
            # identity resolver.
            node_props = dict(props)
            node_props["object.id"] = obj.get("id")
            nodes[str(name)] = node_props
    return nodes, objects


def pw_nodes() -> NodeMap | None:
    """Compatibility wrapper for callers that only need Node properties."""
    snapshot = pw_snapshot()
    return snapshot[0] if snapshot is not None else None


def _pw_object_props(obj: dict[str, Any]) -> dict[str, Any]:
    props = (obj.get("info") or {}).get("props") or {}
    return props if isinstance(props, dict) else {}


def _read_sysfs_value(path: Path, name: str) -> str | None:
    try:
        value = (path / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def microphone_sysfs_by_device(
    objects: list[dict[str, Any]],
    *,
    sysfs_root: Path = Path("/sys"),
) -> dict[str, dict[str, Any]]:
    """Join PipeWire Device globals to their nearest USB parent in sysfs."""
    found: dict[str, dict[str, Any]] = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Device" or obj.get("id") is None:
            continue
        props = _pw_object_props(obj)
        device_id = str(obj["id"])
        facts: dict[str, Any] = {
            # PipeWire's device.* strings are profile/session identifiers, not proof of
            # hardware identity. In particular device.serial is synthesized for USB audio
            # devices that expose no USB serial at all. Only the nearest USB sysfs parent
            # may populate these fields.
            "usb_vendor_id": None,
            "usb_product_id": None,
            "usb_product": None,
            "usb_serial": None,
            "usb_port_path": None,
            "usb_instance_generation": None,
        }
        raw_path = next(
            (
                str(props[key])
                for key in ("device.sysfs.path", "sysfs.path", "api.alsa.path")
                if props.get(key)
            ),
            "",
        )
        if raw_path:
            normalized = raw_path.replace("\\", "/")
            if normalized.startswith("/sys/"):
                candidate = sysfs_root / normalized.removeprefix("/sys/")
            elif normalized.startswith("/devices/"):
                candidate = sysfs_root / normalized.removeprefix("/")
            else:
                candidate = Path(raw_path)
            boundary = sysfs_root.resolve()
            try:
                candidate = candidate.resolve()
            except OSError:
                pass
            while candidate != candidate.parent:
                vendor = _read_sysfs_value(candidate, "idVendor")
                product_id = _read_sysfs_value(candidate, "idProduct")
                if vendor and product_id:
                    port = candidate.name
                    devnum = _read_sysfs_value(candidate, "devnum")
                    facts.update(
                        {
                            "usb_vendor_id": vendor,
                            "usb_product_id": product_id,
                            "usb_product": _read_sysfs_value(candidate, "product"),
                            "usb_serial": _read_sysfs_value(candidate, "serial"),
                            "usb_port_path": port,
                            "usb_instance_generation": (
                                f"{port}@{devnum}" if devnum else str(candidate)
                            ),
                        }
                    )
                    break
                if candidate == boundary:
                    break
                candidate = candidate.parent
        found[device_id] = facts
    return found


def parse_enum_format_output(text: str) -> tuple[dict[str, Any], ...]:
    """Parse the audio fields from ``pw-cli enum-params ... EnumFormat`` output."""
    values: dict[str, set[Any]] = {"format": set(), "rate": set(), "channels": set()}
    active: str | None = None
    for line in text.splitlines():
        if "Prop:" in line:
            if "Audio:format" in line:
                active = "format"
            elif "Audio:rate" in line:
                active = "rate"
            elif "Audio:channels" in line:
                active = "channels"
            else:
                active = None
            continue
        if active == "format":
            for matched in re.findall(r"AudioFormat:([A-Za-z0-9_]+)", line):
                values["format"].add(matched)
        elif active in {"rate", "channels"}:
            for matched in re.findall(r"\bInt\s+([0-9]+)\b", line):
                values[active].add(int(matched))
    if not all(values.values()):
        return ()
    return tuple(
        {"rate": rate, "format": audio_format, "channels": channels}
        for audio_format in sorted(values["format"])
        for rate in sorted(values["rate"])
        for channels in sorted(values["channels"])
    )


def _pw_choice_values(value: Any) -> tuple[Any, ...]:
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = (value,)
    return tuple(dict.fromkeys(item for item in values if item is not None))


def formats_from_pw_dump_object(obj: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Use structured EnumFormat params when pw-dump already supplied them."""
    params = (obj.get("info") or {}).get("params") or {}
    entries = params.get("EnumFormat") or [] if isinstance(params, dict) else []
    found: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("mediaType") not in {None, "audio"}:
            continue
        if entry.get("mediaSubtype") not in {None, "raw"}:
            continue
        formats = _pw_choice_values(entry.get("format"))
        rates = _pw_choice_values(entry.get("rate"))
        channels = _pw_choice_values(entry.get("channels"))
        for audio_format in formats:
            for rate in rates:
                for channel_count in channels:
                    try:
                        item = {
                            "rate": int(rate),
                            "format": str(audio_format),
                            "channels": int(channel_count),
                        }
                    except (TypeError, ValueError):
                        continue
                    if item not in found:
                        found.append(item)
    return tuple(found)


def microphone_capabilities_by_node(
    objects: list[dict[str, Any]],
    *,
    cache: dict[tuple[str, str], tuple[dict[str, Any], ...]] | None = None,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Read EnumFormat once per PipeWire node/device generation."""
    remembered = cache if cache is not None else {}
    device_serials = {
        str(obj.get("id")): str(_pw_object_props(obj).get("object.serial") or obj.get("id"))
        for obj in objects
        if obj.get("type") == "PipeWire:Interface:Device" and obj.get("id") is not None
    }
    found: dict[str, tuple[dict[str, Any], ...]] = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node" or obj.get("id") is None:
            continue
        props = _pw_object_props(obj)
        if props.get("media.class") != "Audio/Source" or not props.get("node.name"):
            continue
        node = str(props["node.name"])
        node_serial = str(props.get("object.serial") or obj["id"])
        device_id = str(props.get("device.id") or "")
        key = (node_serial, device_serials.get(device_id, device_id))
        formats = remembered.get(key)
        if formats is None:
            formats = formats_from_pw_dump_object(obj)
            if not formats:
                try:
                    result = subprocess.run(
                        ["pw-cli", "enum-params", str(obj["id"]), "EnumFormat"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    result = None
                formats = (
                    parse_enum_format_output(result.stdout)
                    if result is not None and result.returncode == 0
                    else ()
                )
            # Some recorded/test snapshots carry one already-negotiated format directly.
            # It is admissible evidence only when all fields are present.
            if not formats and all(
                props.get(key) is not None
                for key in ("audio.rate", "audio.format", "audio.channels")
            ):
                try:
                    formats = (
                        {
                            "rate": int(props["audio.rate"]),
                            "format": str(props["audio.format"]),
                            "channels": int(props["audio.channels"]),
                        },
                    )
                except (TypeError, ValueError):
                    formats = ()
            # Do not make a transient pw-cli/parser failure permanent for this device
            # generation. Successful evidence is stable and worth caching; absence retries.
            if formats:
                remembered[key] = formats
        found[node] = formats
    return found


def discover_microphones(
    objects: list[dict[str, Any]],
    settings: Settings,
    *,
    capability_cache: dict[tuple[str, str], tuple[dict[str, Any], ...]] | None = None,
    sysfs_root: Path = Path("/sys"),
) -> tuple[tuple[Any, ...], Any]:
    """Return observations and one deterministic selection from one pw-dump snapshot."""
    import microphones as microphones_module

    observations = microphones_module.observations_from_pw_dump(
        objects,
        sysfs_by_device=microphone_sysfs_by_device(objects, sysfs_root=sysfs_root),
        capabilities_by_node=microphone_capabilities_by_node(
            objects,
            cache=capability_cache,
        ),
    )
    return observations, microphones_module.resolve(
        settings.microphone_candidates,
        observations,
    )


def resolve_microphone(
    objects: list[dict[str, Any]],
    settings: Settings,
    *,
    capability_cache: dict[tuple[str, str], tuple[dict[str, Any], ...]] | None = None,
    sysfs_root: Path = Path("/sys"),
) -> Any:
    """Generic read-only resolver entry point for the supervisor and rig tools."""
    return discover_microphones(
        objects,
        settings,
        capability_cache=capability_cache,
        sysfs_root=sysfs_root,
    )[1]


class PcmActivityDebouncer:
    """Classify native S16 PCM without mistaking brief silence for a lost transmitter."""

    def __init__(
        self,
        window_bytes: int,
        *,
        active_windows: int = LARK_PCM_ACTIVE_WINDOWS,
        inactive_windows: int = LARK_PCM_INACTIVE_WINDOWS,
    ) -> None:
        if window_bytes <= 0 or active_windows <= 0 or inactive_windows <= 0:
            raise ValueError("PCM liveness window sizes must be positive")
        self.window_bytes = window_bytes
        self.active_windows = active_windows
        self.inactive_windows = inactive_windows
        self.state = "unknown"
        self.revision = 0
        self._buffer = bytearray()
        self._nonzero_windows = 0
        self._zero_windows = 0

    def feed(self, data: bytes) -> bool:
        """Consume complete windows; an empty read is no evidence either way."""
        if not data:
            return False
        before = self.revision
        self._buffer.extend(data)
        while len(self._buffer) >= self.window_bytes:
            window = bytes(self._buffer[: self.window_bytes])
            del self._buffer[: self.window_bytes]
            if any(window):
                self._zero_windows = 0
                self._nonzero_windows = min(
                    self._nonzero_windows + 1,
                    self.active_windows,
                )
                if self._nonzero_windows >= self.active_windows and self.state != "active":
                    self.state = "active"
                    self.revision += 1
            else:
                self._nonzero_windows = 0
                self._zero_windows = min(
                    self._zero_windows + 1,
                    self.inactive_windows,
                )
                if self._zero_windows >= self.inactive_windows and self.state != "inactive":
                    self.state = "inactive"
                    self.revision += 1
        return self.revision != before


def automatic_lark_liveness_enabled(candidates: tuple[Any, ...]) -> bool:
    """Limit the PCM heuristic to the explicit Lark-first/FIFINE-fallback deployment."""
    lark = next((item for item in candidates if getattr(item, "id", None) == "lark-a1"), None)
    fifine = next(
        (item for item in candidates if getattr(item, "id", None) == "fifine-k054"),
        None,
    )
    return bool(lark is not None and not getattr(lark, "legacy", False) and fifine is not None)


class LarkPcmLivenessMonitor:
    """Own one exact-target recorder and publish token-bound Lark eligibility."""

    NO_DATA_SECONDS = 1.0
    LINK_GRACE_SECONDS = 0.5

    def __init__(self, *, popen_factory: Any = subprocess.Popen, clock: Any = time.monotonic):
        self._popen_factory = popen_factory
        self._clock = clock
        self.proc: subprocess.Popen[bytes] | None = None
        self.enabled = False
        self.node: str | None = None
        self.instance_token: str | None = None
        self.state = "disabled"
        self.reason = "Lark transmitter detection is disabled"
        self.revision = 0
        self._detector: PcmActivityDebouncer | None = None
        self._started_at = 0.0
        self._last_data_at = 0.0
        self._next_retry = 0.0
        self._link_verified = False
        self._link_check_requested = False
        self._rate = 48_000
        self._channels = 2

    def _set_state(self, state: str, reason: str) -> bool:
        changed = state != self.state or reason != self.reason
        self.state = state
        self.reason = reason
        if changed:
            self.revision += 1
        return changed

    def _stop_process(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def _deactivate(self, state: str, reason: str) -> None:
        self._stop_process()
        self.node = None
        self.instance_token = None
        self._detector = None
        self._next_retry = 0.0
        self._link_verified = False
        self._link_check_requested = False
        self._set_state(state, reason)

    def _mark_error(self, reason: str) -> bool:
        self._stop_process()
        self._next_retry = self._clock() + LARK_PCM_RETRY_SECONDS
        self._link_verified = False
        self._link_check_requested = False
        return self._set_state("error", reason)

    def _start(self) -> None:
        if self.node is None or self.instance_token is None:
            return
        if self._channels == 1:
            channel_map = "mono"
        elif self._channels == 2:
            channel_map = "stereo"
        else:
            self._mark_error(f"unsupported Lark channel count for PCM monitoring: {self._channels}")
            return
        command = [
            "pw-record",
            "--target",
            self.node,
            "--properties",
            (
                f"node.name={LARK_PCM_MONITOR_NODE} "
                "node.dont-reconnect=true stream.dont-remix=true"
            ),
            "--rate",
            str(self._rate),
            "--channels",
            str(self._channels),
            "--channel-map",
            channel_map,
            "--format",
            "s16",
            "--raw",
            "-",
        ]
        try:
            proc = self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self.proc = proc
            if proc.stdout is None:
                raise OSError("pw-record did not provide a PCM stream")
            os.set_blocking(proc.stdout.fileno(), False)
        except (OSError, subprocess.SubprocessError) as exc:
            self._stop_process()
            self._next_retry = self._clock() + LARK_PCM_RETRY_SECONDS
            self._set_state("error", f"Lark transmitter monitor could not start: {exc}")
            return
        window_bytes = int(self._rate * self._channels * 2 * LARK_PCM_WINDOW_SECONDS)
        self._detector = PcmActivityDebouncer(window_bytes)
        self._started_at = self._clock()
        self._last_data_at = self._started_at
        self._next_retry = 0.0
        self._link_verified = False
        self._link_check_requested = False
        self._set_state("unknown", "checking the Lark receiver for transmitter audio")
        log.info("monitoring Lark transmitter audio on %s", self.node)

    def feed_pcm(self, data: bytes) -> bool:
        """Testable read seam used by poll(); every nonzero S16 bit counts as activity."""
        if self._detector is None or not data:
            return False
        self._last_data_at = self._clock()
        if not self._detector.feed(data):
            return False
        if self._detector.state == "active":
            return self._set_state("active", "Lark transmitter audio is active")
        return self._set_state(
            "inactive",
            "Lark receiver is present, but no transmitter audio is detected",
        )

    def poll(self) -> bool:
        """Drain available PCM without blocking; return true only for a state change."""
        before = self.revision
        proc = self.proc
        if proc is None:
            return False
        if proc.poll() is not None:
            self._mark_error(f"Lark transmitter monitor exited with status {proc.returncode}")
            return self.revision != before
        assert proc.stdout is not None
        while True:
            try:
                data = os.read(proc.stdout.fileno(), 65_536)
            except BlockingIOError:
                break
            except OSError as exc:
                self._mark_error(f"Lark transmitter monitor read failed: {exc}")
                return self.revision != before
            if not data:
                self._mark_error("Lark transmitter monitor ended without PCM")
                return self.revision != before
            self.feed_pcm(data)
        if self.proc is not None and self._clock() - self._last_data_at > self.NO_DATA_SECONDS:
            self._mark_error("Lark transmitter monitor produced no PCM")
        if (
            self.proc is not None
            and not self._link_verified
            and not self._link_check_requested
            and self._clock() - self._started_at >= self.LINK_GRACE_SECONDS
        ):
            # Force one fresh pw-link snapshot before active PCM can affect selection.
            self._link_check_requested = True
            return True
        return self.revision != before

    def reconcile(self, physical_resolution: Any, links: LinkList, *, enabled: bool) -> Any:
        """Bind monitoring to the physical Lark generation, independent of final selection."""
        import microphones as microphones_module

        self.enabled = enabled
        if not enabled:
            self._deactivate("disabled", "Lark transmitter detection is disabled")
            return None
        selection = getattr(physical_resolution, "selected", None)
        candidate = getattr(selection, "candidate", None)
        if selection is None or getattr(candidate, "id", None) != "lark-a1":
            self._deactivate("absent", "no uniquely usable Lark receiver is present")
            return None

        source = selection.source
        selected_format = source.matching_format(candidate)
        token = selection.instance_token
        node = selection.node
        if selected_format is None or selected_format.format != "S16LE":
            if token != self.instance_token or node != self.node:
                self._stop_process()
                self.node = node
                self.instance_token = token
            self._mark_error("Lark transmitter detection requires native S16LE PCM")
        else:
            target_changed = token != self.instance_token or node != self.node
            if target_changed:
                self._stop_process()
                self.node = node
                self.instance_token = token
                self._rate = selected_format.rate
                self._channels = selected_format.channels
                self._start()
            else:
                self.poll()
                now = self._clock()
                if self.proc is None and now >= self._next_retry:
                    self._start()

        if self.proc is not None and self._clock() - self._started_at >= self.LINK_GRACE_SECONDS:
            linked_sources = {
                source_name
                for source_name, target_name in links
                if target_name == LARK_PCM_MONITOR_NODE
            }
            if linked_sources != {self.node}:
                self._mark_error(
                    "Lark transmitter monitor is not exclusively linked to the selected receiver"
                )
            else:
                self._link_verified = True
                self._link_check_requested = False

        assert self.instance_token is not None
        availability_state = self.state
        availability_reason = self.reason
        if availability_state == "active" and not self._link_verified:
            availability_state = "unknown"
            availability_reason = "verifying the Lark transmitter monitor target"
        return microphones_module.DynamicAvailability(
            candidate_id="lark-a1",
            instance_token=self.instance_token,
            state=availability_state,
            reason=availability_reason,
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "detector": "pcm_exact_zero",
            "state": self.state,
            "active": (
                True if self.state == "active" else False if self.state == "inactive" else None
            ),
            "reason": self.reason,
            "node": self.node,
            "instance_token": self.instance_token,
            "link_verified": self._link_verified,
            "owner_pid": self.proc.pid if self.proc is not None else None,
            "active_after_ms": round(LARK_PCM_WINDOW_SECONDS * LARK_PCM_ACTIVE_WINDOWS * 1000),
            "inactive_after_ms": round(LARK_PCM_WINDOW_SECONDS * LARK_PCM_INACTIVE_WINDOWS * 1000),
        }

    def close(self) -> None:
        self.enabled = False
        self._deactivate("disabled", "supervisor is stopping")


def pw_links() -> LinkList | None:
    try:
        result = subprocess.run(
            ["pw-link", "-l"], capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode != 0:
            return None
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("pw-link failed: %s", exc)
        return None

    links: LinkList = []
    current: str | None = None
    for raw in result.stdout.splitlines():
        if not raw.startswith((" ", "\t")):
            current = raw.strip().split(":")[0]
            continue
        value = raw.strip()
        if current is None or not value:
            continue
        if value.startswith("|->"):
            links.append((current, value[3:].strip().split(":")[0]))
        elif value.startswith("|<-"):
            links.append((value[3:].strip().split(":")[0], current))
    return links


def find_lark(nodes: NodeMap, settings: Settings) -> str | None:
    if settings.lark_node in nodes:
        return settings.lark_node
    matches = [
        name
        for name, props in nodes.items()
        if props.get("media.class") == "Audio/Source"
        and str(props.get("alsa.components", "")).upper() == settings.lark_component.upper()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.error("Lark identity %s is ambiguous: %s", settings.lark_component, matches)
    return None


def link(source: str, target: str) -> bool:
    try:
        result = subprocess.run(
            ["pw-link", source, target],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("failed to create link %s -> %s: %s", source, target, exc)
        return False


def unlink(source: str, target: str) -> bool:
    try:
        result = subprocess.run(
            ["pw-link", "-d", source, target],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("failed to remove link %s -> %s: %s", source, target, exc)
        return False


def set_aec_mute(muted: bool) -> bool:
    try:
        result = subprocess.run(
            ["pactl", "set-sink-mute", AEC_SINK, "1" if muted else "0"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_sink_volume(node: str) -> float | None:
    """Return a sink's common channel volume as a 0..1 value."""
    try:
        result = subprocess.run(
            ["pactl", "get-sink-volume", node],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    channels = [float(value) / 100.0 for value in VOLUME_PERCENT_RE.findall(result.stdout)]
    if not channels or max(channels) - min(channels) > 0.01:
        return None
    return sum(channels) / len(channels)


def set_and_verify_sink_volume(
    node: str, desired: float, *, tolerance: float = 0.01
) -> tuple[bool, float | None, str | None]:
    """Set one named sink and verify its observed mixer state before routing audio."""
    try:
        result = subprocess.run(
            ["pactl", "set-sink-volume", node, f"{desired * 100:.2f}%"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, f"volume set failed: {exc}"
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return False, None, f"volume set failed: {detail}"
    observed = read_sink_volume(node)
    if observed is None:
        return False, None, "volume verification returned no common channel value"
    if abs(observed - desired) > tolerance:
        return (
            False,
            observed,
            f"volume mismatch: desired {desired:.3f}, observed {observed:.3f}",
        )
    return True, observed, None


def set_and_verify_microphone_control(
    nodes: NodeMap,
    node: str,
    gain_db: float,
    muted: bool,
    *,
    tolerance: float = 0.01,
) -> tuple[bool, float | None, bool | None, str | None]:
    """Apply and read back software gain/mute on ``output.bridge.mic``.

    wpctl addresses graph objects by global id. The id comes from the same pw-dump snapshot
    used for endpoint resolution, avoiding a second graph read and the replug race that would
    create. Failures make a best-effort mute before returning to the caller's SAFE path.
    """
    props = nodes.get(node) or {}
    object_id = props.get("object.id")
    if object_id is None:
        return False, None, None, f"microphone control node {node} has no PipeWire object id"
    target = str(object_id)
    linear = 10 ** (gain_db / 20.0)
    limit = max(1.0, linear)

    def run(command: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    # New bridge.mic loopbacks are born muted. Keep that containment while changing gain,
    # and make unmute the final mutating step.
    commands = [
        ["wpctl", "set-mute", target, "1"],
        ["wpctl", "set-volume", target, f"{linear:.6f}", "--limit", f"{limit:.6f}"],
        ["wpctl", "set-mute", target, "1" if muted else "0"],
    ]
    for command in commands:
        result = run(command)
        if result is None or result.returncode != 0:
            run(["wpctl", "set-mute", target, "1"])
            detail = (
                "command could not run"
                if result is None
                else (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            )
            return False, None, None, f"microphone control failed: {detail}"

    observed = run(["wpctl", "get-volume", target])
    match = WPCTL_VOLUME_RE.search(observed.stdout) if observed is not None else None
    if observed is None or observed.returncode != 0 or match is None:
        run(["wpctl", "set-mute", target, "1"])
        detail = (
            "command could not run"
            if observed is None
            else (observed.stderr or observed.stdout).strip() or f"exit {observed.returncode}"
        )
        return False, None, None, f"microphone control verification failed: {detail}"
    observed_linear = float(match.group(1))
    observed_muted = "[MUTED]" in observed.stdout
    observed_db = 20.0 * math.log10(observed_linear) if observed_linear > 0 else -math.inf
    if abs(observed_linear - linear) > tolerance or observed_muted != muted:
        run(["wpctl", "set-mute", target, "1"])
        return (
            False,
            observed_db,
            observed_muted,
            (
                "microphone control mismatch: "
                f"desired {gain_db:.2f} dB/muted={muted}, "
                f"observed {observed_db:.2f} dB/muted={observed_muted}"
            ),
        )
    return True, observed_db, observed_muted, None


class Loopback:
    """A child pw-loopback with explicit, continuously verified targets."""

    def __init__(self, name: str, capture: str, playback: str, channels: int):
        self.name = name
        self.capture = capture
        self.playback = playback
        self.channels = channels
        self.proc: subprocess.Popen[str] | None = None
        self.defer_playback = False

    @property
    def out_node(self) -> str:
        return f"output.{self.name}"

    @property
    def in_node(self) -> str:
        return f"input.{self.name}"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        command = [
            "pw-loopback",
            "--name",
            self.name,
            "--capture",
            self.capture,
            "--channels",
            str(self.channels),
            "--capture-props",
            "{ node.dont-reconnect = true node.passive = true }",
            "--playback-props",
            (
                "{ node.dont-reconnect = true node.passive = true " "node.autoconnect = false }"
                if self.defer_playback
                else "{ node.dont-reconnect = true node.passive = true }"
            ),
        ]
        if not self.defer_playback:
            command[command.index("--channels") : command.index("--channels")] = [
                "--playback",
                self.playback,
            ]
        log.info("starting %s: %s -> %s", self.name, self.capture, self.playback)
        self.proc = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True
        )

    def targets_verified(self, links: LinkList) -> bool:
        outputs = {target for source, target in links if source == self.out_node}
        inputs = {source for source, target in links if target == self.in_node}
        return outputs == {self.playback} and inputs == {self.capture}

    def stop(self, reason: str) -> None:
        if self.proc is None:
            return
        log.info("stopping %s (%s)", self.name, reason)
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


def spa_string(value: str) -> str:
    return json.dumps(value)


class NativeAecHost:
    """Hold a native PipeWire WebRTC module inside a child pw-cli context."""

    def __init__(
        self,
        settings: AecSettings,
        microphone: str,
        output: str,
        *,
        latency_frames: int | None = None,
        play_delay_frames: int | None = None,
    ):
        if latency_frames is not None and latency_frames <= 0:
            raise ValueError("latency_frames must be positive")
        if play_delay_frames is not None and play_delay_frames < 0:
            raise ValueError("play_delay_frames cannot be negative")
        self.settings = settings
        self.microphone = microphone
        self.output = output
        self.latency_frames = latency_frames
        self.play_delay_frames = play_delay_frames
        self.proc: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.running and self.proc is not None else None

    def module_command(self) -> str:
        module_args = [
            "{",
            "library.name = aec/libspa-aec-webrtc",
            f"audio.rate = {self.settings.rate}",
            f"audio.channels = {self.settings.channels}",
            "audio.position = [ MONO ]",
            "aec.args = {",
            f"webrtc.noise_suppression = {str(self.settings.noise_suppression).lower()}",
            f"webrtc.gain_control = {str(self.settings.gain_control).lower()}",
            f"webrtc.voice_detection = {str(self.settings.voice_detection).lower()}",
            f"webrtc.high_pass_filter = {str(self.settings.high_pass_filter).lower()}",
            f"webrtc.transient_suppression = {str(self.settings.transient_suppression).lower()}",
            "}",
            "capture.props = {",
            f"target.object = {spa_string(self.microphone)}",
            "node.dont-reconnect = true",
            "node.passive = true",
            "}",
            "source.props = {",
            f"node.name = {spa_string(AEC_SOURCE)}",
            "media.class = Audio/Source",
            "}",
            "sink.props = {",
            f"node.name = {spa_string(AEC_SINK)}",
            "media.class = Audio/Sink",
            "}",
            "playback.props = {",
            f"target.object = {spa_string(self.output)}",
            "node.dont-reconnect = true",
            "node.passive = true",
            "}",
            "}",
        ]
        if self.latency_frames is not None:
            module_args.insert(5, f"node.latency = {self.latency_frames}/{self.settings.rate}")
        if self.play_delay_frames is not None:
            module_args.insert(
                5,
                f"buffer.play_delay = {self.play_delay_frames}/{self.settings.rate}",
            )
        arguments = " ".join(module_args)
        return f"load-module libpipewire-module-echo-cancel {arguments}\n"

    def start(self) -> None:
        if self.running:
            return
        log.info("starting native WebRTC AEC: %s -> %s", self.microphone, self.output)
        self.proc = subprocess.Popen(
            ["pw-cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert self.proc.stdin is not None
        self.proc.stdin.write(self.module_command())
        self.proc.stdin.flush()

    def stop(self, reason: str) -> None:
        if self.proc is None:
            return
        log.info("stopping native AEC host (%s)", reason)
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.running:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
        self.proc = None


def unexpected_call_links(
    links: LinkList,
    *,
    lark: str,
    hfp_source: str,
    hfp_sink: str,
    microphone_input: str,
    callout_input: str,
    aec_enabled: bool,
    microphones: tuple[str, ...] = (),
    selected_microphone: str | None = None,
    microphone_output: str | None = None,
) -> LinkList:
    physical = set(microphones) or {lark}
    selected = selected_microphone or lark
    unexpected: LinkList = []
    for source, target in links:
        if (
            source in physical
            and target == hfp_sink
            or source == hfp_source
            and target != callout_input
            or aec_enabled
            and source == AEC_SOURCE
            and target != microphone_input
            or microphone_output is not None
            and target == hfp_sink
            and source != microphone_output
            or aec_enabled
            and target == AEC_CAPTURE
            and source != selected
            or not aec_enabled
            and target == microphone_input
            and source != selected
        ):
            unexpected.append((source, target))
    return unexpected


def selected_microphone_node(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    node = getattr(value, "node", None)
    return str(node) if node else None


def selected_microphone_token(value: Any) -> Any:
    if isinstance(value, str):
        return value
    token = getattr(value, "instance_token", None)
    return token if token is not None else selected_microphone_node(value)


def microphone_resolution_report(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str) or value is None:
        return None
    as_dict = getattr(value, "as_dict", None)
    if not callable(as_dict):
        return None
    report = as_dict()
    return report if isinstance(report, dict) else None


def microphone_candidate_nodes(report: dict[str, Any] | None) -> tuple[str, ...]:
    """Extract every identity-matched node from resolver diagnostics.

    The resolver's public JSON uses ``matched_nodes``. Accept the selected node separately
    so a legacy/partial diagnostic can never omit the active microphone from safety sweeps.
    """
    if not report:
        return ()
    found: set[str] = set()
    selected = report.get("selected") or {}
    if isinstance(selected, dict) and selected.get("node"):
        found.add(str(selected["node"]))
    for candidate in report.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        for key in ("matched_nodes", "nodes"):
            values = candidate.get(key) or []
            if isinstance(values, str):
                values = [values]
            for node in values:
                if node:
                    found.add(str(node))
        if candidate.get("node"):
            found.add(str(candidate["node"]))
    return tuple(sorted(found))


def lark_node_from_report(report: dict[str, Any] | None) -> str | None:
    if not report:
        return None
    selected = report.get("selected") or {}
    if isinstance(selected, dict) and selected.get("id") == "lark-a1":
        return str(selected.get("node") or "") or None
    for candidate in report.get("candidates") or []:
        if not isinstance(candidate, dict) or candidate.get("id") != "lark-a1":
            continue
        nodes = candidate.get("matched_nodes") or candidate.get("nodes") or []
        if isinstance(nodes, str):
            nodes = [nodes]
        if len(nodes) == 1:
            return str(nodes[0])
        if candidate.get("node"):
            return str(candidate["node"])
    return None


class CallGraph:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = State.CALL_DOWN
        self.generation = 0
        self.signature: tuple[Any, ...] | None = None
        self.aec_host: NativeAecHost | None = None
        self.microphone: Loopback | None = None
        self.callout: Loopback | None = None
        self.build_started = 0.0
        self.routes_started = 0.0
        self.attempts = 0
        self.next_attempt = 0.0
        self.verified = False
        self.last_failure: str | None = None
        self.unexpected_links: LinkList = []
        # The output node in force for this generation. None until the first tick; callers
        # that predate output selection (the test suite, and any tick() without an explicit
        # node) fall back to the configured wired output via output_target.
        self.output_node: str | None = None
        self.output_volume_target: str | None = None
        self.output_volume_observed: float | None = None
        self.output_volume_verified = False
        self.output_volume_error: str | None = None
        self.selected_microphone: str | None = None
        self.selected_microphone_token: Any = None
        self.microphone_report: dict[str, Any] | None = None
        self.candidate_microphones: tuple[str, ...] = ()
        self.break_before_make = False
        self.mic_control_target: str | None = None
        self.mic_gain_observed_db: float | None = None
        self.mic_mute_observed: bool | None = None
        self.mic_control_verified = False
        self.mic_control_error: str | None = None
        self.mic_control_blocked = False
        self.mic_control_primed = False
        self.mic_link_requested = False

    @property
    def output_target(self) -> str:
        return self.output_node or self.settings.wired_output

    @property
    def microphone_controls_required(self) -> bool:
        return self.settings.mic_gain_db is not None and self.settings.mic_muted is not None

    def teardown(self, reason: str) -> None:
        # Microphone switches are deliberately break-before-make. Stop the only owned HFP
        # uplink before touching the callout or AEC owner so there is never a two-mic overlap.
        if self.microphone is not None:
            self.microphone.stop(reason)
        if self.aec_host is not None:
            set_aec_mute(True)
        if self.callout is not None:
            self.callout.stop(reason)
        if self.aec_host is not None:
            self.aec_host.stop(reason)
        self.callout = None
        self.microphone = None
        self.aec_host = None
        self.build_started = 0.0
        self.routes_started = 0.0
        self.verified = False
        self.unexpected_links = []
        self.output_volume_target = None
        self.output_volume_observed = None
        self.output_volume_verified = False
        self.output_volume_error = None
        self.mic_control_target = None
        self.mic_gain_observed_db = None
        self.mic_mute_observed = None
        self.mic_control_verified = False
        self.mic_control_error = None
        self.mic_control_primed = False
        self.mic_link_requested = False

    def ensure_output_volume(self, target: str) -> bool:
        """Verify the selected wired sink once per graph generation."""
        desired = self.settings.wired_output_volume
        if target != self.settings.wired_output or desired is None:
            self.output_volume_target = target
            self.output_volume_observed = None
            self.output_volume_verified = True
            self.output_volume_error = None
            return True
        if self.output_volume_target == target and self.output_volume_verified:
            return True
        ok, observed, error = set_and_verify_sink_volume(target, desired)
        self.output_volume_target = target
        self.output_volume_observed = observed
        self.output_volume_verified = ok
        self.output_volume_error = error
        return ok

    def ensure_microphone_control(self, nodes: NodeMap) -> bool:
        """Verify post-AEC software controls exactly once per graph generation."""
        gain = self.settings.mic_gain_db
        muted = self.settings.mic_muted
        if gain is None or muted is None:
            self.mic_control_target = "output.bridge.mic"
            self.mic_gain_observed_db = gain
            self.mic_mute_observed = muted
            self.mic_control_verified = True
            self.mic_control_error = None
            return True
        if self.mic_control_verified:
            return True
        ok, observed_gain, observed_muted, error = set_and_verify_microphone_control(
            nodes,
            "output.bridge.mic",
            gain,
            muted,
        )
        self.mic_control_target = "output.bridge.mic"
        self.mic_gain_observed_db = observed_gain
        self.mic_mute_observed = observed_muted
        self.mic_control_verified = ok
        self.mic_control_error = error
        return ok

    def prime_microphone_control(self, nodes: NodeMap) -> bool:
        """Set gain while muted before bridge.mic is linked to the HFP sink."""
        if not self.microphone_controls_required:
            self.mic_control_primed = True
            return True
        if self.mic_control_primed:
            return True
        assert self.settings.mic_gain_db is not None
        ok, observed_gain, observed_muted, error = set_and_verify_microphone_control(
            nodes,
            "output.bridge.mic",
            self.settings.mic_gain_db,
            True,
        )
        self.mic_control_target = "output.bridge.mic"
        self.mic_gain_observed_db = observed_gain
        self.mic_mute_observed = observed_muted
        self.mic_control_error = error
        self.mic_control_primed = ok
        return ok

    def hold_microphone_control_safe(self, reason: str) -> None:
        control_state = (
            self.mic_control_target,
            self.mic_gain_observed_db,
            self.mic_mute_observed,
            self.mic_control_error,
        )
        self.teardown(reason)
        (
            self.mic_control_target,
            self.mic_gain_observed_db,
            self.mic_mute_observed,
            self.mic_control_error,
        ) = control_state
        self.mic_control_verified = False
        self.mic_control_blocked = True
        self.last_failure = reason
        self.state = State.SAFE
        log.error("call graph held SAFE: %s", reason)

    def update_signature(self, signature: tuple[Any, ...]) -> bool:
        if signature == self.signature:
            return False
        had_graph = any(item is not None for item in (self.aec_host, self.microphone, self.callout))
        self.generation += 1
        self.teardown("endpoint generation changed")
        self.signature = signature
        self.attempts = 0
        self.next_attempt = 0.0
        self.last_failure = None
        self.break_before_make = had_graph
        self.mic_control_blocked = False
        return had_graph

    def fail(self, reason: str) -> None:
        self.last_failure = reason
        self.attempts += 1
        log.error("call graph generation %d failed: %s", self.generation, reason)
        self.teardown(reason)
        if self.attempts >= MAX_BUILD_ATTEMPTS:
            self.state = State.FAILED
            self.next_attempt = time.monotonic() + FAILED_RETRY_SECONDS
        else:
            self.state = State.DEGRADED
            self.next_attempt = time.monotonic() + min(2**self.attempts, 30)

    def begin_build(self, lark: str) -> None:
        self.state = State.BUILDING
        self.build_started = time.monotonic()
        if self.settings.aec.enabled:
            self.aec_host = NativeAecHost(
                self.settings.aec,
                lark,
                self.output_target,
                latency_frames=self.settings.aec.node_latency_frames,
                play_delay_frames=self.settings.aec.play_delay_frames,
            )
            self.aec_host.start()
            return
        self.callout = Loopback("bridge.callout", self.settings.hfp_source, self.output_target, 2)
        self.microphone = Loopback("bridge.mic", lark, self.settings.hfp_sink, 1)
        self.microphone.defer_playback = self.microphone_controls_required
        self.callout.start()
        self.microphone.start()
        self.routes_started = time.monotonic()

    def can_switch_output_live(self) -> bool:
        aec_host = self.aec_host
        return bool(
            self.state == State.ACTIVE
            and self.verified
            and self.microphone is not None
            and self.microphone.running
            and self.callout is not None
            and self.callout.running
            and (not self.settings.aec.enabled or aec_host is not None)
            and (not self.settings.aec.enabled or (aec_host is not None and aec_host.running))
        )

    def switch_output_live(self, target: str) -> bool:
        """Make-before-break retargeting that leaves the microphone and AEC alive."""
        old = self.output_node
        if old is None or old == target or self.callout is None:
            return False
        playback_source = AEC_PLAYBACK if self.settings.aec.enabled else self.callout.out_node
        log.info("switching output live: %s -> %s", old, target)
        if not link(playback_source, target):
            return False
        if not unlink(playback_source, old):
            # The old route still carries audio, so roll the new route back rather than
            # leaving an unannounced two-speaker fan-out. A failed rollback is contained by
            # fail(), which tears down the owner and therefore both links.
            unlink(playback_source, target)
            return False
        self.output_node = target
        self.callout.playback = AEC_SINK if self.settings.aec.enabled else target
        if self.aec_host is not None:
            self.aec_host.output = target
        self.generation += 1
        self.state = State.SWITCHING
        self.last_failure = None
        return True

    def start_aec_routes(self) -> None:
        set_aec_mute(True)
        self.callout = Loopback("bridge.callout", self.settings.hfp_source, AEC_SINK, 1)
        self.microphone = Loopback("bridge.mic", AEC_SOURCE, self.settings.hfp_sink, 1)
        self.microphone.defer_playback = self.microphone_controls_required
        self.callout.start()
        self.microphone.start()
        self.routes_started = time.monotonic()

    def remove_dangerous_autolinks(
        self,
        links: LinkList,
        microphones: tuple[str, ...],
        selected: str | None,
    ) -> bool:
        changed = False
        physical = set(microphones)
        if selected:
            physical.add(selected)
        owned_uplink = (
            self.microphone.out_node
            if self.microphone is not None
            and (not self.microphone_controls_required or self.mic_control_primed)
            else None
        )
        for source, target in links:
            dangerous = source in physical and target == self.settings.hfp_sink
            dangerous = dangerous or (target == self.settings.hfp_sink and source != owned_uplink)
            dangerous = dangerous or (
                target == AEC_CAPTURE and source in physical and source != selected
            )
            if self.microphone is not None:
                dangerous = dangerous or (
                    target == self.microphone.in_node
                    and source in physical
                    and source != self.microphone.capture
                )
            if self.callout is not None:
                dangerous = dangerous or (
                    source == self.settings.hfp_source and target != self.callout.in_node
                )
            if self.microphone is not None and self.settings.aec.enabled:
                dangerous = dangerous or (
                    source == AEC_SOURCE and target != self.microphone.in_node
                )
            if dangerous:
                log.warning("removing unsafe auto-link %s -> %s", source, target)
                unlink(source, target)
                changed = True
        return changed

    def validate(
        self,
        links: LinkList,
        microphone: str,
        microphones: tuple[str, ...],
    ) -> bool:
        if self.microphone is None or self.callout is None:
            return False
        if not self.microphone.targets_verified(links) or not self.callout.targets_verified(links):
            return False
        uplink_sources = {source for source, target in links if target == self.settings.hfp_sink}
        if uplink_sources != {self.microphone.out_node}:
            return False
        if self.settings.aec.enabled:
            capture_sources = {source for source, target in links if target == AEC_CAPTURE}
            if capture_sources != {microphone}:
                return False
            playback_targets = {target for source, target in links if source == AEC_PLAYBACK}
            if playback_targets != {self.output_target}:
                return False
        else:
            microphone_sources = {
                source for source, target in links if target == self.microphone.in_node
            }
            if microphone_sources != {microphone}:
                return False
        self.unexpected_links = unexpected_call_links(
            links,
            lark=microphone,
            hfp_source=self.settings.hfp_source,
            hfp_sink=self.settings.hfp_sink,
            microphone_input=self.microphone.in_node,
            callout_input=self.callout.in_node,
            aec_enabled=self.settings.aec.enabled,
            microphones=microphones,
            selected_microphone=microphone,
            microphone_output=self.microphone.out_node,
        )
        return not self.unexpected_links

    def tick(
        self,
        nodes: NodeMap,
        links: LinkList,
        microphone: Any,
        output_node: str | None = DERIVE_OUTPUT,
        *,
        microphone_token: Any = None,
        candidate_nodes: tuple[str, ...] = (),
        microphone_report: dict[str, Any] | None = None,
        microphone_blocked: bool | None = None,
        raw_hfp_sink_present: bool | None = None,
    ) -> None:
        call_up = self.settings.hfp_sink in nodes and self.settings.hfp_source in nodes
        safety_hfp_sink_present = (
            self.settings.hfp_sink in nodes
            if raw_hfp_sink_present is None
            else raw_hfp_sink_present
        )
        selected = selected_microphone_node(microphone)
        token = (
            microphone_token
            if microphone_token is not None
            else selected_microphone_token(microphone)
        )
        report = microphone_report or microphone_resolution_report(microphone)
        blocked = (
            bool(microphone_blocked)
            if microphone_blocked is not None
            else bool(getattr(microphone, "blocked", False))
        )
        discovered = set(candidate_nodes) | set(microphone_candidate_nodes(report))
        if selected:
            discovered.add(selected)
        self.selected_microphone = selected
        self.selected_microphone_token = token
        self.microphone_report = report
        self.candidate_microphones = tuple(sorted(discovered))

        # DERIVE_OUTPUT means the caller does not do output selection -- the test suite and
        # any pre-selection caller -- so fall back to exactly the old rule. An explicit None
        # means the resolver looked and found nothing playable, which is a different thing.
        resolved = (
            (self.settings.wired_output if self.settings.wired_output in nodes else None)
            if output_node == DERIVE_OUTPUT
            else output_node
        )
        old_output = self.output_node
        signature = (call_up, token)
        endpoints_changed = signature != self.signature
        self.update_signature(signature)

        # This is the first graph action on every call tick, including missing/blocked
        # selections. It covers every identity-matched candidate rather than only the active
        # one, so an inactive FIFINE can never autolink beside the selected Lark (or vice versa).
        early_links_removed = False
        if safety_hfp_sink_present:
            early_links_removed = self.remove_dangerous_autolinks(
                links,
                self.candidate_microphones,
                selected,
            )

        if blocked:
            reason = str((report or {}).get("selection_reason") or "microphone identity is unsafe")
            self.teardown(reason)
            self.last_failure = reason
            self.state = State.SAFE
            return

        if not call_up:
            self.break_before_make = False
            # ``update_signature`` already tears an active graph down on the
            # call-up -> call-down transition.  Keep the physical AUX sink at
            # its deterministic level while idle as well: a freshly booted,
            # demo-ready unit must prove the wired output before the phone is
            # available, not defer mixer ownership until HFP appears.
            self.output_node = resolved
            if resolved is None:
                self.output_volume_target = None
                self.output_volume_observed = None
                self.output_volume_verified = False
                self.output_volume_error = "wired output is unavailable"
                self.last_failure = self.output_volume_error
            elif self.ensure_output_volume(resolved):
                self.last_failure = None
            else:
                self.last_failure = self.output_volume_error or "wired output volume did not verify"
            self.state = State.CALL_DOWN
            return

        if selected is None:
            self.teardown("required physical endpoint absent")
            self.last_failure = str(
                (report or {}).get("selection_reason") or "no usable microphone is present"
            )
            self.state = State.WAITING_MIC
            return
        if resolved is None:
            self.teardown("required output endpoint absent")
            self.last_failure = "no usable output is present"
            self.state = State.DISCOVERING
            return

        if early_links_removed:
            if not any(item is not None for item in (self.aec_host, self.microphone, self.callout)):
                self.state = State.DISCOVERING
            # Never construct or validate against the snapshot that contained a raw or
            # duplicate route. Wait until pw-dump/pw-link prove the unlink completed.
            return

        # A selected-instance change first stops bridge.mic in update_signature(). The links
        # are a pre-teardown snapshot, so do not build the new AEC owner until a later snapshot
        # proves the HFP uplink has no stale feed at all.
        if self.break_before_make:
            stale_uplinks = [pair for pair in links if pair[1] == self.settings.hfp_sink]
            if stale_uplinks:
                # The early safety sweep above already requested every stale unlink. Wait
                # for a fresh snapshot instead of issuing duplicate graph mutations.
                return
            self.break_before_make = False

        if self.mic_control_blocked:
            self.state = State.SAFE
            return

        # Mixer verification precedes both initial graph construction and a live switch
        # back to AUX.  A failed set/read leaves every call route torn down.
        if not self.ensure_output_volume(resolved):
            volume_state = (
                self.output_volume_target,
                self.output_volume_observed,
                self.output_volume_error,
            )
            reason = self.output_volume_error or "wired output volume did not verify"
            self.teardown(reason)
            (
                self.output_volume_target,
                self.output_volume_observed,
                self.output_volume_error,
            ) = volume_state
            self.output_node = resolved
            self.last_failure = reason
            self.state = State.SAFE
            log.error("call graph held SAFE: %s", reason)
            return

        if resolved != old_output:
            if not endpoints_changed and resolved is not None and self.can_switch_output_live():
                if self.switch_output_live(resolved):
                    # `links` is the snapshot from before the make-before-break operation.
                    # Revalidate on the next fast tick rather than judging fresh state with
                    # stale evidence.
                    return
                self.fail("live output retarget failed")
                return
            if not endpoints_changed and any(
                item is not None for item in (self.aec_host, self.microphone, self.callout)
            ):
                self.generation += 1
                self.teardown("output changed before graph became active")
                self.attempts = 0
                self.next_attempt = 0.0
                self.last_failure = None
            self.output_node = resolved

        if self.state == State.FAILED:
            if time.monotonic() < self.next_attempt:
                return
            # The burst that exhausted the attempts may be long over. Try again from
            # scratch rather than staying dead for the life of the call.
            log.warning("retrying call graph from FAILED after %.0fs", FAILED_RETRY_SECONDS)
            self.attempts = 0
            self.next_attempt = 0.0
            self.last_failure = None
            self.state = State.DEGRADED
        if time.monotonic() < self.next_attempt:
            self.state = State.DEGRADED
            return
        if self.aec_host is None and self.microphone is None and self.callout is None:
            try:
                self.begin_build(selected)
            except OSError as exc:
                self.fail(f"call graph process could not start: {exc}")
            return

        if self.settings.aec.enabled:
            if self.aec_host is None or not self.aec_host.running:
                self.fail("native AEC owner exited")
                return
            aec_nodes_ready = AEC_SOURCE in nodes and AEC_SINK in nodes
            if self.microphone is None and self.callout is None:
                if aec_nodes_ready:
                    self.start_aec_routes()
                    return
                if time.monotonic() - self.build_started > BUILD_TIMEOUT_SECONDS:
                    self.fail("AEC nodes did not appear before timeout")
                return

        if self.microphone is None or self.callout is None:
            self.fail("call routes are only partially constructed")
            return
        if not self.microphone.running or not self.callout.running:
            self.fail("a call loopback exited")
            return
        if self.microphone_controls_required and not self.mic_link_requested:
            if self.microphone.out_node not in nodes:
                if time.monotonic() - self.routes_started > BUILD_TIMEOUT_SECONDS:
                    self.hold_microphone_control_safe(
                        "microphone control node did not appear before timeout"
                    )
                return
            if not self.prime_microphone_control(nodes):
                self.hold_microphone_control_safe(
                    self.mic_control_error or "microphone controls did not prime muted"
                )
                return
            if not link(self.microphone.out_node, self.settings.hfp_sink):
                self.hold_microphone_control_safe("muted microphone uplink could not be linked")
                return
            self.mic_link_requested = True
            # Validate the manually created link from a fresh snapshot before applying the
            # configured (possibly unmuted) state.
            return
        if time.monotonic() - self.routes_started < ATTACH_GRACE_SECONDS:
            return
        if self.remove_dangerous_autolinks(
            links,
            self.candidate_microphones,
            selected,
        ):
            return
        if not self.validate(links, selected, self.candidate_microphones):
            self.fail("graph targets or safety invariants did not verify")
            return
        if not self.ensure_microphone_control(nodes):
            reason = self.mic_control_error or "microphone controls did not verify"
            self.hold_microphone_control_safe(reason)
            return
        if self.settings.aec.enabled and not self.verified and not set_aec_mute(False):
            self.fail("AEC sink could not be unmuted")
            return

        if not self.verified:
            log.info("call graph generation %d ACTIVE and verified", self.generation)
        self.verified = True
        self.attempts = 0
        self.state = State.ACTIVE

    def status(
        self,
        nodes: NodeMap,
        links: LinkList,
        microphone: Any = None,
        *,
        microphone_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        call_up = self.settings.hfp_sink in nodes and self.settings.hfp_source in nodes
        selected = selected_microphone_node(microphone) or self.selected_microphone
        report = (
            microphone_report or microphone_resolution_report(microphone) or self.microphone_report
        )
        if report is None:
            report = {
                "selected": (
                    {
                        "id": "lark-a1",
                        "label": "Hollyland Lark A1",
                        "priority": 0,
                        "node": selected,
                    }
                    if selected
                    else None
                ),
                "selection_reason": (
                    "legacy Lark microphone is available"
                    if selected
                    else "legacy Lark microphone is absent"
                ),
                "candidates": [],
            }
        expected: LinkList = []
        if self.microphone is not None and self.callout is not None:
            expected.extend(
                [
                    (self.microphone.capture, self.microphone.in_node),
                    (self.microphone.out_node, self.microphone.playback),
                    (self.callout.capture, self.callout.in_node),
                    (self.callout.out_node, self.callout.playback),
                ]
            )
        if self.aec_host is not None and selected is not None:
            expected.extend([(selected, AEC_CAPTURE), (AEC_PLAYBACK, self.output_target)])
        missing = [pair for pair in expected if pair not in links]
        actual_lark = lark_node_from_report(report)
        if actual_lark is None and self.microphone_report is None:
            actual_lark = selected
        return {
            "timestamp": time.time(),
            "state": self.state.value,
            "generation": self.generation,
            "call": {"hfp_nodes_present": call_up},
            "microphone": report,
            "endpoints": {
                "microphone": selected,
                "lark": actual_lark,
                "hfp_source": self.settings.hfp_source if call_up else None,
                "hfp_sink": self.settings.hfp_sink if call_up else None,
                "wired_output": (self.output_target if self.output_target in nodes else None),
            },
            "aec": {
                "enabled": self.settings.aec.enabled,
                "method": self.settings.aec.method,
                "rate": self.settings.aec.rate,
                "channels": self.settings.aec.channels,
                "node_latency_frames": self.settings.aec.node_latency_frames,
                "play_delay_frames": self.settings.aec.play_delay_frames,
                "verified": self.verified and self.settings.aec.enabled,
                "module_backend": ("native-pw-cli" if self.settings.aec.enabled else None),
                "owner_pid": self.aec_host.pid if self.aec_host is not None else None,
            },
            "wired_output_volume": {
                "required": bool(
                    self.settings.wired_output_volume is not None
                    and self.output_target == self.settings.wired_output
                ),
                "target": self.output_volume_target,
                "desired": self.settings.wired_output_volume,
                "observed": self.output_volume_observed,
                "verified": self.output_volume_verified,
                "error": self.output_volume_error,
            },
            "microphone_control": {
                "target": self.mic_control_target,
                "desired_gain_db": self.settings.mic_gain_db,
                "observed_gain_db": self.mic_gain_observed_db,
                "desired_muted": self.settings.mic_muted,
                "observed_muted": self.mic_mute_observed,
                "verified": self.mic_control_verified,
                "error": self.mic_control_error,
            },
            "graph": {
                "expected_links": expected,
                "missing_links": missing,
                "unexpected_links": self.unexpected_links,
            },
            "attempts": self.attempts,
            "last_failure": self.last_failure,
            "config_path": str(self.settings.config_path),
        }


def reconcile_stale_pulse_aec() -> None:
    """Remove only old bridge-owned Pulse modules from pre-native experiments."""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "modules"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in result.stdout.splitlines():
        if "module-echo-cancel" not in line or "bridge.aec." not in line:
            continue
        module_id = line.split(maxsplit=1)[0]
        log.warning("unloading stale bridge-owned Pulse AEC module %s", module_id)
        subprocess.run(
            ["pactl", "unload-module", module_id],
            capture_output=True,
            timeout=10,
            check=False,
        )


def runtime_metrics() -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    try:
        thermal = Path("/sys/class/thermal/thermal_zone0/temp")
        metrics["temperature_c"] = round(
            int(thermal.read_text(encoding="utf-8").strip()) / 1000.0, 2
        )
    except (OSError, ValueError):
        metrics["temperature_c"] = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                metrics["mem_available_kib"] = int(line.split()[1])
                break
    except (OSError, ValueError):
        metrics["mem_available_kib"] = None
    for key, command in {
        "throttled": ["vcgencmd", "get_throttled"],
        "arm_clock": ["vcgencmd", "measure_clock", "arm"],
    }.items():
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
            metrics[key] = result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            metrics[key] = None
    return metrics


def write_status(path: Path, status: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".bridge-status-", delete=False
    ) as handle:
        json.dump(status, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def desire_stamp(path: Path | None = None) -> int | None:
    try:
        return (path or desire_path()).stat().st_mtime_ns
    except OSError:
        return None


def wait_for_next_tick(observed_desire: int | None, should_stop, *, wake=None) -> None:
    """Wake cheaply on an output choice or stable microphone-liveness transition."""
    deadline = time.monotonic() + POLL_SECONDS
    while not should_stop() and time.monotonic() < deadline:
        if desire_stamp() != observed_desire:
            return
        if wake is not None and wake():
            return
        time.sleep(OUTPUT_EVENT_POLL_SECONDS)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG", "INFO").upper(),
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        settings = load_settings()
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as exc:
        log.error("configuration rejected: %s", exc)
        return 2
    if settings.controller_roles is None:
        log.error("configuration rejected: permanent call-controller identity is required")
        return 2

    reconcile_stale_pulse_aec()
    graph = CallGraph(settings)
    lark_liveness = LarkPcmLivenessMonitor()
    lark_liveness_enabled = automatic_lark_liveness_enabled(settings.microphone_candidates)
    if lark_liveness_enabled:
        log.info("Lark transmitter PCM detection enabled with FIFINE fallback")
    stopping = False

    def on_signal(signum: int, _frame: Any) -> None:
        nonlocal stopping
        log.info("signal %s; shutting down", signum)
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info(
        "watching HFP nodes %s / %s; AEC %s",
        settings.hfp_sink,
        settings.hfp_source,
        "enabled" if settings.aec.enabled else "disabled",
    )

    last_metrics = 0.0
    metrics: dict[str, Any] = {}
    output_report: dict[str, Any] | None = None
    last_output: str | None = None
    microphone_resolution: Any = None
    microphone_report: dict[str, Any] = {
        "selected": None,
        "selection_reason": "microphones have not been inspected",
        "blocked": False,
        "candidates": [],
    }
    microphone_capability_cache: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
    controller_block: dict[str, Any] = {
        "policy": "transitional",
        "ready": False,
        "error": "controller roles were not inspected",
        "call": {},
        "output": {},
    }
    while not stopping:
        controller_block, bluez_tree, controller_inventory = inspect_controller_roles(settings)
        call_binding_accepted, call_binding_error = call_role_acceptance(
            settings, bluez_tree, controller_inventory
        )
        snapshot = pw_snapshot()
        nodes = snapshot[0] if snapshot is not None else None
        pw_objects = snapshot[1] if snapshot is not None else []
        links = pw_links()
        observed_desire = desire_stamp()
        if nodes is None or links is None:
            graph.fail("PipeWire graph could not be inspected")
            nodes = nodes or {}
            links = links or []
        else:
            accepted_nodes = accepted_call_nodes(nodes, settings, call_binding_accepted)
            observations, physical_microphone_resolution = discover_microphones(
                pw_objects,
                settings,
                capability_cache=microphone_capability_cache,
            )
            dynamic_availability = lark_liveness.reconcile(
                physical_microphone_resolution,
                links,
                enabled=lark_liveness_enabled,
            )
            import microphones as microphones_module

            microphone_resolution = microphones_module.resolve(
                settings.microphone_candidates,
                observations,
                (
                    {dynamic_availability.candidate_id: dynamic_availability}
                    if dynamic_availability is not None
                    else None
                ),
            )
            microphone_report = microphone_resolution.as_dict()
            microphone_report["lark_transmitter"] = lark_liveness.status()
            desired = read_desire()
            output_node, output_report = resolve_output(
                nodes,
                settings,
                desired,
                bluez_objects=bluez_tree,
                output_controller_ready=bool((controller_block.get("output") or {}).get("ready")),
            )
            if output_node != last_output:
                log.info(
                    "output resolved to %s (desired %s)",
                    output_node,
                    desired or settings.desired_output or "<unset>",
                )
                last_output = output_node
            graph.tick(
                accepted_nodes,
                links,
                microphone_resolution,
                output_node,
                microphone_token=microphone_resolution.instance_token,
                candidate_nodes=microphone_candidate_nodes(microphone_report),
                microphone_report=microphone_report,
                microphone_blocked=microphone_resolution.blocked,
                raw_hfp_sink_present=settings.hfp_sink in nodes,
            )

        now = time.monotonic()
        if now - last_metrics >= 10:
            metrics = runtime_metrics()
            last_metrics = now
        accepted_nodes = accepted_call_nodes(nodes, settings, call_binding_accepted)
        raw_hfp_nodes_present = settings.hfp_sink in nodes and settings.hfp_source in nodes
        status = graph.status(
            accepted_nodes,
            links,
            microphone_resolution,
            microphone_report=microphone_report,
        )
        status["call"].update(
            {
                "raw_hfp_nodes_present": raw_hfp_nodes_present,
                "controller_binding_accepted": call_binding_accepted,
                "controller_binding_error": call_binding_error,
            }
        )
        status["controllers"] = controller_block
        status["system"] = metrics
        # Publish the candidate list and the resolution so a front-end -- a CLI, the phone
        # bridge -- can render a selector by reading one atomic file, with no socket and no
        # privilege beyond reading $XDG_RUNTIME_DIR.
        status["output"] = output_report or {}
        status["mode"] = settings.mode
        try:
            write_status(settings.status_path, status)
        except OSError as exc:
            log.warning("status write failed: %s", exc)
        wait_for_next_tick(
            observed_desire if nodes is not None and links is not None else desire_stamp(),
            lambda: stopping,
            wake=lark_liveness.poll,
        )

    lark_liveness.close()
    graph.teardown("supervisor shutting down")
    graph.state = State.CALL_DOWN
    try:
        stopped_status = graph.status(
            {},
            [],
            microphone_resolution,
            microphone_report=microphone_report,
        )
        stopped_status["call"].update(
            {
                "raw_hfp_nodes_present": False,
                "controller_binding_accepted": False,
                "controller_binding_error": "supervisor stopped",
            }
        )
        stopped_status["controllers"] = controller_block
        write_status(settings.status_path, stopped_status)
    except OSError:
        pass
    log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
