#!/usr/bin/env python3
"""bridge-supervisor — create the Mode 1W audio path only while a call is up.

Runs as a user systemd service on the Pi. Python because it is pure policy: it decides
WHEN links should exist and shells out to pw-loopback to make them. It never touches PCM
samples — PipeWire does that (ADR-0003).

WHY THIS EXISTS INSTEAD OF PURE CONFIG
--------------------------------------
ADR-0002 declared the loopbacks statically, assuming endpoints are persistent. They are
not. The HFP nodes (bluez_input/bluez_output for the phone) exist ONLY while SCO is up,
i.e. during a call. Measured consequence of the static approach:

    WirePlumber does not leave a stream waiting when target.object is absent.
    It links it to the DEFAULT device instead.

With no call active that produced:
    bridge.mic.playback    -> dongle B   (wanted: HFP sink)
    bridge.callout.capture -> Lark       (wanted: HFP source)

which closes Lark -> callout.playback -> car speakers -> Lark: a live acoustic feedback
loop, unattended. `node.dont-reconnect = true` does not fix it — the streams error when
their target is missing at creation and take the whole loopback module down.

So: create the loopbacks when the targets appear, destroy them when they vanish. The
fallback window cannot occur because there is nothing to fall back.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not connect Bluetooth devices. Android initiates to headsets and racing it causes
collisions (PLAN.md §6.5). The phone connects itself; this only reacts.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time

POLL_SECONDS = 2.0

# Deterministic node names. bluez names derive from the phone's MAC; ALSA names from USB
# vendor/product/serial. Both survive reboots and replugs, unlike card numbers or node ids.
LARK = os.environ.get(
    "BRIDGE_LARK",
    "alsa_input.usb-Shenzhen_Hollyland_Technology_Co._Ltd_Wireless_Microphone"
    "_Wireless_Microphone-01.analog-stereo",
)
WIRED_OUT = os.environ.get(
    "BRIDGE_WIRED_OUT",
    "alsa_output.usb-Generic_AB13X_USB_Audio_202405280846-00.analog-stereo",
)
PHONE_MAC = os.environ.get("BRIDGE_PHONE_MAC", "5C:33:7B:CB:BF:C5")
_M = PHONE_MAC.replace(":", "_")
HFP_SINK = f"bluez_output.{_M}.1"    # audio TO the phone   (our microphone uplink)
HFP_SOURCE = f"bluez_input.{_M}.0"   # audio FROM the phone (call audio downlink)

log = logging.getLogger("bridge-supervisor")


def pw_nodes() -> set[str]:
    """Names of every node currently in the graph. Empty set if PipeWire is unreachable."""
    try:
        out = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
        objs = json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("pw-dump failed (%s); treating graph as unknown", exc)
        return set()
    names = set()
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        n = ((o.get("info") or {}).get("props") or {}).get("node.name")
        if n:
            names.add(str(n))
    return names


class Loopback:
    """One pw-loopback process, started only when both endpoints exist."""

    def __init__(self, name: str, capture: str, playback: str, channels: str):
        self.name = name
        self.capture = capture
        self.playback = playback
        self.channels = channels
        self.proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        cmd = [
            "pw-loopback",
            "--name", self.name,
            "--capture", self.capture,
            "--playback", self.playback,
            "--channels", self.channels,
            "-P", '{ media.role=Communication node.pause-on-idle=false }',
        ]
        log.info("starting %s: %s -> %s", self.name, self.capture, self.playback)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def stop(self, why: str) -> None:
        if not self.running:
            self.proc = None
            return
        log.info("stopping %s (%s)", self.name, why)
        assert self.proc is not None
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("%s did not exit; killing", self.name)
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc = None


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG", "INFO").upper(),
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    mic = Loopback("bridge.mic", LARK, HFP_SINK, "1")
    callout = Loopback("bridge.callout", HFP_SOURCE, WIRED_OUT, "2")
    legs = (mic, callout)

    stopping = False

    def on_signal(signum, _frame):
        nonlocal stopping
        log.info("signal %s — shutting down", signum)
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log.info("watching for HFP nodes: %s / %s", HFP_SINK, HFP_SOURCE)
    last_call_up: bool | None = None

    while not stopping:
        nodes = pw_nodes()

        # A call is "up" only when BOTH HFP nodes are present. One without the other is a
        # transient during setup or teardown; acting on it would create a half-graph.
        call_up = HFP_SINK in nodes and HFP_SOURCE in nodes

        if call_up != last_call_up:
            log.info("call %s", "UP" if call_up else "DOWN")
            last_call_up = call_up

        if call_up:
            # Only start a leg once its own endpoints are both present. The Lark or the
            # wired output can be unplugged independently of the phone.
            if LARK in nodes:
                mic.start()
            elif mic.running:
                mic.stop("lark disappeared")

            if WIRED_OUT in nodes:
                callout.start()
            elif callout.running:
                callout.stop("wired output disappeared")
        else:
            for leg in legs:
                leg.stop("call ended")

        # A pw-loopback that died on its own (PipeWire restart, target vanished mid-call)
        # must not be left as a phantom: clear it so the next tick restarts it cleanly.
        for leg in legs:
            if leg.proc is not None and leg.proc.poll() is not None:
                log.warning("%s exited unexpectedly (rc=%s)", leg.name, leg.proc.returncode)
                leg.proc = None

        time.sleep(POLL_SECONDS)

    for leg in legs:
        leg.stop("supervisor shutting down")
    log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
