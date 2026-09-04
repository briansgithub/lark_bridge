#!/usr/bin/env python3
"""Pure microphone inventory and priority resolution.

The supervisor owns subprocesses and graph mutations.  This module deliberately does
neither: callers provide one PipeWire snapshot plus any sysfs/capability facts they have
collected, and receive a deterministic, status-ready decision.

Explicit ``[[devices.microphones]]`` entries are identity-strict.  A preferred PipeWire
node name chooses a profile *after* USB identity has been established; it is never an
identity shortcut.  The synthesized legacy Lark entry is the sole exception so existing
installations retain the historical exact-node-first/component-fallback behaviour.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import product
from typing import Any

DEFAULT_LARK_NODE = (
    "alsa_input.usb-Shenzhen_Hollyland_Technology_Co._Ltd_Wireless_Microphone"
    "_Wireless_Microphone-01.analog-stereo"
)
DEFAULT_LARK_COMPONENT = "USB3547:0407"
_USB_COMPONENT_RE = re.compile(r"USB([0-9a-fA-F]{4}):([0-9a-fA-F]{4})")
_HEX_ID_RE = re.compile(r"(?:0x|usb:)?([0-9a-fA-F]{1,4})\Z", re.IGNORECASE)


def normalize_usb_id(value: Any) -> str:
    """Return a lowercase, four-digit USB identifier or raise a useful error."""
    if isinstance(value, bool):
        raise TypeError("USB identifiers must be hexadecimal strings or integers")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"USB identifier is outside 0000..ffff: {value!r}")
        return f"{value:04x}"
    raw = str(value).strip()
    match = _HEX_ID_RE.fullmatch(raw)
    if not match:
        raise ValueError(f"invalid USB identifier: {value!r}")
    return match.group(1).lower().zfill(4)


def normalize_audio_format(value: Any) -> str:
    """Canonical spelling used for comparisons and public status (for example S16LE)."""
    normalized = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    if not normalized:
        raise ValueError("audio format cannot be blank")
    return normalized


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_usb_id(value: Any) -> str | None:
    normalized = _optional_text(value)
    return normalize_usb_id(normalized) if normalized is not None else None


def _component_ids(component: str) -> tuple[str, str] | None:
    match = _USB_COMPONENT_RE.fullmatch(component.strip())
    if not match:
        return None
    return normalize_usb_id(match.group(1)), normalize_usb_id(match.group(2))


def _component_tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [str(item) for item in value]
    else:
        values = [str(value)]
    tokens: set[str] = set()
    for raw in values:
        tokens.update(match.group(0).upper() for match in _USB_COMPONENT_RE.finditer(raw))
        tokens.update(part.strip().upper() for part in re.split(r"[,\s]+", raw) if part.strip())
    return tuple(sorted(tokens))


@dataclass(frozen=True, order=True)
class MicrophoneFormat:
    rate: int
    format: str
    channels: int

    def __post_init__(self) -> None:
        if isinstance(self.rate, bool) or not isinstance(self.rate, int) or self.rate <= 0:
            raise ValueError("microphone format rate must be a positive integer")
        if (
            isinstance(self.channels, bool)
            or not isinstance(self.channels, int)
            or self.channels <= 0
        ):
            raise ValueError("microphone format channels must be a positive integer")
        object.__setattr__(self, "format", normalize_audio_format(self.format))

    def as_dict(self) -> dict[str, Any]:
        return {"rate": self.rate, "format": self.format, "channels": self.channels}


@dataclass(frozen=True)
class DynamicAvailability:
    """Runtime eligibility evidence bound to one physical microphone generation."""

    candidate_id: str
    instance_token: str
    state: str
    reason: str

    def __post_init__(self) -> None:
        candidate_id = str(self.candidate_id).strip()
        instance_token = str(self.instance_token).strip()
        state = str(self.state).strip().lower()
        reason = str(self.reason).strip()
        if not candidate_id:
            raise ValueError("dynamic microphone availability candidate_id cannot be blank")
        if not instance_token:
            raise ValueError("dynamic microphone availability instance_token cannot be blank")
        if state not in {"active", "inactive", "unknown", "error"}:
            raise ValueError(f"invalid dynamic microphone availability state: {self.state!r}")
        if not reason:
            raise ValueError("dynamic microphone availability reason cannot be blank")
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "instance_token", instance_token)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)

    def as_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "reason": self.reason,
            "instance_token": self.instance_token,
        }


@dataclass(frozen=True)
class MicrophoneCandidate:
    id: str
    label: str
    node_name: str | None
    usb_vendor_id: str
    usb_product_id: str
    usb_product: str | None = None
    usb_serial: str | None = None
    usb_port_path: str | None = None
    required_rate: int | None = None
    required_format: str | None = None
    required_channels: int | None = None
    capture_only: bool = True
    capture_control: str | None = None
    capture_gain_db: float | None = None
    alsa_component: str | None = None
    legacy: bool = False

    def __post_init__(self) -> None:
        candidate_id = str(self.id).strip()
        label = str(self.label).strip()
        if not candidate_id:
            raise ValueError("microphone candidate id cannot be blank")
        if not label:
            raise ValueError(f"microphone {candidate_id!r} label cannot be blank")
        object.__setattr__(self, "id", candidate_id)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "node_name", _optional_text(self.node_name))
        vendor = normalize_usb_id(self.usb_vendor_id)
        product_id = normalize_usb_id(self.usb_product_id)
        object.__setattr__(self, "usb_vendor_id", vendor)
        object.__setattr__(self, "usb_product_id", product_id)
        object.__setattr__(self, "usb_product", _optional_text(self.usb_product))
        object.__setattr__(self, "usb_serial", _optional_text(self.usb_serial))
        object.__setattr__(self, "usb_port_path", _optional_text(self.usb_port_path))
        component = (
            _optional_text(self.alsa_component) or f"USB{vendor.upper()}:{product_id.upper()}"
        )
        object.__setattr__(self, "alsa_component", component.upper())
        if self.required_format is not None:
            object.__setattr__(
                self, "required_format", normalize_audio_format(self.required_format)
            )
        capability_fields = (
            self.required_rate,
            self.required_format,
            self.required_channels,
        )
        if any(value is not None for value in capability_fields) and not all(
            value is not None for value in capability_fields
        ):
            raise ValueError(
                f"microphone {candidate_id!r} required_rate, required_format, and "
                "required_channels must be set together"
            )
        for field_name in ("required_rate", "required_channels"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"microphone {candidate_id!r} {field_name} must be positive")
        if not isinstance(self.capture_only, bool):
            raise TypeError(f"microphone {candidate_id!r} capture_only must be a boolean")
        control = _optional_text(self.capture_control)
        object.__setattr__(self, "capture_control", control)
        gain = self.capture_gain_db
        if (control is None) != (gain is None):
            raise ValueError(
                f"microphone {candidate_id!r} capture_control and capture_gain_db "
                "must be set together"
            )
        if gain is not None:
            if isinstance(gain, bool) or not isinstance(gain, (int, float)):
                raise TypeError(
                    f"microphone {candidate_id!r} capture_gain_db must be a finite number"
                )
            gain = float(gain)
            if not math.isfinite(gain):
                raise ValueError(
                    f"microphone {candidate_id!r} capture_gain_db must be finite"
                )
            object.__setattr__(self, "capture_gain_db", gain)

    @property
    def required_capability(self) -> MicrophoneFormat | None:
        if (
            self.required_rate is None
            or self.required_format is None
            or self.required_channels is None
        ):
            return None
        return MicrophoneFormat(
            self.required_rate,
            self.required_format,
            self.required_channels,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "node_name": self.node_name,
            "usb_vendor_id": self.usb_vendor_id,
            "usb_product_id": self.usb_product_id,
            "usb_product": self.usb_product,
            "usb_serial": self.usb_serial,
            "usb_port_path": self.usb_port_path,
            "required_rate": self.required_rate,
            "required_format": self.required_format,
            "required_channels": self.required_channels,
            "capture_only": self.capture_only,
            "capture_control": self.capture_control,
            "capture_gain_db": self.capture_gain_db,
            "legacy": self.legacy,
        }


@dataclass(frozen=True)
class ObservedSource:
    node: str
    media_class: str = "Audio/Source"
    pipewire_id: str | None = None
    pipewire_object_serial: str | None = None
    device_id: str | None = None
    device_object_serial: str | None = None
    alsa_components: tuple[str, ...] = ()
    usb_vendor_id: str | None = None
    usb_product_id: str | None = None
    usb_product: str | None = None
    usb_serial: str | None = None
    usb_port_path: str | None = None
    usb_instance_generation: str | None = None
    formats: tuple[MicrophoneFormat, ...] = ()
    device_has_playback: bool | None = None
    alsa_card: str | None = None

    def __post_init__(self) -> None:
        if not str(self.node).strip():
            raise ValueError("observed source node cannot be blank")
        object.__setattr__(self, "node", str(self.node).strip())
        object.__setattr__(self, "media_class", str(self.media_class))
        for name in (
            "pipewire_id",
            "pipewire_object_serial",
            "device_id",
            "device_object_serial",
            "usb_product",
            "usb_serial",
            "usb_port_path",
            "usb_instance_generation",
            "alsa_card",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name)))
        object.__setattr__(self, "usb_vendor_id", _optional_usb_id(self.usb_vendor_id))
        object.__setattr__(self, "usb_product_id", _optional_usb_id(self.usb_product_id))
        object.__setattr__(self, "alsa_components", _component_tokens(self.alsa_components))
        object.__setattr__(self, "formats", tuple(sorted(set(self.formats))))

    @property
    def physical_device_key(self) -> str:
        """Snapshot-local physical identity used to distinguish duplicate units."""
        if self.device_id is not None:
            return f"pw-device:{self.device_id}:{self.device_object_serial or ''}"
        if self.usb_instance_generation is not None:
            return f"usb-instance:{self.usb_instance_generation}"
        if self.usb_port_path is not None:
            return f"usb-port:{self.usb_port_path}"
        # Missing device association is not permission to merge unrelated profiles.
        return f"unassociated-source:{self.pipewire_id or self.node}"

    def supports(self, candidate: MicrophoneCandidate) -> bool:
        required = candidate.required_capability
        if required is not None and required not in self.formats:
            return False
        return not (candidate.capture_only and self.device_has_playback is not False)

    def matching_format(self, candidate: MicrophoneCandidate) -> MicrophoneFormat | None:
        required = candidate.required_capability
        if required is not None:
            return required if required in self.formats else None
        return self.formats[0] if self.formats else None

    def identity_dict(self) -> dict[str, Any]:
        return {
            "usb_vendor_id": self.usb_vendor_id,
            "usb_product_id": self.usb_product_id,
            "usb_product": self.usb_product,
            "usb_serial": self.usb_serial,
            "usb_port_path": self.usb_port_path,
            "pipewire_object_serial": self.pipewire_object_serial,
            "device_object_serial": self.device_object_serial,
            "usb_instance_generation": self.usb_instance_generation,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "media_class": self.media_class,
            "pipewire_id": self.pipewire_id,
            "device_id": self.device_id,
            "identity": self.identity_dict(),
            "alsa_components": list(self.alsa_components),
            "formats": [item.as_dict() for item in self.formats],
            "device_has_playback": self.device_has_playback,
            "alsa_card": self.alsa_card,
        }


@dataclass(frozen=True)
class CandidateDiagnostic:
    candidate: MicrophoneCandidate
    priority: int
    state: str
    matched_sources: tuple[ObservedSource, ...]
    reason: str
    liveness: DynamicAvailability | None = None

    @property
    def matched_nodes(self) -> tuple[str, ...]:
        return tuple(sorted(source.node for source in self.matched_sources))

    def as_dict(self) -> dict[str, Any]:
        preferred = tuple(
            source for source in self.matched_sources if source.node == self.candidate.node_name
        )
        representative = (
            preferred[0]
            if len(preferred) == 1
            else self.matched_sources[0] if len(self.matched_sources) == 1 else None
        )
        selected_format = representative.matching_format(self.candidate) if representative else None
        result = {
            "id": self.candidate.id,
            "label": self.candidate.label,
            "priority": self.priority,
            "state": self.state,
            "node": representative.node if representative else None,
            "matched_nodes": list(self.matched_nodes),
            "reason": self.reason,
            "unavailable_reason": (None if self.state in {"selected", "usable"} else self.reason),
            "identity": representative.identity_dict() if representative else None,
            "format": selected_format.as_dict() if selected_format else None,
        }
        if self.liveness is not None:
            result["liveness"] = self.liveness.as_dict()
        return result


@dataclass(frozen=True)
class MicrophoneSelection:
    candidate: MicrophoneCandidate
    source: ObservedSource
    priority: int
    liveness: DynamicAvailability | None = None

    @property
    def node(self) -> str:
        return self.source.node

    @property
    def instance_token(self) -> str:
        # JSON avoids separator ambiguity while retaining every field needed to explain a
        # graph-generation change in an artifact.
        return json.dumps(
            [
                self.candidate.id,
                self.source.node,
                self.source.pipewire_object_serial,
                self.source.device_object_serial,
                self.source.usb_instance_generation,
            ],
            separators=(",", ":"),
        )

    def as_dict(self) -> dict[str, Any]:
        selected_format = self.source.matching_format(self.candidate)
        result = {
            "id": self.candidate.id,
            "label": self.candidate.label,
            "priority": self.priority,
            "node": self.source.node,
            "identity": self.source.identity_dict(),
            "format": selected_format.as_dict() if selected_format else None,
            "instance_token": self.instance_token,
        }
        if self.liveness is not None:
            result["liveness"] = self.liveness.as_dict()
        return result


@dataclass(frozen=True)
class SelectionResult:
    selected: MicrophoneSelection | None
    reason: str
    diagnostics: tuple[CandidateDiagnostic, ...]
    blocked: bool = False

    @property
    def node(self) -> str | None:
        return self.selected.node if self.selected else None

    @property
    def instance_token(self) -> str | None:
        return self.selected.instance_token if self.selected else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.as_dict() if self.selected else None,
            "selection_reason": self.reason,
            "blocked": self.blocked,
            "candidates": [item.as_dict() for item in self.diagnostics],
        }


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be a table")
    return value


def _required_int(table: Mapping[str, Any], key: str, candidate_id: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"microphone {candidate_id!r} {key} must be a positive integer")
    return value


def parse_microphone_candidates(
    document: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    *,
    default_lark_node: str = DEFAULT_LARK_NODE,
    default_lark_component: str = DEFAULT_LARK_COMPONENT,
) -> tuple[MicrophoneCandidate, ...]:
    """Parse ordered candidates, synthesizing one legacy Lark when the array is absent.

    In explicit mode the list is authoritative.  Legacy environment variables can alter
    only the ``lark-a1`` profile/component; its configured hard USB identity remains intact.
    In legacy mode precedence is environment, non-blank ``[devices.lark]``, then defaults.
    """
    env = os.environ if environ is None else environ
    devices = _mapping(document.get("devices") or {}, "devices")
    raw_candidates = devices.get("microphones")
    if raw_candidates is not None:
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise TypeError("devices.microphones must be a non-empty array of tables")
        parsed: list[MicrophoneCandidate] = []
        seen_ids: set[str] = set()
        for priority, raw in enumerate(raw_candidates):
            table = _mapping(raw, f"devices.microphones[{priority}]")
            candidate_id = str(table.get("id", "")).strip()
            if not candidate_id:
                raise ValueError(f"devices.microphones[{priority}].id cannot be blank")
            if candidate_id in seen_ids:
                raise ValueError(f"duplicate microphone candidate id: {candidate_id}")
            seen_ids.add(candidate_id)
            vendor = _optional_usb_id(table.get("usb_vendor_id"))
            product_id = _optional_usb_id(table.get("usb_product_id"))
            if vendor is None or product_id is None:
                raise ValueError(
                    f"microphone {candidate_id!r} requires usb_vendor_id and usb_product_id"
                )
            node_name = _optional_text(table.get("node_name"))
            component = _optional_text(table.get("alsa_component"))
            if candidate_id == "lark-a1":
                node_name = _optional_text(env.get("BRIDGE_LARK")) or node_name
                component = _optional_text(env.get("BRIDGE_LARK_COMPONENT")) or component
            parsed.append(
                MicrophoneCandidate(
                    id=candidate_id,
                    label=str(table.get("label", candidate_id)),
                    node_name=node_name,
                    usb_vendor_id=vendor,
                    usb_product_id=product_id,
                    usb_product=_optional_text(table.get("usb_product")),
                    usb_serial=_optional_text(table.get("usb_serial")),
                    usb_port_path=_optional_text(table.get("usb_port_path")),
                    required_rate=_required_int(table, "required_rate", candidate_id),
                    required_format=str(table.get("required_format", "")),
                    required_channels=_required_int(table, "required_channels", candidate_id),
                    capture_only=table.get("capture_only", True),
                    capture_control=_optional_text(table.get("capture_control")),
                    capture_gain_db=table.get("capture_gain_db"),
                    alsa_component=component,
                    legacy=False,
                )
            )
        return tuple(parsed)

    legacy = _mapping(devices.get("lark") or {}, "devices.lark")
    configured_vendor = _optional_usb_id(legacy.get("usb_vendor_id"))
    configured_product_id = _optional_usb_id(legacy.get("usb_product_id"))
    if (configured_vendor is None) != (configured_product_id is None):
        raise ValueError("devices.lark usb_vendor_id and usb_product_id must be set together")

    component = (
        _optional_text(env.get("BRIDGE_LARK_COMPONENT"))
        or (
            f"USB{configured_vendor.upper()}:{configured_product_id.upper()}"
            if configured_vendor and configured_product_id
            else None
        )
        or default_lark_component
    )
    component_ids = _component_ids(component)
    default_ids = _component_ids(default_lark_component)
    if component_ids is not None:
        ids = component_ids
    elif configured_vendor is not None and configured_product_id is not None:
        ids = (configured_vendor, configured_product_id)
    else:
        ids = default_ids
    if ids is None or ids[0] is None or ids[1] is None:
        raise ValueError("legacy Lark component must contain a USB vendor/product identity")
    return (
        MicrophoneCandidate(
            id="lark-a1",
            label=_optional_text(legacy.get("label")) or "Hollyland Lark A1",
            node_name=(
                _optional_text(env.get("BRIDGE_LARK"))
                or _optional_text(legacy.get("node_name"))
                or default_lark_node
            ),
            usb_vendor_id=ids[0],
            usb_product_id=ids[1],
            usb_product=_optional_text(legacy.get("usb_product")),
            usb_serial=_optional_text(legacy.get("usb_serial")),
            usb_port_path=_optional_text(legacy.get("usb_port_path")),
            required_rate=None,
            required_format=None,
            required_channels=None,
            capture_only=False,
            alsa_component=component,
            legacy=True,
        ),
    )


def _props(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    info = obj.get("info") or {}
    return info.get("props") or {} if isinstance(info, Mapping) else {}


def _first(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _lookup_enrichment(
    values: Mapping[Any, Mapping[str, Any]],
    *,
    device_id: Any,
    device_serial: Any,
    node: str,
) -> Mapping[str, Any]:
    for key in (device_id, str(device_id) if device_id is not None else None, device_serial, node):
        if key is not None and key in values:
            return values[key]
    return {}


def _scalar_values(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _coerce_formats(value: Any) -> tuple[MicrophoneFormat, ...]:
    if value is None:
        return ()
    if isinstance(value, MicrophoneFormat):
        return (value,)
    if isinstance(value, Mapping):
        if "formats" in value and not {"rate", "format", "channels"}.issubset(value):
            return _coerce_formats(value["formats"])
        if {"rate", "format", "channels"}.issubset(value):
            formats = {
                MicrophoneFormat(int(rate), str(audio_format), int(channels))
                for rate, audio_format, channels in product(
                    _scalar_values(value["rate"]),
                    _scalar_values(value["format"]),
                    _scalar_values(value["channels"]),
                )
            }
            return tuple(sorted(formats))
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        found: set[MicrophoneFormat] = set()
        for item in value:
            found.update(_coerce_formats(item))
        return tuple(sorted(found))
    raise TypeError(f"unsupported microphone capability value: {value!r}")


def observations_from_pw_dump(
    objects: Sequence[Mapping[str, Any]],
    *,
    sysfs_by_device: Mapping[Any, Mapping[str, Any]] | None = None,
    capabilities_by_node: Mapping[str, Any] | None = None,
) -> tuple[ObservedSource, ...]:
    """Join Source nodes to Device objects and caller-supplied identity/capability facts.

    ``sysfs_by_device`` may be keyed by PipeWire device global id, device object serial, or
    node name (checked in that order).  The value uses public status field names such as
    ``usb_vendor_id``, ``usb_product_id``, ``usb_port_path`` and
    ``usb_instance_generation``.  This keeps host-specific sysfs traversal out of the pure
    resolver while allowing a caller to provide the nearest USB parent it has already read.
    """
    sysfs = sysfs_by_device or {}
    capabilities = capabilities_by_node or {}
    devices: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Device":
            continue
        object_id = obj.get("id")
        props = _props(obj)
        if object_id is not None:
            devices[str(object_id)] = (obj, props)

    playback_devices: set[str] = set()
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = _props(obj)
        device_id = props.get("device.id")
        if props.get("media.class") == "Audio/Sink" and device_id is not None:
            playback_devices.add(str(device_id))

    found: list[ObservedSource] = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        node_props = _props(obj)
        if node_props.get("media.class") != "Audio/Source":
            continue
        node = _optional_text(node_props.get("node.name"))
        if node is None:
            continue
        raw_device_id = node_props.get("device.id")
        device_id = str(raw_device_id) if raw_device_id is not None else None
        _, device_props = devices.get(device_id or "", ({}, {}))
        device_serial = _first(device_props, ("object.serial", "object.id"))
        enrichment = _lookup_enrichment(
            sysfs,
            device_id=raw_device_id,
            device_serial=device_serial,
            node=node,
        )
        raw_vendor = _first(
            enrichment,
            ("usb_vendor_id", "idVendor", "ID_VENDOR_ID"),
        )
        raw_product_id = _first(
            enrichment,
            ("usb_product_id", "idProduct", "ID_MODEL_ID"),
        )
        try:
            vendor = _optional_usb_id(raw_vendor)
        except ValueError:
            vendor = None
        try:
            product_id = _optional_usb_id(raw_product_id)
        except ValueError:
            product_id = None

        supplied_formats = capabilities.get(node)
        if supplied_formats is None:
            inferred = {
                "rate": _first(node_props, ("audio.rate", "node.rate")),
                "format": node_props.get("audio.format"),
                "channels": node_props.get("audio.channels"),
            }
            supplied_formats = (
                inferred if all(value is not None for value in inferred.values()) else None
            )

        port = _optional_text(_first(enrichment, ("usb_port_path", "sysfs_path")))
        generation = _optional_text(
            _first(enrichment, ("usb_instance_generation", "instance_generation"))
        )
        devnum = _optional_text(_first(enrichment, ("devnum", "device_number")))
        if generation is None and port is not None and devnum is not None:
            generation = f"{port}@{devnum}"

        found.append(
            ObservedSource(
                node=node,
                media_class=str(node_props.get("media.class")),
                pipewire_id=_optional_text(obj.get("id")),
                pipewire_object_serial=_optional_text(node_props.get("object.serial")),
                device_id=device_id,
                device_object_serial=_optional_text(device_serial),
                alsa_components=_component_tokens(node_props.get("alsa.components")),
                usb_vendor_id=vendor,
                usb_product_id=product_id,
                usb_product=_optional_text(_first(enrichment, ("usb_product", "product"))),
                usb_serial=_optional_text(_first(enrichment, ("usb_serial", "serial"))),
                usb_port_path=port,
                usb_instance_generation=generation,
                formats=_coerce_formats(supplied_formats),
                device_has_playback=(
                    str(raw_device_id) in playback_devices if raw_device_id is not None else None
                ),
                alsa_card=_optional_text(
                    _first(device_props, ("api.alsa.card", "alsa.card"))
                    or _first(node_props, ("api.alsa.pcm.card", "alsa.card"))
                ),
            )
        )
    return tuple(sorted(found, key=lambda source: (source.node, source.pipewire_id or "")))


def observations_from_node_map(
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    identities_by_node: Mapping[str, Mapping[str, Any]] | None = None,
    capabilities_by_node: Mapping[str, Any] | None = None,
) -> tuple[ObservedSource, ...]:
    """Compatibility adapter for callers that currently retain only pw-dump node props."""
    identities = identities_by_node or {}
    capabilities = capabilities_by_node or {}
    playback_devices = {
        str(props.get("device.id"))
        for props in nodes.values()
        if props.get("media.class") == "Audio/Sink" and props.get("device.id") is not None
    }
    found: list[ObservedSource] = []
    for node, props in nodes.items():
        if props.get("media.class") != "Audio/Source":
            continue
        identity = identities.get(node) or {}
        raw_vendor = _first(identity, ("usb_vendor_id", "idVendor"))
        raw_product_id = _first(identity, ("usb_product_id", "idProduct"))
        device_id = _optional_text(props.get("device.id"))
        supplied_playback = identity.get("device_has_playback")
        device_has_playback = (
            supplied_playback
            if isinstance(supplied_playback, bool)
            else (device_id in playback_devices if device_id is not None else None)
        )
        found.append(
            ObservedSource(
                node=node,
                media_class="Audio/Source",
                pipewire_id=_optional_text(props.get("global.id")) or node,
                pipewire_object_serial=_optional_text(props.get("object.serial")),
                device_id=device_id,
                device_object_serial=_optional_text(identity.get("device_object_serial")),
                alsa_components=_component_tokens(props.get("alsa.components")),
                usb_vendor_id=_optional_usb_id(raw_vendor),
                usb_product_id=_optional_usb_id(raw_product_id),
                usb_product=_optional_text(_first(identity, ("usb_product", "product"))),
                usb_serial=_optional_text(_first(identity, ("usb_serial", "serial"))),
                usb_port_path=_optional_text(_first(identity, ("usb_port_path", "sysfs_path"))),
                usb_instance_generation=_optional_text(identity.get("usb_instance_generation")),
                formats=_coerce_formats(capabilities.get(node)),
                device_has_playback=device_has_playback,
                alsa_card=_optional_text(
                    _first(identity, ("alsa_card", "api.alsa.card"))
                    or _first(props, ("api.alsa.pcm.card", "alsa.card"))
                ),
            )
        )
    return tuple(sorted(found, key=lambda source: (source.node, source.pipewire_id or "")))


def _text_matches(expected: str | None, observed: str | None) -> bool:
    return expected is None or (
        observed is not None and expected.strip().casefold() == observed.strip().casefold()
    )


def _identity_matches(candidate: MicrophoneCandidate, source: ObservedSource) -> bool:
    if source.media_class != "Audio/Source":
        return False
    exact_legacy = candidate.legacy and candidate.node_name == source.node
    component_match = (candidate.alsa_component or "").upper() in source.alsa_components
    if candidate.legacy:
        if not (exact_legacy or component_match):
            return False
    elif not (
        source.usb_vendor_id == candidate.usb_vendor_id
        and source.usb_product_id == candidate.usb_product_id
        and component_match
    ):
        return False
    return (
        _text_matches(candidate.usb_product, source.usb_product)
        and _text_matches(candidate.usb_serial, source.usb_serial)
        and _text_matches(candidate.usb_port_path, source.usb_port_path)
    )


def _constraints_overlap(left: MicrophoneCandidate, right: MicrophoneCandidate) -> bool:
    if (left.usb_vendor_id, left.usb_product_id) != (
        right.usb_vendor_id,
        right.usb_product_id,
    ):
        return False
    for field_name in ("usb_product", "usb_serial", "usb_port_path"):
        left_value = getattr(left, field_name)
        right_value = getattr(right, field_name)
        if left_value and right_value and left_value.casefold() != right_value.casefold():
            return False
    return True


def _choose_source(
    candidate: MicrophoneCandidate,
    sources: Sequence[ObservedSource],
) -> tuple[str, tuple[ObservedSource, ...], str, ObservedSource | None]:
    identity_matches = tuple(source for source in sources if _identity_matches(candidate, source))
    if not identity_matches:
        return "absent", (), "no identity-qualified source is present", None
    capable = tuple(source for source in identity_matches if source.supports(candidate))
    if not capable:
        return (
            "capability_mismatch",
            identity_matches,
            "identity matched, but required capture capabilities are unavailable",
            None,
        )
    physical = {source.physical_device_key for source in capable}
    if len(physical) > 1:
        return (
            "ambiguous",
            capable,
            f"{len(physical)} physical devices match; configure usb_serial or usb_port_path",
            None,
        )
    preferred = tuple(source for source in capable if source.node == candidate.node_name)
    selectable = preferred or capable
    if len(selectable) != 1:
        return (
            "ambiguous",
            capable,
            "one physical device exposes multiple usable profiles and node_name does not select one",
            None,
        )
    return "usable", identity_matches, "identity and capabilities match", selectable[0]


def resolve(
    candidates: Sequence[MicrophoneCandidate],
    observations: Sequence[ObservedSource],
    dynamic_availability: Mapping[str, DynamicAvailability] | None = None,
) -> SelectionResult:
    """Resolve the highest-priority safe microphone without relying on enumeration order."""
    if not candidates:
        return SelectionResult(None, "no microphone candidates are configured", (), blocked=False)
    ids = [candidate.id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("microphone candidate ids must be unique")

    conflicts: dict[int, list[str]] = {}
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            if _constraints_overlap(left, right):
                conflicts.setdefault(left_index, []).append(right.id)
                conflicts.setdefault(right_index, []).append(left.id)

    diagnostics: list[CandidateDiagnostic] = []
    choices: dict[int, ObservedSource] = {}
    for priority, candidate in enumerate(candidates):
        if priority in conflicts:
            matched = tuple(
                source for source in observations if _identity_matches(candidate, source)
            )
            peers = ", ".join(conflicts[priority])
            diagnostics.append(
                CandidateDiagnostic(
                    candidate,
                    priority,
                    "conflict",
                    matched,
                    f"configuration overlaps candidate(s): {peers}",
                )
            )
            continue
        state, matched, reason, choice = _choose_source(candidate, observations)
        liveness: DynamicAvailability | None = None
        if choice is not None and dynamic_availability is not None:
            supplied = dynamic_availability.get(candidate.id)
            if supplied is not None:
                provisional = MicrophoneSelection(candidate, choice, priority)
                if supplied.candidate_id != candidate.id:
                    liveness = DynamicAvailability(
                        candidate.id,
                        supplied.instance_token,
                        "unknown",
                        "dynamic availability belongs to a different microphone candidate",
                    )
                elif supplied.instance_token != provisional.instance_token:
                    liveness = DynamicAvailability(
                        candidate.id,
                        supplied.instance_token,
                        "unknown",
                        "dynamic availability is stale for the current microphone instance",
                    )
                else:
                    liveness = supplied
                if liveness.state != "active":
                    state = "capability_mismatch"
                    reason = liveness.reason
                    choice = None
        diagnostics.append(
            CandidateDiagnostic(candidate, priority, state, matched, reason, liveness)
        )
        if choice is not None:
            choices[priority] = choice

    for priority, diagnostic in enumerate(diagnostics):
        if diagnostic.state in {"ambiguous", "conflict"}:
            return SelectionResult(
                None,
                f"{diagnostic.candidate.id} {diagnostic.state}: {diagnostic.reason}",
                tuple(diagnostics),
                blocked=True,
            )
        if diagnostic.state != "usable":
            continue
        selection = MicrophoneSelection(
            diagnostic.candidate,
            choices[priority],
            priority,
            diagnostic.liveness,
        )
        diagnostics[priority] = replace(
            diagnostic,
            state="selected",
            reason="highest-priority usable microphone",
        )
        if priority == 0:
            reason = f"using {diagnostic.candidate.id}"
        else:
            prior = "; ".join(
                (
                    f"{item.candidate.id} {item.state}: {item.reason}"
                    if item.liveness is not None
                    else f"{item.candidate.id} {item.state}"
                )
                for item in diagnostics[:priority]
            )
            reason = f"{prior}; using {diagnostic.candidate.id}"
        return SelectionResult(selection, reason, tuple(diagnostics), blocked=False)

    summary = ", ".join(
        (
            f"{item.candidate.id} {item.state}: {item.reason}"
            if item.liveness is not None
            else f"{item.candidate.id} {item.state}"
        )
        for item in diagnostics
    )
    return SelectionResult(
        None,
        f"no configured microphone is usable: {summary}",
        tuple(diagnostics),
        blocked=False,
    )


__all__ = [
    "DEFAULT_LARK_COMPONENT",
    "DEFAULT_LARK_NODE",
    "CandidateDiagnostic",
    "DynamicAvailability",
    "MicrophoneCandidate",
    "MicrophoneFormat",
    "MicrophoneSelection",
    "ObservedSource",
    "SelectionResult",
    "normalize_audio_format",
    "normalize_usb_id",
    "observations_from_node_map",
    "observations_from_pw_dump",
    "parse_microphone_candidates",
    "resolve",
]
