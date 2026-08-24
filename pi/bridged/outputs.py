#!/usr/bin/env python3
"""Enumerate the places call audio can be played, and decide which one is live.

WHY THIS IS A MODULE AND NOT A CONFIG STRING
--------------------------------------------
Until now the supervisor had exactly one output, `alsa_output.platform-...mailbox`, hardcoded
at bridge_supervisor.py:47. Mode 1 adds Bluetooth speakers, and the operator requirement is
explicit: **the user must be able to select which device the far end plays on.** A single
config string cannot express that, because:

  * the set of candidates changes while the appliance runs -- a speaker is powered on, driven
    out of range, or bonded for the first time;
  * the choice must survive the phone being absent, so it cannot live on the phone;
  * the supervisor reads its config exactly once at startup and has no reload path, so
    runtime selection cannot be config-driven at all.

So this module answers two questions and nothing else: *what could we play to* and *what
should we play to*. Applying the answer is the supervisor's job; asking on the user's behalf
is a front-end's job. Keeping those three apart is what lets a CLI, a phone app and a plain
status file all drive the same behaviour without duplicating policy.

WHAT COUNTS AS A CANDIDATE, AND THE TRAP IN IT
----------------------------------------------
A Bluetooth speaker is a bonded device whose advertised UUIDs include **0000110b, A2DP Sink**.
That test is load-bearing, not decoration: the PHONE also owns a `bluez_output.*` PipeWire node
while a call is up -- `bluez_output.5C_33_7B_CB_BF_C5.1`, profile `headset-audio-gateway` --
and routing the far end's own voice back into the phone would be a feedback loop. Measured on
the unit: the Pixel reports `a2dp_sink=False`, every speaker reports `True`, so the UUID test
separates them cleanly where a name prefix would not.

The 3.5 mm jack is a first-class candidate, not a fallback bolted on the side. It is the only
output that is always present, it is the shipped Mode 1W path, and the operator asked to be
able to select "which device to route the audio out" -- which includes choosing the wire.

DELIBERATELY NOT HERE: connecting, disconnecting, pairing, or writing config. This module
reads. A module that both decides and acts is one that cannot be tested without hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import btadapters

A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

# PipeWire profile string for a real speaker, as opposed to the phone's HFP endpoint. Read off
# api.bluez5.profile; measured values are "a2dp-sink" for a speaker and
# "headset-audio-gateway" for the Pixel mid-call.
A2DP_PROFILE = "a2dp-sink"

WIRED_PREFIX = "alsa_output."
BLUEZ_PREFIX = "bluez_output."


@dataclass(frozen=True)
class Output:
    """One place call audio could go.

    `id` is the stable handle a front-end round-trips; it is deliberately NOT the PipeWire node
    name, because a Bluetooth speaker has no node at all while it is switched off and we still
    have to be able to name it in a list and select it.
    """

    id: str
    kind: str  # "wired" | "a2dp"
    label: str
    node: str | None
    connected: bool
    adapter: str | None = None
    adapter_address: str | None = None
    address: str | None = None

    @property
    def present(self) -> bool:
        """True when this output can be routed to *right now* -- it has a live graph node."""
        return self.node is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "node": self.node,
            "present": self.present,
            "connected": self.connected,
            "adapter": self.adapter,
            "adapter_address": self.adapter_address,
            "address": self.address,
        }


def wired_id(node: str) -> str:
    return f"wired:{node}"


def a2dp_id(address: str) -> str:
    return f"a2dp:{address.strip().upper()}"


def _node_underscored(address: str) -> str:
    return address.strip().upper().replace(":", "_")


def find_a2dp_node(nodes: dict[str, dict], address: str) -> str | None:
    """The live PipeWire sink for a speaker, matched by prefix rather than by suffix.

    The profile index suffix is NOT a constant. `bluez_output.<MAC>.1` happens to be what both
    the iWorld and the Boombox produced, but that trailing number is a profile index and
    hardcoding it would break on the first device that negotiates differently. Match the MAC
    and require the a2dp-sink profile instead.
    """
    prefix = f"{BLUEZ_PREFIX}{_node_underscored(address)}."
    for name, props in nodes.items():
        if not name.startswith(prefix):
            continue
        if props.get("api.bluez5.profile") == A2DP_PROFILE:
            return name
    return None


def wired_outputs(nodes: dict[str, dict]) -> list[Output]:
    """Every local ALSA sink, most-preferred first.

    A USB DAC is listed ahead of the onboard PWM jack because E09 and E12 both found the
    bcm2835 PWM output to be the weakest link in the chain, and because E07 runs 11-12 found
    moving Mode 1W's output off USB made controller wedges rarer -- so neither is a clear
    winner and the ordering exists only to be deterministic. Do not read policy into it.
    """
    found: list[Output] = []
    for name, props in sorted(nodes.items()):
        if not name.startswith(WIRED_PREFIX):
            continue
        if props.get("media.class") != "Audio/Sink":
            continue
        label = str(props.get("node.description") or name)
        found.append(
            Output(id=wired_id(name), kind="wired", label=label, node=name, connected=True)
        )
    # Platform (onboard) sinks last: "usb" sorts before "platform" only by accident, so be
    # explicit rather than relying on the alphabet.
    found.sort(key=lambda o: ("platform-" in (o.node or ""), o.node or ""))
    return found


def a2dp_outputs(
    nodes: dict[str, dict],
    objects: dict[str, dict] | None = None,
    speaker_adapter: str | None = None,
) -> list[Output]:
    """Every bonded A2DP speaker, whether or not it is switched on.

    Offline speakers are included on purpose. A selector that only lists what is already
    connected cannot be used to turn something on, which is precisely the thing the user wants
    to do when the car stereo is silent.

    A device bonded on BOTH adapters -- measured on the iWorld, which ended up on hci0 and
    hci1 -- yields one entry, not two. The adapter chosen is, in order: the one it is actually
    connected on, then `speaker_adapter` if it is bonded there, then the lowest path. Getting
    this wrong is not cosmetic: it is how a speaker ends up being paged from the radio carrying
    the call.
    """
    tree = objects if objects is not None else btadapters.managed_objects()
    by_address: dict[str, dict[str, Any]] = {}
    adapter_addresses = {
        path.split("/")[-1]: str(
            (((interfaces.get("org.bluez.Adapter1") or {}).get("Address") or {}).get("data"))
            or ""
        ).upper()
        for path, interfaces in tree.items()
        if "org.bluez.Adapter1" in interfaces
    }

    for path, interfaces in sorted(tree.items()):
        device = interfaces.get("org.bluez.Device1")
        if not device:
            continue

        def prop(key: str, _device: dict = device) -> Any:
            return (_device.get(key) or {}).get("data")

        uuids = [str(u).lower() for u in (prop("UUIDs") or [])]
        if A2DP_SINK_UUID not in uuids:
            continue
        address = str(prop("Address") or "").upper()
        if not address:
            continue

        adapter = path.split("/")[3]
        adapter_address = adapter_addresses.get(adapter) or None
        connected = bool(prop("Connected"))
        entry = by_address.get(address)
        # Preference order, highest first. Recomputed rather than short-circuited so the
        # comparison is visible.
        preferred_adapter = speaker_adapter in {adapter, adapter_address}
        rank = (2 if connected else 0) + (1 if preferred_adapter else 0)
        if entry is None or rank > entry["rank"]:
            by_address[address] = {
                "rank": rank,
                "adapter": adapter,
                "adapter_address": adapter_address,
                "connected": connected,
                "label": str(prop("Alias") or prop("Name") or address),
            }

    outputs = [
        Output(
            id=a2dp_id(address),
            kind="a2dp",
            label=entry["label"],
            node=find_a2dp_node(nodes, address),
            connected=entry["connected"],
            adapter=entry["adapter"],
            adapter_address=entry["adapter_address"],
            address=address,
        )
        for address, entry in by_address.items()
    ]
    # Connected first, then alphabetical, so a list shown to a human is stable between polls.
    outputs.sort(key=lambda o: (not o.connected, o.label.lower()))
    return outputs


def candidates(
    nodes: dict[str, dict],
    objects: dict[str, dict] | None = None,
    speaker_adapter: str | None = None,
) -> list[Output]:
    """Everything selectable, wired first.

    Wired leads because it is the output that cannot fail to exist, so a front-end rendering
    the list in order always shows a working choice at the top.
    """
    return wired_outputs(nodes) + a2dp_outputs(nodes, objects, speaker_adapter)


def by_id(outputs: list[Output], output_id: str | None) -> Output | None:
    if not output_id:
        return None
    for output in outputs:
        if output.id == output_id:
            return output
    return None


@dataclass(frozen=True)
class Resolution:
    """What the supervisor should actually use, and why -- the reason is for humans."""

    chosen: Output | None
    reason: str
    desired_id: str | None
    desired_available: bool

    @property
    def node(self) -> str | None:
        return self.chosen.node if self.chosen else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.as_dict() if self.chosen else None,
            "reason": self.reason,
            "desired_id": self.desired_id,
            "desired_available": self.desired_available,
        }


def resolve(
    desired_id: str | None,
    outputs: list[Output],
    *,
    fallback: bool = True,
    prefer_speaker: bool = False,
) -> Resolution:
    """Pick the live output. Never mutates the desire.

    The desire is sticky by design. If the user chose the car stereo and the car is switched
    off, the answer is "play on the wire for now" -- NOT "the user now wants the wire". Silently
    rewriting an explicit choice because the device was briefly absent is how an appliance
    starts arguing with its owner, and in a car the owner cannot argue back.

    With `fallback=False` an unavailable desire resolves to nothing, so a caller can honour
    fail-closed rather than quietly playing somewhere the user did not ask for.
    """
    desired = by_id(outputs, desired_id)
    if desired is not None and desired.present:
        return Resolution(desired, "desired output is available", desired_id, True)

    if not fallback:
        reason = (
            "desired output is not available and fallback is disabled"
            if desired is not None
            else "no desired output set and fallback is disabled"
        )
        return Resolution(None, reason, desired_id, False)

    # Fallback ORDER follows the configured mode; an explicit desire, handled above, always
    # outranks it. `prefer_speaker` is set for Mode 1 and clear for Mode 1W.
    #
    # This distinction is not cosmetic. Without it, a Mode 1W unit -- the proven, shipped
    # configuration whose whole point is the wired output -- would silently move call audio
    # to any bonded speaker that happened to be switched on nearby, purely because it was
    # connected. That is a regression in the fallback, dressed up as a feature.
    order = ("a2dp", "wired") if prefer_speaker else ("wired", "a2dp")
    for kind in order:
        for output in outputs:
            if output.kind != kind or not output.present:
                continue
            noun = "a connected speaker" if kind == "a2dp" else "the wired output"
            reason = (
                f"desired output unavailable; using {noun}"
                if desired_id
                else f"no desired output set; using {noun}"
            )
            return Resolution(output, reason, desired_id, False)

    return Resolution(None, "no output of any kind is present", desired_id, False)
