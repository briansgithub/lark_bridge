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
import os
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

POLL_SECONDS = 2.0
BUILD_TIMEOUT_SECONDS = 10.0
ATTACH_GRACE_SECONDS = 4.0
MAX_BUILD_ATTEMPTS = 5

DEFAULT_LARK = (
    "alsa_input.usb-Shenzhen_Hollyland_Technology_Co._Ltd_Wireless_Microphone"
    "_Wireless_Microphone-01.analog-stereo"
)
DEFAULT_LARK_COMPONENT = "USB3547:0407"
DEFAULT_WIRED_OUT = "alsa_output.platform-3f00b840.mailbox.stereo-fallback"
DEFAULT_PHONE_MAC = "5C:33:7B:CB:BF:C5"

AEC_SOURCE = "bridge.aec.source"
AEC_SINK = "bridge.aec.sink"
AEC_CAPTURE = "echo-cancel-capture"
AEC_PLAYBACK = "echo-cancel-playback"

log = logging.getLogger("bridge-supervisor")


class State(str, Enum):
    CALL_DOWN = "CALL_DOWN"
    DISCOVERING = "DISCOVERING"
    BUILDING = "BUILDING"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class AecSettings:
    enabled: bool = False
    method: str = "webrtc"
    rate: int = 48_000
    channels: int = 1
    failure_policy: str = "fail_closed"


@dataclass(frozen=True)
class Settings:
    aec: AecSettings
    lark_node: str = DEFAULT_LARK
    lark_component: str = DEFAULT_LARK_COMPONENT
    wired_output: str = DEFAULT_WIRED_OUT
    phone_mac: str = DEFAULT_PHONE_MAC
    status_path: Path = Path("/tmp/bridge-status.json")
    config_path: Path | None = None

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


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "bridge.toml"


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

    aec_data = (data.get("audio") or {}).get("aec") or {}
    enabled = bool(aec_data.get("enabled", False))
    if "BRIDGE_AEC_ENABLED" in os.environ:
        enabled = parse_bool(os.environ["BRIDGE_AEC_ENABLED"])
    method = str(aec_data.get("method", "webrtc"))
    rate = int(aec_data.get("rate", 48_000))
    channels = int(aec_data.get("channels", 1))
    failure_policy = str(aec_data.get("failure_policy", "fail_closed"))

    if method != "webrtc":
        raise ValueError("audio.aec.method must be 'webrtc'")
    if rate != 48_000:
        raise ValueError("audio.aec.rate must remain 48000 on this fixed-rate graph")
    if channels != 1:
        raise ValueError("audio.aec.channels must be 1 on the Pi 3 optimized graph")
    if failure_policy != "fail_closed":
        raise ValueError("audio.aec.failure_policy must be 'fail_closed'")

    phone = str(((data.get("devices") or {}).get("phone") or {}).get("address", ""))
    if not phone or phone == "AA:BB:CC:DD:EE:FF":
        phone = DEFAULT_PHONE_MAC

    status_value = os.environ.get("BRIDGE_STATUS")
    return Settings(
        aec=AecSettings(enabled, method, rate, channels, failure_policy),
        lark_node=os.environ.get("BRIDGE_LARK", DEFAULT_LARK),
        lark_component=os.environ.get("BRIDGE_LARK_COMPONENT", DEFAULT_LARK_COMPONENT),
        wired_output=os.environ.get("BRIDGE_WIRED_OUT", DEFAULT_WIRED_OUT),
        phone_mac=os.environ.get("BRIDGE_PHONE_MAC", phone).upper(),
        status_path=Path(status_value) if status_value else default_status_path(),
        config_path=config_path,
    )


NodeMap = dict[str, dict[str, Any]]
LinkList = list[tuple[str, str]]


def pw_nodes() -> NodeMap | None:
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode != 0:
            return None
        objects = json.loads(result.stdout)
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
            nodes[str(name)] = props
    return nodes


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


def unlink(source: str, target: str) -> None:
    try:
        subprocess.run(
            ["pw-link", "-d", source, target],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("failed to remove link %s -> %s: %s", source, target, exc)


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


class Loopback:
    """A child pw-loopback with explicit, continuously verified targets."""

    def __init__(self, name: str, capture: str, playback: str, channels: int):
        self.name = name
        self.capture = capture
        self.playback = playback
        self.channels = channels
        self.proc: subprocess.Popen[str] | None = None

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
            "--playback",
            self.playback,
            "--channels",
            str(self.channels),
        ]
        log.info("starting %s: %s -> %s", self.name, self.capture, self.playback)
        self.proc = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True
        )

    def targets_verified(self, links: LinkList) -> bool:
        outputs = {target for source, target in links if source == self.out_node}
        inputs = {source for source, target in links if target == self.in_node}
        return self.playback in outputs and self.capture in inputs

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

    def __init__(self, settings: AecSettings, microphone: str, output: str):
        self.settings = settings
        self.microphone = microphone
        self.output = output
        self.proc: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.running and self.proc is not None else None

    def module_command(self) -> str:
        arguments = " ".join(
            [
                "{",
                "library.name = aec/libspa-aec-webrtc",
                f"audio.rate = {self.settings.rate}",
                f"audio.channels = {self.settings.channels}",
                "audio.position = [ MONO ]",
                "aec.args = {",
                "webrtc.noise_suppression = false",
                "webrtc.gain_control = false",
                "webrtc.voice_detection = false",
                "webrtc.high_pass_filter = true",
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
        )
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
) -> LinkList:
    unexpected: LinkList = []
    for source, target in links:
        if (
            source == lark
            and target == hfp_sink
            or source == hfp_source
            and target != callout_input
            or aec_enabled
            and source == AEC_SOURCE
            and target != microphone_input
        ):
            unexpected.append((source, target))
    return unexpected


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

    def teardown(self, reason: str) -> None:
        if self.aec_host is not None:
            set_aec_mute(True)
        if self.callout is not None:
            self.callout.stop(reason)
        if self.microphone is not None:
            self.microphone.stop(reason)
        if self.aec_host is not None:
            self.aec_host.stop(reason)
        self.callout = None
        self.microphone = None
        self.aec_host = None
        self.build_started = 0.0
        self.routes_started = 0.0
        self.verified = False
        self.unexpected_links = []

    def update_signature(self, signature: tuple[Any, ...]) -> None:
        if signature == self.signature:
            return
        self.generation += 1
        self.teardown("endpoint generation changed")
        self.signature = signature
        self.attempts = 0
        self.next_attempt = 0.0
        self.last_failure = None

    def fail(self, reason: str) -> None:
        self.last_failure = reason
        self.attempts += 1
        log.error("call graph generation %d failed: %s", self.generation, reason)
        self.teardown(reason)
        if self.attempts >= MAX_BUILD_ATTEMPTS:
            self.state = State.FAILED
        else:
            self.state = State.DEGRADED
            self.next_attempt = time.monotonic() + min(2**self.attempts, 30)

    def begin_build(self, lark: str) -> None:
        self.state = State.BUILDING
        self.build_started = time.monotonic()
        if self.settings.aec.enabled:
            self.aec_host = NativeAecHost(self.settings.aec, lark, self.settings.wired_output)
            self.aec_host.start()
            return
        self.callout = Loopback(
            "bridge.callout", self.settings.hfp_source, self.settings.wired_output, 2
        )
        self.microphone = Loopback("bridge.mic", lark, self.settings.hfp_sink, 1)
        self.callout.start()
        self.microphone.start()
        self.routes_started = time.monotonic()

    def start_aec_routes(self) -> None:
        set_aec_mute(True)
        self.callout = Loopback("bridge.callout", self.settings.hfp_source, AEC_SINK, 1)
        self.microphone = Loopback("bridge.mic", AEC_SOURCE, self.settings.hfp_sink, 1)
        self.callout.start()
        self.microphone.start()
        self.routes_started = time.monotonic()

    def remove_dangerous_autolinks(self, links: LinkList, lark: str) -> bool:
        changed = False
        for source, target in links:
            dangerous = source == lark and target == self.settings.hfp_sink
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

    def validate(self, links: LinkList, lark: str) -> bool:
        if self.microphone is None or self.callout is None:
            return False
        if not self.microphone.targets_verified(links) or not self.callout.targets_verified(links):
            return False
        if self.settings.aec.enabled:
            if (lark, AEC_CAPTURE) not in links:
                return False
            if (AEC_PLAYBACK, self.settings.wired_output) not in links:
                return False
        self.unexpected_links = unexpected_call_links(
            links,
            lark=lark,
            hfp_source=self.settings.hfp_source,
            hfp_sink=self.settings.hfp_sink,
            microphone_input=self.microphone.in_node,
            callout_input=self.callout.in_node,
            aec_enabled=self.settings.aec.enabled,
        )
        return not self.unexpected_links

    def tick(self, nodes: NodeMap, links: LinkList, lark: str | None) -> None:
        call_up = self.settings.hfp_sink in nodes and self.settings.hfp_source in nodes
        output_up = self.settings.wired_output in nodes
        signature = (call_up, lark, output_up)
        self.update_signature(signature)

        if not call_up:
            self.teardown("call down")
            self.state = State.CALL_DOWN
            return
        if lark is None or not output_up:
            self.teardown("required physical endpoint absent")
            self.state = State.DISCOVERING
            return
        if self.state == State.FAILED:
            return
        if time.monotonic() < self.next_attempt:
            self.state = State.DEGRADED
            return
        if self.aec_host is None and self.microphone is None and self.callout is None:
            try:
                self.begin_build(lark)
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
        if time.monotonic() - self.routes_started < ATTACH_GRACE_SECONDS:
            return
        if self.remove_dangerous_autolinks(links, lark):
            return
        if not self.validate(links, lark):
            self.fail("graph targets or safety invariants did not verify")
            return
        if self.settings.aec.enabled and not self.verified and not set_aec_mute(False):
            self.fail("AEC sink could not be unmuted")
            return

        if not self.verified:
            log.info("call graph generation %d ACTIVE and verified", self.generation)
        self.verified = True
        self.attempts = 0
        self.state = State.ACTIVE

    def status(self, nodes: NodeMap, links: LinkList, lark: str | None) -> dict[str, Any]:
        call_up = self.settings.hfp_sink in nodes and self.settings.hfp_source in nodes
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
        if self.aec_host is not None and lark is not None:
            expected.extend([(lark, AEC_CAPTURE), (AEC_PLAYBACK, self.settings.wired_output)])
        missing = [pair for pair in expected if pair not in links]
        return {
            "timestamp": time.time(),
            "state": self.state.value,
            "generation": self.generation,
            "call": {"hfp_nodes_present": call_up},
            "endpoints": {
                "lark": lark,
                "hfp_source": self.settings.hfp_source if call_up else None,
                "hfp_sink": self.settings.hfp_sink if call_up else None,
                "wired_output": (
                    self.settings.wired_output if self.settings.wired_output in nodes else None
                ),
            },
            "aec": {
                "enabled": self.settings.aec.enabled,
                "method": self.settings.aec.method,
                "rate": self.settings.aec.rate,
                "channels": self.settings.aec.channels,
                "verified": self.verified and self.settings.aec.enabled,
                "module_backend": ("native-pw-cli" if self.settings.aec.enabled else None),
                "owner_pid": self.aec_host.pid if self.aec_host is not None else None,
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


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG", "INFO").upper(),
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        settings = load_settings()
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        log.error("configuration rejected: %s", exc)
        return 2

    reconcile_stale_pulse_aec()
    graph = CallGraph(settings)
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
    while not stopping:
        nodes = pw_nodes()
        links = pw_links()
        if nodes is None or links is None:
            graph.fail("PipeWire graph could not be inspected")
            nodes = nodes or {}
            links = links or []
        else:
            lark = find_lark(nodes, settings)
            graph.tick(nodes, links, lark)

        now = time.monotonic()
        if now - last_metrics >= 10:
            metrics = runtime_metrics()
            last_metrics = now
        lark = find_lark(nodes, settings) if nodes else None
        status = graph.status(nodes, links, lark)
        status["system"] = metrics
        try:
            write_status(settings.status_path, status)
        except OSError as exc:
            log.warning("status write failed: %s", exc)
        time.sleep(POLL_SECONDS)

    graph.teardown("supervisor shutting down")
    graph.state = State.CALL_DOWN
    try:
        write_status(settings.status_path, graph.status({}, [], None))
    except OSError:
        pass
    log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
