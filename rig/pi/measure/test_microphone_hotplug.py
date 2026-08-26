from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from rig.pi.measure import microphone_hotplug as hotplug

HFP_SINK = "bluez_output.phone.headset-head-unit"
LARK = "alsa_input.usb-LARK.analog-stereo"
FIFINE = "alsa_input.usb-FIFINE.mono-fallback"


def usb_hub(generation: str) -> dict:
    port, raw_devnum = generation.rsplit("@", 1)
    return {
        "usb_device_class": "09",
        "usb_vendor_id": "0bda",
        "usb_product_id": "5411",
        "usb_product": "USB2.1 Hub",
        "usb_port_path": port,
        "usb_devnum": int(raw_devnum),
        "usb_instance_generation": generation,
    }


def usb_device(
    candidate_id: str,
    generation: str,
    *,
    hubs: tuple[str, ...] = (),
) -> dict:
    port, raw_devnum = generation.rsplit("@", 1)
    vendor_id, product_id = hotplug.USB_MICROPHONE_FINGERPRINTS[candidate_id]
    return {
        "id": candidate_id,
        "usb_vendor_id": vendor_id,
        "usb_product_id": product_id,
        "usb_product": (
            "Wireless Microphone"
            if candidate_id == "lark-a1"
            else "USB PnP Audio Device"
        ),
        "usb_serial": None,
        "usb_port_path": port,
        "usb_devnum": int(raw_devnum),
        "usb_instance_generation": generation,
        "usb_hub_ancestors": [usb_hub(value) for value in hubs],
    }


def usb_topology(
    *,
    lark: tuple[str, ...] = (),
    fifine: tuple[str, ...] = ("1-1.2@4",),
    lark_hubs: tuple[str, ...] = (),
    fifine_hubs: tuple[str, ...] = (),
) -> dict[str, list[dict]]:
    return {
        "lark-a1": [usb_device("lark-a1", value, hubs=lark_hubs) for value in lark],
        "fifine-k054": [
            usb_device("fifine-k054", value, hubs=fifine_hubs) for value in fifine
        ],
    }


def usb_baseline(topology: dict[str, list[dict]]) -> dict:
    microphone = selected_for_topology(topology)
    samples = [
        {
            "seq": seq,
            "remote_seq": None,
            "capture_started_monotonic": source_time,
            "usb_microphones": topology,
            "usb_error": None,
            "microphone": microphone,
            "sampling": {"start_gap_s": 0.15},
        }
        for seq, source_time in ((0, 98.85), (1, 99.0))
    ]
    return hotplug.stable_usb_baseline_from_samples(samples)


def selected_for_topology(topology: dict[str, list[dict]]) -> dict | None:
    candidate_id = (
        "lark-a1"
        if len(topology["lark-a1"]) == 1
        else ("fifine-k054" if len(topology["fifine-k054"]) == 1 else None)
    )
    if candidate_id is None:
        return None
    selected = json.loads(
        json.dumps(active_status(selected_id=candidate_id)["microphone"]["selected"])
    )
    raw = topology[candidate_id][0]
    selected["identity"] = {key: raw.get(key) for key in hotplug.USB_IDENTITY_FIELDS}
    return selected


def direct_usb_sample(
    seq: int,
    topology: dict[str, list[dict]],
    *,
    source_monotonic: float | None = None,
    gap: float = 0.15,
) -> dict:
    return {
        "seq": seq,
        "capture_started_monotonic": (
            float(seq) * 0.15 if source_monotonic is None else source_monotonic
        ),
        "usb_microphones": topology,
        "usb_error": None,
        "microphone": selected_for_topology(topology),
        "sampling": {"start_gap_s": gap},
    }


@contextlib.contextmanager
def feed_local_action_baseline(
    sampler: hotplug.LiveSampler,
    topologies: list[dict[str, list[dict]]],
    *,
    gaps: list[float] | None = None,
):
    original = sampler._action_usb_baseline_locked
    producers: list[threading.Thread] = []

    def wrapped(phase: str, after_seq: int) -> dict:
        def produce() -> None:
            with sampler._condition:
                for offset, topology in enumerate(topologies, start=1):
                    seq = after_seq + offset
                    sampler._seq = seq
                    sampler._recent.append(
                        direct_usb_sample(
                            seq,
                            topology,
                            source_monotonic=100.0 + offset * 0.15,
                            gap=(gaps or [0.15] * len(topologies))[offset - 1],
                        )
                    )
                sampler._condition.notify_all()

        producer = threading.Thread(target=produce)
        producers.append(producer)
        producer.start()
        return original(phase, after_seq)

    with mock.patch.object(sampler, "_action_usb_baseline_locked", side_effect=wrapped):
        yield
    for producer in producers:
        producer.join(2)


def active_status(*, selected_id: str = "fifine-k054") -> dict:
    selected_node = FIFINE if selected_id == "fifine-k054" else LARK
    is_fifine = selected_id == "fifine-k054"
    return {
        "timestamp": 100.0,
        "state": "ACTIVE",
        "generation": 7,
        "call": {"hfp_nodes_present": True},
        "endpoints": {
            "microphone": selected_node,
            "lark": LARK,
            "hfp_source": "bluez_input.phone.headset-head-unit",
            "hfp_sink": HFP_SINK,
        },
        "microphone": {
            "selected": {
                "id": selected_id,
                "node": selected_node,
                "instance_token": f"{selected_id}:instance-7",
                "identity": {
                    "usb_vendor_id": "0c76" if is_fifine else "3547",
                    "usb_product_id": "161e" if is_fifine else "0407",
                    "usb_product": (
                        "USB PnP Audio Device" if is_fifine else "Wireless Microphone"
                    ),
                    "usb_serial": None,
                    "usb_port_path": "1-1.2" if is_fifine else "1-1.3",
                    "usb_instance_generation": ("1-1.2@4" if is_fifine else "1-1.3@9"),
                },
                "format": {
                    "rate": 48000,
                    "format": "S16LE",
                    "channels": 1 if selected_id == "fifine-k054" else 2,
                },
            },
            "selection_reason": f"using {selected_id}",
            "candidates": [
                {
                    "id": "lark-a1",
                    "state": "selected" if selected_id == "lark-a1" else "usable",
                    "node": LARK,
                    "matched_nodes": [LARK],
                },
                {
                    "id": "fifine-k054",
                    "state": ("selected" if selected_id == "fifine-k054" else "usable"),
                    "node": FIFINE,
                    "matched_nodes": [FIFINE],
                },
            ],
        },
        "aec": {"enabled": True, "verified": True, "owner_pid": 1234},
    }


def active_links(selected_node: str = FIFINE) -> list[tuple[str, str]]:
    return [
        (selected_node, hotplug.AEC_CAPTURE),
        (hotplug.AEC_SOURCE, hotplug.MICROPHONE_INPUT),
        (hotplug.MICROPHONE_OUTPUT, HFP_SINK),
    ]


def sample_for(status: dict, links: list[tuple[str, str]] | None = None) -> dict:
    observed_links = active_links() if links is None else links
    return {
        "state": status.get("state"),
        "status_error": None,
        "link_error": None,
        "microphone": hotplug.selected_microphone(status),
        "candidates": hotplug.candidate_inventory(status),
        "graph_generation": status.get("generation"),
        "aec": status.get("aec") or {},
        "invariants": hotplug.evaluate_link_invariants(
            status, None, observed_links, now=100.0
        ),
    }


def valid_timing_transition(
    kind: str,
    *,
    outcome: str = "completed",
    connection_layout: str | None = None,
    cycle: int | None = None,
    direct_external_hub: bool = False,
) -> dict:
    powered_layout = connection_layout == hotplug.CONNECTION_LAYOUT_POWERED_HUB
    if powered_layout:
        hub_generations = ("1-1.2.1@8", "1-1.2@7", "1-1@2")
        fifine_generation = "1-1.2.1.3@17"
        lark_generation = "1-1.2.1.1@16"
        restore_fifine_generation = fifine_generation
    elif direct_external_hub:
        hub_generations = ("1-1.4@6", "1-1@2")
        fifine_generation = "1-1.4.3@17"
        lark_generation = "1-1.4.1@16"
        restore_fifine_generation = fifine_generation
    else:
        hub_generations = ("1-1@2",)
        fifine_generation = "1-1.2@4"
        lark_generation = "1-1.3@9"
        restore_fifine_generation = "1-1.2@12"
    fifine_only = usb_topology(fifine=(fifine_generation,), fifine_hubs=hub_generations)
    both = usb_topology(
        lark=(lark_generation,),
        fifine=(fifine_generation,),
        lark_hubs=hub_generations,
        fifine_hubs=hub_generations,
    )
    before, after, selected_id = {
        "promotion": (fifine_only, both, "lark-a1"),
        "fallback": (both, fifine_only, "fifine-k054"),
        "fifine_replug": (
            usb_topology(fifine=()),
            usb_topology(
                fifine=(restore_fifine_generation,),
                fifine_hubs=hub_generations,
            ),
            "fifine-k054",
        ),
    }[kind]
    selected = active_status(selected_id=selected_id)["microphone"]["selected"]
    target_id = hotplug.USB_GATE_TARGET[kind][0]
    target_devices = after[target_id]
    selected_devices = after[selected_id]
    if selected_devices:
        selected = json.loads(json.dumps(selected))
        selected["identity"] = {
            key: selected_devices[0].get(key) for key in hotplug.USB_IDENTITY_FIELDS
        }
    first = {
        "seq": 2,
        "remote_seq": None,
        "capture_started_monotonic": 100.0,
        "usb_microphones": after,
        "usb_error": None,
        "microphone": None,
        "sampling": {"start_gap_s": 0.15},
    }
    confirmation = {
        **first,
        "seq": 3,
        "capture_started_monotonic": 100.15,
        "microphone": selected,
    }
    persistence = [first, confirmation]
    if outcome == "completed":
        for seq in range(4, 8):
            persistence.append(
                {
                    **confirmation,
                    "seq": seq,
                    "capture_started_monotonic": 100.0 + (seq - 2) * 0.15,
                }
            )
    final = persistence[-1]
    baseline = usb_baseline(before)
    if kind == "fallback":
        raw_device = before[target_id][0]
        binding = hotplug._identity_binding_evidence(
            baseline["microphone"], raw_device, target_id, "preaction"
        )
    elif outcome == "completed":
        binding = hotplug._identity_binding_evidence(
            selected, target_devices[0], target_id, "final_selected"
        )
    else:
        binding = None
    final_candidate_id = hotplug.USB_FINAL_SELECTED_CANDIDATE[kind]
    final_binding = (
        hotplug._identity_binding_evidence(
            selected,
            after[final_candidate_id][0],
            final_candidate_id,
            "final_selected",
        )
        if outcome == "completed"
        else None
    )
    settled_latency = (
        final["capture_started_monotonic"] - first["capture_started_monotonic"]
    )
    state_settle = (
        final["capture_started_monotonic"] - confirmation["capture_started_monotonic"]
    )
    result = {
        "phase": {
            "promotion": "lark_promotion",
            "fallback": "lark_fallback",
            "fifine_replug": "fifine_replug/restore",
        }[kind],
        "cycle": cycle,
        "connection_layout": connection_layout,
        "gate_kind": kind,
        "outcome": outcome,
        "timing_evidence_version": hotplug.TIMING_EVIDENCE_VERSION,
        "timing_origin": hotplug.USB_TIMING_ORIGIN,
        "transition_latency_s": 0.15 if outcome == "completed" else None,
        "settled_latency_s": settled_latency if outcome == "completed" else None,
        "settle_requirement_s": hotplug.QUALIFICATION_MIN_SETTLE_SECONDS,
        "state_settle_s": state_settle if outcome == "completed" else None,
        "safe_state_latency_s": settled_latency if outcome == "safe_state" else None,
        "usb_baseline": baseline,
        "usb_event": {
            **hotplug._usb_event_evidence(first, kind),
            "confirmed": True,
            "confirmation": hotplug._usb_event_evidence(confirmation, kind),
            "persistent": True,
            "stable_through_seq": final["seq"],
            "persistence_samples": [
                hotplug._event_sample_structure(sample, kind) for sample in persistence
            ],
        },
        "usb_identity_binding": binding,
        "usb_final_identity_binding": final_binding,
        "first_matching_sample": confirmation if outcome == "completed" else None,
        "final_sample": final,
        "safety_clean": True,
        "restart_clean": True,
    }
    return result


def timing_transitions(kind: str) -> list[dict]:
    transitions = [
        valid_timing_transition(kind)
        for _ in range(hotplug.QUALIFICATION_REQUIRED_FAST)
    ]
    transitions.append(valid_timing_transition(kind, outcome="safe_state"))
    return transitions


def valid_layout_handoff(*, direct_external_hub: bool = False) -> dict:
    direct_device = (
        usb_device(
            "fifine-k054",
            "1-1.4.3@17",
            hubs=("1-1.4@6", "1-1@2"),
        )
        if direct_external_hub
        else usb_device("fifine-k054", "1-1.2@4", hubs=("1-1@2",))
    )
    powered_device = usb_device(
        "fifine-k054",
        "1-1.2.1.3@17",
        hubs=("1-1.2.1@8", "1-1.2@7", "1-1@2"),
    )
    return {
        "phase": hotplug.CONNECTION_LAYOUT_HANDOFF_PHASE,
        "cycle": hotplug.CONNECTION_LAYOUT_BOUNDARY_CYCLE,
        "connection_layout": "direct_to_powered_hub",
        "gate_kind": None,
        "outcome": "completed",
        "layout_handoff_validated": True,
        "operator_attestation": {
            "observation_kind": "operator_attestation",
            "claim": "external_hub_power_supply_connected",
            "operator_acknowledged": True,
        },
        "observed_usb_ancestry": {
            "validated": True,
            "observation_kind": "usb_sysfs_ancestry_delta",
            "direct_usb_device": direct_device,
            "powered_hub_usb_device": powered_device,
            "added_hub_ancestor_generations": ["1-1.2.1@8", "1-1.2@7"],
            "direct_instance_token": "fifine:direct",
            "powered_hub_instance_token": "fifine:hub",
            "direct_graph_generation": 20,
            "powered_hub_graph_generation": 21,
            "lark_absent": True,
            "settled_sample_count": 5,
        },
        "safety_clean": True,
        "restart_clean": True,
    }


def split_layout_transitions(
    campaign: str, *, direct_external_hub: bool = False
) -> list[dict]:
    kinds = (
        ("promotion", "fallback")
        if campaign == "promotion-fallback"
        else ("fifine_replug",)
    )
    transitions: list[dict] = []
    for cycle in range(1, 21):
        if cycle == 11:
            transitions.append(
                valid_layout_handoff(direct_external_hub=direct_external_hub)
            )
        layout = hotplug.connection_layout_for_cycle(
            cycle, hotplug.CONNECTION_PLAN_DIRECT10_HUB10
        )
        transitions.extend(
            valid_timing_transition(
                kind,
                connection_layout=layout,
                cycle=cycle,
                direct_external_hub=direct_external_hub and cycle <= 10,
            )
            for kind in kinds
        )
    return transitions


def summary_sampler(*, max_gap: float = 0.24) -> hotplug.LiveSampler:
    sampler = hotplug.LiveSampler(Path("status.json"), Path("timeline.jsonl"), 0.20)
    sampler.initial_service_counts = {unit: 0 for unit in hotplug.SERVICE_UNITS}
    sampler.final_service_counts = {unit: 0 for unit in hotplug.SERVICE_UNITS}
    sampler.total_samples = 1
    sampler.max_sample_gap = max_gap
    return sampler


class MicrophoneHotplugTests(unittest.TestCase):
    def test_v2_baseline_rejects_stale_unstable_and_reversed_samples(self) -> None:
        base = usb_topology()
        cases = {
            "stale": [
                direct_usb_sample(1, base, source_monotonic=10.0),
                direct_usb_sample(2, base, source_monotonic=10.15, gap=0.251),
            ],
            "unstable": [
                direct_usb_sample(1, base, source_monotonic=10.0),
                direct_usb_sample(
                    2,
                    usb_topology(lark=("1-1.3@9",)),
                    source_monotonic=10.15,
                ),
            ],
            "reversed": [
                direct_usb_sample(1, base, source_monotonic=10.15),
                direct_usb_sample(2, base, source_monotonic=10.0),
            ],
        }
        for label, samples in cases.items():
            with self.subTest(label=label), self.assertRaises(hotplug.CampaignAbort):
                hotplug.stable_usb_baseline_from_samples(samples)

    def test_latched_usb_edge_rejects_bounce_duplicate_and_generation_change(
        self,
    ) -> None:
        cases = (
            (
                "promotion bounce",
                "promotion",
                usb_topology(lark=("1-1.3@9",)),
                usb_topology(),
            ),
            (
                "promotion generation",
                "promotion",
                usb_topology(lark=("1-1.3@9",)),
                usb_topology(lark=("1-1.3@10",)),
            ),
            (
                "promotion duplicate",
                "promotion",
                usb_topology(lark=("1-1.3@9",)),
                usb_topology(lark=("1-1.3@9", "1-1.4@10")),
            ),
            (
                "fallback reappearance",
                "fallback",
                usb_topology(),
                usb_topology(lark=("1-1.3@10",)),
            ),
            (
                "replug generation",
                "fifine_replug",
                usb_topology(fifine=("1-1.2@12",)),
                usb_topology(fifine=("1-1.2@13",)),
            ),
        )
        for label, gate_kind, first_topology, later_topology in cases:
            with self.subTest(label=label):
                first = direct_usb_sample(1, first_topology, source_monotonic=10.0)
                later = direct_usb_sample(2, later_topology, source_monotonic=10.15)
                error = hotplug._latched_usb_topology_error(
                    first, first, later, gate_kind
                )
                self.assertIsNotNone(error)

    def test_full_runtime_usb_identity_binding_rejects_mismatches(self) -> None:
        raw = usb_device("fifine-k054", "1-1.2@12")
        selected = selected_for_topology(usb_topology(fifine=("1-1.2@12",)))
        assert selected is not None
        self.assertIsNone(
            hotplug.usb_identity_binding_error(selected, raw, "fifine-k054")
        )
        for key in (
            "usb_product",
            "usb_serial",
            "usb_port_path",
            "usb_instance_generation",
        ):
            with self.subTest(key=key):
                changed = json.loads(json.dumps(selected))
                changed["identity"][key] = "wrong"
                self.assertIn(
                    key,
                    hotplug.usb_identity_binding_error(changed, raw, "fifine-k054")
                    or "",
                )

    def test_fallback_timing_binds_final_fifine_to_persistent_raw_usb(self) -> None:
        transition = valid_timing_transition("fallback")
        preaction_binding = json.loads(json.dumps(transition["usb_identity_binding"]))
        transition["final_sample"]["microphone"]["identity"][
            "usb_instance_generation"
        ] = "9-9.9@999"

        gate = hotplug.summarize_timing_gate(
            [transition], kind="fallback", expected_cycles=1
        )

        self.assertEqual(transition["usb_identity_binding"], preaction_binding)
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertEqual(gate["structurally_valid_usb_evidence_cycles"], 0)

    def test_timing_gate_requires_canonical_observed_state_settle(self) -> None:
        valid = valid_timing_transition("promotion")
        self.assertIsNone(hotplug._timing_evidence_error(valid, "promotion"))
        self.assertGreaterEqual(
            valid["state_settle_s"] + 1e-6,
            hotplug.QUALIFICATION_MIN_SETTLE_SECONDS,
        )

        configured_zero = valid_timing_transition("promotion")
        configured_zero["settle_requirement_s"] = 0.0
        self.assertIn(
            "immutable qualification settle requirement",
            hotplug._timing_evidence_error(configured_zero, "promotion") or "",
        )

        observed_zero = valid_timing_transition("promotion")
        first_match = observed_zero["first_matching_sample"]
        observed_zero["final_sample"] = first_match
        observed_zero["settled_latency_s"] = observed_zero["transition_latency_s"]
        observed_zero["state_settle_s"] = 0.0
        observed_zero["usb_event"]["persistence_samples"] = observed_zero["usb_event"][
            "persistence_samples"
        ][:2]
        observed_zero["usb_event"]["stable_through_seq"] = first_match["seq"]
        self.assertIn(
            "remain stable",
            hotplug._timing_evidence_error(observed_zero, "promotion") or "",
        )

    def test_safe_state_timing_is_structurally_bounded_by_usb_deadline(self) -> None:
        def safe_after(sample_count: int) -> dict:
            transition = valid_timing_transition("promotion", outcome="safe_state")
            first = transition["usb_event"]["persistence_samples"][0]
            samples = [first]
            for index in range(1, sample_count):
                sample = json.loads(json.dumps(first))
                sample["seq"] = first["seq"] + index
                sample["capture_started_monotonic"] = (
                    first["capture_started_monotonic"] + index * 0.15
                )
                samples.append(sample)
            transition["usb_event"]["persistence_samples"] = samples
            transition["usb_event"]["confirmation"] = hotplug._usb_event_evidence(
                samples[1], "promotion"
            )
            transition["usb_event"]["stable_through_seq"] = samples[-1]["seq"]
            transition["final_sample"] = json.loads(json.dumps(samples[-1]))
            transition["safe_state_latency_s"] = (
                samples[-1]["capture_started_monotonic"]
                - first["capture_started_monotonic"]
            )
            return transition

        at_deadline = safe_after(401)
        after_deadline = safe_after(402)

        self.assertIsNone(hotplug._timing_evidence_error(at_deadline, "promotion"))
        self.assertIn(
            "after the 60-second deadline",
            hotplug._timing_evidence_error(after_deadline, "promotion") or "",
        )

        repeated = [valid_timing_transition("promotion") for _ in range(19)]
        repeated.append(after_deadline)
        gate = hotplug.summarize_timing_gate(
            repeated, kind="promotion", expected_cycles=20
        )
        self.assertEqual(gate["verdict"], "FAIL")

    def test_timing_gate_revalidates_v2_evidence_structure(self) -> None:
        mutations = {
            "version": lambda item: item.update(timing_evidence_version=1),
            "baseline": lambda item: item["usb_baseline"].update(stable=False),
            "confirmation": lambda item: item["usb_event"].update(confirmed=False),
            "persistence": lambda item: item["usb_event"].update(persistent=False),
            "binding": lambda item: item["final_sample"]["microphone"][
                "identity"
            ].update(usb_product="wrong"),
            "source_time": lambda item: item["final_sample"].update(
                capture_started_monotonic=float("nan")
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                transition = valid_timing_transition("promotion")
                mutate(transition)
                gate = hotplug.summarize_timing_gate(
                    [transition], kind="promotion", expected_cycles=1
                )
                self.assertEqual(gate["verdict"], "FAIL")

    def test_local_mark_action_rejects_missing_fresh_stream(self) -> None:
        sampler = hotplug.LiveSampler(Path("status.json"), Path("timeline.jsonl"), 0.15)
        sampler._started_monotonic = time.monotonic()
        with (
            mock.patch.object(
                sampler,
                "_query_service_counts",
                return_value=({unit: 0 for unit in hotplug.SERVICE_UNITS}, None),
            ),
            mock.patch.object(
                hotplug, "USB_BASELINE_OBSERVATION_TIMEOUT_SECONDS", 0.001
            ),
            self.assertRaisesRegex(hotplug.CampaignAbort, "timed out"),
        ):
            sampler.mark_action("lark_promotion", 1, "plug")

    def test_usb_sysfs_inventory_records_port_devnum_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "1-1.3"
            device.mkdir()
            (device / "idVendor").write_text("3547\n", encoding="ascii")
            (device / "idProduct").write_text("0407\n", encoding="ascii")
            (device / "devnum").write_text("9\n", encoding="ascii")
            (device / "product").write_text("Wireless Microphone\n", encoding="utf-8")

            topology, error = hotplug.read_usb_microphones(root)

        self.assertIsNone(error)
        self.assertEqual(topology["fifine-k054"], [])
        self.assertEqual(topology["lark-a1"][0]["usb_instance_generation"], "1-1.3@9")
        self.assertEqual(topology["lark-a1"][0]["usb_devnum"], 9)

    def test_usb_sysfs_inventory_records_nested_hub_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, devnum in (("1-1.2", 7), ("1-1.2.1", 8)):
                hub = root / name
                hub.mkdir()
                (hub / "bDeviceClass").write_text("09\n", encoding="ascii")
                (hub / "idVendor").write_text("0bda\n", encoding="ascii")
                (hub / "idProduct").write_text("5411\n", encoding="ascii")
                (hub / "devnum").write_text(f"{devnum}\n", encoding="ascii")
                (hub / "product").write_text("USB2.1 Hub\n", encoding="utf-8")
            device = root / "1-1.2.1.3"
            device.mkdir()
            (device / "idVendor").write_text("0c76\n", encoding="ascii")
            (device / "idProduct").write_text("161e\n", encoding="ascii")
            (device / "devnum").write_text("17\n", encoding="ascii")
            (device / "product").write_text("USB PnP Audio Device\n", encoding="utf-8")

            topology, error = hotplug.read_usb_microphones(root)

        self.assertIsNone(error)
        self.assertEqual(
            [
                item["usb_instance_generation"]
                for item in topology["fifine-k054"][0]["usb_hub_ancestors"]
            ],
            ["1-1.2.1@8", "1-1.2@7"],
        )
        self.assertEqual(
            topology["fifine-k054"][0]["usb_hub_ancestors"][0]["usb_product"],
            "USB2.1 Hub",
        )

    def test_hub_ancestry_must_be_a_nearest_first_parent_route(self) -> None:
        device = usb_device(
            "fifine-k054",
            "1-1.2.1.3@17",
            hubs=("1-1.2.1@8", "1-1.2@7", "1-1@2"),
        )
        ancestors, error = hotplug._validated_hub_ancestors(device)
        self.assertIsNone(error)
        self.assertIsNotNone(ancestors)

        unrelated = json.loads(json.dumps(device))
        unrelated["usb_hub_ancestors"][0]["usb_port_path"] = "8-8"
        unrelated["usb_hub_ancestors"][0]["usb_instance_generation"] = "8-8@8"
        _ancestors, error = hotplug._validated_hub_ancestors(unrelated)
        self.assertIn("non-parent", str(error))

        reversed_order = json.loads(json.dumps(device))
        reversed_order["usb_hub_ancestors"].reverse()
        _ancestors, error = hotplug._validated_hub_ancestors(reversed_order)
        self.assertIn("nearest-first", str(error))

        incomplete = json.loads(json.dumps(device))
        incomplete["usb_hub_ancestors"].pop()
        _ancestors, error = hotplug._validated_hub_ancestors(incomplete)
        self.assertIn("incomplete", str(error))

    def test_each_gated_kind_anchors_to_its_expected_usb_edge(self) -> None:
        cases = (
            (
                "promotion",
                usb_topology(),
                usb_topology(lark=("1-1.3@9",)),
            ),
            (
                "fallback",
                usb_topology(lark=("1-1.3@9",)),
                usb_topology(),
            ),
            (
                "fifine_replug",
                usb_topology(fifine=()),
                usb_topology(fifine=("1-1.2@12",)),
            ),
        )
        for gate_kind, before, after in cases:
            with self.subTest(gate_kind=gate_kind):
                state, error = hotplug.observe_expected_usb_edge(
                    gate_kind,
                    usb_baseline(before),
                    {"usb_microphones": after, "usb_error": None},
                )
                self.assertEqual(state, "observed")
                self.assertIsNone(error)

        state, error = hotplug.observe_expected_usb_edge(
            "promotion",
            usb_baseline(usb_topology()),
            {
                "usb_microphones": usb_topology(lark=("1-1.3@9", "1-1.4@10")),
                "usb_error": None,
            },
        )
        self.assertEqual(state, "error")
        self.assertIn("ambiguous", error or "")

    def test_fallback_edge_ignores_transient_descriptor_loss_for_same_instance(
        self,
    ) -> None:
        baseline_topology = usb_topology(lark=("1-1.3@9",))
        baseline_topology["lark-a1"][0]["usb_serial"] = "Wireless Microphone"
        teardown_sample = usb_topology(lark=("1-1.3@9",))

        state, error = hotplug.observe_expected_usb_edge(
            "fallback",
            usb_baseline(baseline_topology),
            {"usb_microphones": teardown_sample, "usb_error": None},
        )

        self.assertEqual(state, "waiting")
        self.assertIsNone(error)

    def test_fallback_edge_rejects_new_generation_without_observed_removal(
        self,
    ) -> None:
        state, error = hotplug.observe_expected_usb_edge(
            "fallback",
            usb_baseline(usb_topology(lark=("1-1.3@9",))),
            {
                "usb_microphones": usb_topology(lark=("1-1.3@10",)),
                "usb_error": None,
            },
        )

        self.assertEqual(state, "error")
        self.assertIn("changed instance", error or "")

    def test_gated_latency_uses_usb_edge_source_monotonic(self) -> None:
        class Sampler:
            def __init__(self, samples):
                self.samples = samples

            def samples_after(self, seq):
                return [sample for sample in self.samples if sample["seq"] > seq]

            def wait_for_new_sample(self, seq, timeout=1.0):
                return None

            def service_counts(self):
                return {unit: 0 for unit in hotplug.SERVICE_UNITS}

            def record_event(self, event_type, **values):
                return None

        event = {
            "seq": 1,
            "timestamp": 125.0,
            "elapsed_s": 25.0,
            "capture_started_monotonic": 125.0,
            "state": "DISCOVERING",
            "microphone": None,
            "candidates": {},
            "aec": {},
            "invariants": {"violations": [], "hfp_inputs": []},
            "usb_microphones": usb_topology(lark=("1-1.3@9",)),
            "usb_error": None,
            "sampling": {"start_gap_s": 0.15},
        }
        confirmation = {**event, "seq": 2, "capture_started_monotonic": 125.15}
        active_samples = []
        for seq, source_time in enumerate(
            (125.3, 125.45, 125.6, 125.75, 125.9), start=3
        ):
            sample = sample_for(
                active_status(selected_id="lark-a1"), active_links(LARK)
            )
            sample.update(
                {
                    "seq": seq,
                    "timestamp": source_time,
                    # Host/receipt-style elapsed evidence deliberately advances too
                    # little to satisfy settle; the Pi source clock must decide it.
                    "elapsed_s": 25.0 + seq * 0.01,
                    "capture_started_monotonic": source_time,
                    "usb_microphones": usb_topology(lark=("1-1.3@9",)),
                    "usb_error": None,
                    "sampling": {"start_gap_s": 0.15},
                }
            )
            active_samples.append(sample)
        action = {
            "monotonic": 100.0,
            "elapsed_s": 0.0,
            "after_seq": 0,
            "phase": "lark_promotion",
            "cycle": 1,
            "action_id": "action-1",
            "timestamp": 1_000.0,
            "usb_baseline": usb_baseline(usb_topology()),
            "usb_action_observation_timeout_s": 60.0,
            "service_restart_counts": {unit: 0 for unit in hotplug.SERVICE_UNITS},
        }
        with mock.patch.object(hotplug.time, "monotonic", return_value=100.0):
            result = hotplug.wait_for_expectation(
                Sampler([event, confirmation, *active_samples]),
                action,
                hotplug.Expectation(state="ACTIVE", selected_id="lark-a1"),
                timeout_s=60.0,
                settle_s=0.6,
                gate_kind="promotion",
            )

        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["timing_origin"], hotplug.USB_TIMING_ORIGIN)
        self.assertEqual(result["action_to_usb_s"], 25.0)
        self.assertEqual(result["operator_latency_s"], 25.0)
        self.assertEqual(result["transition_latency_s"], 0.3)
        self.assertEqual(result["settled_latency_s"], 0.9)
        self.assertEqual(result["usb_event"]["candidate_id"], "lark-a1")
        self.assertTrue(result["usb_event"]["confirmed"])
        self.assertTrue(result["usb_event"]["persistent"])
        self.assertEqual(
            result["timing_evidence_version"], hotplug.TIMING_EVIDENCE_VERSION
        )

    def test_gated_transition_fails_closed_when_usb_edge_is_missing(self) -> None:
        class Sampler:
            def samples_after(self, seq):
                return [
                    {
                        "seq": 1,
                        "capture_started_monotonic": 130.0,
                        "elapsed_s": 30.0,
                        "state": "ACTIVE",
                        "invariants": {"violations": [], "hfp_inputs": []},
                        "usb_microphones": usb_topology(),
                        "usb_error": None,
                    }
                ]

            def wait_for_new_sample(self, seq, timeout=1.0):
                return None

            def service_counts(self):
                return {unit: 0 for unit in hotplug.SERVICE_UNITS}

            def record_event(self, event_type, **values):
                return None

        action = {
            "monotonic": 100.0,
            "elapsed_s": 0.0,
            "after_seq": 0,
            "phase": "lark_promotion",
            "cycle": 1,
            "action_id": "action-1",
            "timestamp": 1_000.0,
            "usb_baseline": usb_baseline(usb_topology()),
            "usb_action_observation_timeout_s": 60.0,
            "service_restart_counts": {unit: 0 for unit in hotplug.SERVICE_UNITS},
        }
        clock_values = iter((100.0, 161.0, 161.0))
        with mock.patch.object(
            hotplug.time, "monotonic", side_effect=lambda: next(clock_values, 161.0)
        ):
            result = hotplug.wait_for_expectation(
                Sampler(),
                action,
                hotplug.Expectation(state="ACTIVE", selected_id="lark-a1"),
                timeout_s=60.0,
                settle_s=0.0,
                gate_kind="promotion",
            )

        self.assertEqual(result["outcome"], "usb_event_missing")
        self.assertEqual(result["timing_origin"], "usb_sysfs_edge_missing")
        self.assertIsNone(result["transition_latency_s"])
        self.assertIn("not observed", result["usb_event_error"])

    def test_sample_schema_carries_raw_usb_inventory(self) -> None:
        sampler = hotplug.LiveSampler(Path("status.json"), Path("timeline.jsonl"), 0.15)
        started = time.monotonic()
        sampler._started_monotonic = started - 1.0
        topology = usb_topology()
        with (
            mock.patch.object(
                hotplug, "read_status", return_value=(active_status(), None)
            ),
            mock.patch.object(
                hotplug, "read_usb_microphones", return_value=(topology, None)
            ),
            mock.patch.object(hotplug, "command_output", return_value=("", None)),
            mock.patch.object(hotplug, "parse_pw_links", return_value=active_links()),
        ):
            sampler._capture_sample(started)

        sample = sampler.latest()
        assert sample is not None
        self.assertEqual(sample["usb_microphones"], topology)
        self.assertIsNone(sample["usb_error"])

    def test_parse_pw_links_preserves_node_level_direction(self) -> None:
        text = "\n".join(
            [
                f"{hotplug.MICROPHONE_OUTPUT}:output_FL",
                f"  |-> {HFP_SINK}:input_FL",
                f"{hotplug.AEC_CAPTURE}:input_FL",
                f"  |<- {FIFINE}:capture_FL",
            ]
        )
        self.assertEqual(
            hotplug.parse_pw_links(text),
            [
                (hotplug.MICROPHONE_OUTPUT, HFP_SINK),
                (FIFINE, hotplug.AEC_CAPTURE),
            ],
        )

    def test_active_graph_passes_link_invariants(self) -> None:
        result = hotplug.evaluate_link_invariants(
            active_status(), None, active_links(), now=100.0
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["hfp_inputs"], [hotplug.MICROPHONE_OUTPUT])
        self.assertEqual(result["aec_capture_inputs"], [FIFINE])
        self.assertEqual(result["inactive_candidate_nodes"], [LARK])

    def test_status_timestamp_must_be_finite_and_not_future(self) -> None:
        for timestamp in (
            None,
            "100.0",
            True,
            float("nan"),
            float("inf"),
            float("-inf"),
            100.001,
        ):
            with self.subTest(timestamp=timestamp):
                status = active_status()
                status["timestamp"] = timestamp
                result = hotplug.evaluate_link_invariants(
                    status, None, active_links(), now=100.0
                )
                self.assertIn("H0", {item["id"] for item in result["violations"]})

    def test_raw_inactive_and_duplicate_uplinks_fail_closed(self) -> None:
        links = active_links() + [(LARK, hotplug.AEC_CAPTURE), (LARK, HFP_SINK)]
        result = hotplug.evaluate_link_invariants(
            active_status(), None, links, now=100.0
        )
        rules = {item["id"] for item in result["violations"]}
        self.assertTrue({"H1", "H2", "H3", "H4", "H5"}.issubset(rules))

    def test_new_bluez_sink_is_guarded_before_status_publishes_it(self) -> None:
        status = active_status()
        status["endpoints"]["hfp_sink"] = None
        result = hotplug.evaluate_link_invariants(
            status,
            None,
            [(FIFINE, HFP_SINK)],
            now=100.0,
        )
        self.assertIn("H1", {item["id"] for item in result["violations"]})

    def test_new_physical_source_is_inactive_before_status_publishes_it(self) -> None:
        new_node = "alsa_input.usb-NEW-MIC.analog-stereo"
        for target in (hotplug.AEC_CAPTURE, hotplug.MICROPHONE_INPUT, HFP_SINK):
            with self.subTest(target=target):
                result = hotplug.evaluate_link_invariants(
                    active_status(),
                    None,
                    active_links() + [(new_node, target)],
                    now=100.0,
                )
                self.assertIn("H4", {item["id"] for item in result["violations"]})
                self.assertIn(new_node, result["inactive_candidate_nodes"])

    def test_former_candidate_node_remains_in_safety_inventory(self) -> None:
        status = active_status(selected_id="lark-a1")
        fifine = status["microphone"]["candidates"][1]
        fifine.update({"state": "absent", "node": None, "matched_nodes": []})
        result = hotplug.evaluate_link_invariants(
            status,
            None,
            [(FIFINE, hotplug.AEC_CAPTURE)],
            known_microphone_nodes={LARK, FIFINE},
            now=100.0,
        )
        self.assertIn("H4", {item["id"] for item in result["violations"]})
        self.assertIn(FIFINE, result["inactive_candidate_nodes"])

    def test_waiting_mic_requires_no_routes_or_aec_owner(self) -> None:
        status = active_status()
        status["state"] = "WAITING_MIC"
        status["microphone"]["selected"] = None
        status["microphone"]["candidates"] = [
            {"id": "lark-a1", "state": "absent", "matched_nodes": []},
            {"id": "fifine-k054", "state": "absent", "matched_nodes": []},
        ]
        status["endpoints"]["microphone"] = None
        status["endpoints"]["lark"] = None
        status["aec"]["owner_pid"] = None
        clean = hotplug.evaluate_link_invariants(status, None, [], now=100.0)
        self.assertTrue(clean["passed"])

        unsafe = hotplug.evaluate_link_invariants(
            status,
            None,
            [(hotplug.MICROPHONE_OUTPUT, HFP_SINK)],
            now=100.0,
        )
        self.assertIn("H6", {item["id"] for item in unsafe["violations"]})

    def test_waiting_mic_still_requires_live_hfp_call_endpoints(self) -> None:
        status = active_status()
        status["state"] = "WAITING_MIC"
        status["microphone"]["selected"] = None
        status["microphone"]["candidates"] = []
        status["aec"]["owner_pid"] = None
        status["call"]["hfp_nodes_present"] = False
        status["endpoints"]["hfp_source"] = None
        status["endpoints"]["hfp_sink"] = None
        result = hotplug.evaluate_link_invariants(status, None, [], now=100.0)
        self.assertIn("H9", {item["id"] for item in result["violations"]})

    def test_ambiguous_candidate_and_stale_status_are_unsafe(self) -> None:
        status = active_status()
        status["microphone"]["candidates"][0]["state"] = "ambiguous"
        result = hotplug.evaluate_link_invariants(
            status,
            None,
            active_links(),
            now=107.0,
        )
        rules = {item["id"] for item in result["violations"]}
        self.assertEqual(rules, {"H0", "H7"})

    def test_active_expectation_requires_identity_generation_and_exact_routes(
        self,
    ) -> None:
        status = active_status()
        sample = {
            "state": "ACTIVE",
            "status_error": None,
            "link_error": None,
            "microphone": hotplug.selected_microphone(status),
            "candidates": hotplug.candidate_inventory(status),
            "graph_generation": 7,
            "aec": status["aec"],
            "invariants": hotplug.evaluate_link_invariants(
                status, None, active_links(), now=100.0
            ),
        }
        expected = hotplug.Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            different_instance_token="old-token",
            generation_after=6,
            candidate_states={"lark-a1": frozenset({"usable"})},
        )
        self.assertEqual(hotplug.expectation_failures(sample, expected), [])

        sample["graph_generation"] = 6
        self.assertTrue(hotplug.expectation_failures(sample, expected))

    def test_break_before_make_silence_is_safe_but_not_active_completion(self) -> None:
        status = active_status()
        safe_partial_routes = {
            "empty": [],
            "aec_capture_only": [(FIFINE, hotplug.AEC_CAPTURE)],
            "aec_source_only": [(hotplug.AEC_SOURCE, hotplug.MICROPHONE_INPUT)],
            "hfp_output_only": [(hotplug.MICROPHONE_OUTPUT, HFP_SINK)],
        }
        for name, links in safe_partial_routes.items():
            with self.subTest(name=name):
                invariants = hotplug.evaluate_link_invariants(
                    status, None, links, now=100.0
                )
                self.assertEqual(invariants["violations"], [])
                sample = {
                    "state": "ACTIVE",
                    "status_error": None,
                    "link_error": None,
                    "microphone": hotplug.selected_microphone(status),
                    "candidates": hotplug.candidate_inventory(status),
                    "graph_generation": 7,
                    "aec": status["aec"],
                    "invariants": invariants,
                }
                failures = hotplug.expectation_failures(
                    sample,
                    hotplug.Expectation(state="ACTIVE", selected_id="fifine-k054"),
                )
                self.assertTrue(failures)

    def test_active_requires_enabled_verified_owned_aec_without_direct_bypass(
        self,
    ) -> None:
        expected = hotplug.Expectation(state="ACTIVE", selected_id="fifine-k054")
        for field, value in (
            ("enabled", False),
            ("verified", False),
            ("owner_pid", None),
        ):
            with self.subTest(field=field):
                status = active_status()
                status["aec"][field] = value
                sample = sample_for(status)
                self.assertIn(
                    "H5", {item["id"] for item in sample["invariants"]["violations"]}
                )
                self.assertTrue(hotplug.expectation_failures(sample, expected))

        status = active_status()
        direct_links = [
            (FIFINE, hotplug.MICROPHONE_INPUT),
            (hotplug.MICROPHONE_OUTPUT, HFP_SINK),
        ]
        sample = sample_for(status, direct_links)
        self.assertIn("H5", {item["id"] for item in sample["invariants"]["violations"]})
        self.assertTrue(hotplug.expectation_failures(sample, expected))

    def test_identity_comparisons_fail_closed_when_current_evidence_is_missing(
        self,
    ) -> None:
        status = active_status()
        sample = sample_for(status)
        sample["microphone"].pop("instance_token")
        sample["graph_generation"] = None
        failures = hotplug.expectation_failures(
            sample,
            hotplug.Expectation(
                state="ACTIVE",
                selected_id="fifine-k054",
                different_instance_token="old-token",
                generation_after=6,
            ),
        )
        self.assertIn("selected microphone instance token is missing", failures)
        self.assertIn("graph generation is missing", failures)

    def test_selected_microphone_always_requires_token_and_generation(self) -> None:
        sample = sample_for(active_status())
        sample["microphone"].pop("instance_token")
        sample["graph_generation"] = None
        failures = hotplug.expectation_failures(
            sample,
            hotplug.Expectation(state="ACTIVE", selected_id="fifine-k054"),
        )
        self.assertIn("selected microphone instance token is missing", failures)
        self.assertIn("graph generation is missing", failures)

    def test_sample_identity_refuses_missing_token_or_generation(self) -> None:
        sample = sample_for(active_status())
        sample["microphone"].pop("instance_token")
        with self.assertRaisesRegex(hotplug.CampaignAbort, "instance token"):
            hotplug._sample_identity({"final_sample": sample})

        sample = sample_for(active_status())
        sample["graph_generation"] = None
        with self.assertRaisesRegex(hotplug.CampaignAbort, "graph generation"):
            hotplug._sample_identity({"final_sample": sample})

    def test_twenty_cycle_timing_gate_requires_nineteen_fast_and_all_bounded(
        self,
    ) -> None:
        transitions = timing_transitions("promotion")
        gate = hotplug.summarize_timing_gate(
            transitions, kind="promotion", expected_cycles=20
        )
        self.assertEqual(gate["verdict"], "PASS")
        self.assertEqual(gate["completed_under_fast_limit"], 19)
        self.assertEqual(gate["within_max_or_actionable_safe"], 20)

        transitions.append(dict(transitions[0]))
        self.assertEqual(
            hotplug.summarize_timing_gate(
                transitions, kind="promotion", expected_cycles=20
            )["verdict"],
            "FAIL",
        )
        transitions.pop()
        transitions[18]["transition_latency_s"] = 40.0
        self.assertEqual(
            hotplug.summarize_timing_gate(
                transitions, kind="promotion", expected_cycles=20
            )["verdict"],
            "FAIL",
        )

    def test_timing_gate_cannot_be_weakened_for_repeated_campaigns(self) -> None:
        transitions = [valid_timing_transition("promotion") for _ in range(20)]
        for transition in transitions:
            transition["transition_latency_s"] = 45.0
        gate = hotplug.summarize_timing_gate(
            transitions,
            kind="promotion",
            expected_cycles=2,
            fast_limit_s=90.0,
            max_limit_s=120.0,
        )
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertEqual(gate["expected_cycles"], 20)
        self.assertEqual(gate["required_completed_under_fast_limit"], 19)
        self.assertEqual(gate["fast_limit_s"], 30.0)
        self.assertEqual(gate["max_limit_s"], 60.0)

    def test_matrix_timing_gate_has_one_canonical_cycle(self) -> None:
        transition = valid_timing_transition("promotion")
        gate = hotplug.summarize_timing_gate(
            [transition], kind="promotion", expected_cycles=1
        )
        self.assertEqual(gate["verdict"], "PASS")
        self.assertEqual(gate["required_completed_under_fast_limit"], 1)

        transition["transition_latency_s"] = -0.001
        self.assertEqual(
            hotplug.summarize_timing_gate(
                [transition], kind="promotion", expected_cycles=1
            )["verdict"],
            "FAIL",
        )

    def test_timing_gate_rejects_legacy_operator_origin(self) -> None:
        transition = valid_timing_transition("promotion")
        transition["timing_origin"] = "operator_now"
        gate = hotplug.summarize_timing_gate(
            [transition], kind="promotion", expected_cycles=1
        )
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertEqual(gate["usb_timed_cycles"], 0)

    def test_gate_transition_may_return_actionable_safe_state(self) -> None:
        class Sampler:
            def mark_action(self, phase, cycle, instruction, connection_layout=None):
                return {
                    "phase": phase,
                    "cycle": cycle,
                    "instruction": instruction,
                    "action_id": "action-1",
                    "connection_layout": connection_layout,
                }

            def record_event(self, event_type, **values):
                return None

        result = {
            "phase": "lark_promotion",
            "outcome": "safe_state",
            "transition_latency_s": None,
        }
        with mock.patch.object(hotplug, "wait_for_expectation", return_value=result):
            observed = hotplug.run_step(
                Sampler(),
                [],
                phase="lark_promotion",
                cycle=20,
                instruction="plug it in",
                expected=hotplug.Expectation(state="ACTIVE", selected_id="lark-a1"),
                timeout_s=60.0,
                settle_s=0.0,
                gate_kind="promotion",
                input_fn=lambda: "PLUG LARK",
            )
        self.assertEqual(observed["outcome"], "safe_state")

    def test_repeated_promotion_fallback_recovers_each_safe_gate(self) -> None:
        transitions: list[dict] = []
        calls: list[dict] = []

        def fake_run_step(_sampler, recorded, **values):
            phase = values["phase"]
            calls.append(values)
            identities = {
                "fifine_only_baseline": ("fifine-opening", 0),
                "lark_promotion/recovery": ("lark-recovered", 1),
                "lark_fallback/recovery": ("fifine-recovered", 2),
            }
            outcome = (
                "safe_state"
                if phase in {"lark_promotion", "lark_fallback"}
                else "completed"
            )
            token, generation = identities.get(phase, (None, None))
            result = {
                "phase": phase,
                "cycle": values["cycle"],
                "gate_kind": values.get("gate_kind"),
                "outcome": outcome,
                "timing_origin": (
                    hotplug.USB_TIMING_ORIGIN
                    if values.get("gate_kind")
                    else "operator_now"
                ),
                "transition_latency_s": None if outcome == "safe_state" else 0.0,
                "safety_clean": True,
                "restart_clean": True,
                "final_sample": {
                    "microphone": (
                        {"instance_token": token} if token is not None else None
                    ),
                    "graph_generation": generation,
                },
            }
            recorded.append(result)
            return result

        with mock.patch.object(hotplug, "run_step", side_effect=fake_run_step):
            hotplug.run_promotion_fallback(
                object(),
                transitions,
                cycles=1,
                timeout_s=60.0,
                settle_s=0.6,
            )

        self.assertEqual(
            [item["phase"] for item in calls],
            [
                "fifine_only_baseline",
                "lark_promotion",
                "lark_promotion/recovery",
                "lark_fallback",
                "lark_fallback/recovery",
            ],
        )
        self.assertEqual(
            [item["gate_kind"] for item in transitions if item["gate_kind"]],
            ["promotion", "fallback"],
        )
        fallback_call = next(item for item in calls if item["phase"] == "lark_fallback")
        self.assertEqual(
            fallback_call["expected"].different_instance_token, "lark-recovered"
        )
        self.assertEqual(fallback_call["expected"].generation_after, 1)

    def test_matrix_recovers_after_each_gated_safe_state(self) -> None:
        transitions: list[dict] = []
        calls: list[dict] = []
        safe_phases = {"lark_promotion", "lark_fallback", "restore_fifine"}
        identities = {
            "fifine_only_baseline": ("fifine-opening", 0),
            "lark_promotion/recovery": ("lark-recovered", 1),
            "lark_fallback/recovery": ("fifine-fallback-recovered", 2),
            "inactive_fifine_change/setup_lark": ("lark-setup", 3),
            "restore_fifine/recovery": ("fifine-replug-recovered", 4),
            "restore_lark": ("lark-final", 5),
        }

        def fake_run_step(_sampler, recorded, **values):
            phase = values["phase"]
            calls.append(values)
            outcome = "safe_state" if phase in safe_phases else "completed"
            token, generation = identities.get(phase, (None, None))
            result = {
                "phase": phase,
                "cycle": values["cycle"],
                "gate_kind": values.get("gate_kind"),
                "outcome": outcome,
                "timing_origin": (
                    hotplug.USB_TIMING_ORIGIN
                    if values.get("gate_kind")
                    else "operator_now"
                ),
                "transition_latency_s": None if outcome == "safe_state" else 0.0,
                "safety_clean": True,
                "restart_clean": True,
                "final_sample": {
                    "microphone": (
                        {"instance_token": token} if token is not None else None
                    ),
                    "graph_generation": generation,
                },
            }
            recorded.append(result)
            return result

        with mock.patch.object(hotplug, "run_step", side_effect=fake_run_step):
            hotplug.run_matrix(
                object(),
                transitions,
                cycles=1,
                timeout_s=60.0,
                settle_s=0.6,
            )

        self.assertEqual(
            [item["phase"] for item in calls if item["phase"].endswith("/recovery")],
            [
                "lark_promotion/recovery",
                "lark_fallback/recovery",
                "restore_fifine/recovery",
            ],
        )
        self.assertEqual(
            [item["gate_kind"] for item in transitions if item["gate_kind"]],
            ["promotion", "fallback", "fifine_replug"],
        )
        restore_lark = next(item for item in calls if item["phase"] == "restore_lark")
        self.assertEqual(
            restore_lark["expected"].different_instance_token,
            "fifine-replug-recovered",
        )
        self.assertEqual(restore_lark["expected"].generation_after, 4)

    def test_twenty_cycle_replug_qualifies_with_one_safe_recovery(self) -> None:
        transitions: list[dict] = []
        calls: list[dict] = []

        def fake_run_step(_sampler, recorded, **values):
            phase = values["phase"]
            cycle = values["cycle"]
            calls.append(values)
            safe = phase == "fifine_replug/restore" and cycle == 20
            identity = None
            generation = None
            if phase == "fifine_only_baseline":
                identity, generation = "fifine-0", 0
            elif phase == "fifine_replug/restore" and not safe:
                identity, generation = f"fifine-{cycle}", cycle
            elif phase == "fifine_replug/restore/recovery":
                identity, generation = "fifine-20", 20
            result = {
                "phase": phase,
                "cycle": cycle,
                "gate_kind": values.get("gate_kind"),
                "outcome": "safe_state" if safe else "completed",
                "timing_origin": (
                    hotplug.USB_TIMING_ORIGIN
                    if values.get("gate_kind")
                    else "operator_now"
                ),
                "transition_latency_s": (
                    None if safe else (30.0 if values.get("gate_kind") else 0.0)
                ),
                "safety_clean": True,
                "restart_clean": True,
                "final_sample": {
                    "microphone": (
                        {"instance_token": identity} if identity is not None else None
                    ),
                    "graph_generation": generation,
                },
            }
            if values.get("gate_kind"):
                evidence = valid_timing_transition(
                    values["gate_kind"],
                    outcome="safe_state" if safe else "completed",
                )
                evidence.update(
                    {
                        key: value
                        for key, value in result.items()
                        if key != "final_sample"
                    }
                )
                evidence["transition_latency_s"] = None if safe else 0.15
                if identity is not None:
                    evidence["final_sample"]["microphone"]["instance_token"] = identity
                    evidence["final_sample"]["graph_generation"] = generation
                result = evidence
            recorded.append(result)
            return result

        with mock.patch.object(hotplug, "run_step", side_effect=fake_run_step):
            hotplug.run_fifine_replug(
                object(),
                transitions,
                cycles=20,
                timeout_s=60.0,
                settle_s=0.6,
            )

        gated = [item for item in transitions if item["gate_kind"] == "fifine_replug"]
        recoveries = [
            item
            for item in transitions
            if item["phase"] == "fifine_replug/restore/recovery"
        ]
        self.assertEqual(len(gated), 20)
        self.assertEqual(sum(item["outcome"] == "completed" for item in gated), 19)
        self.assertEqual(sum(item["outcome"] == "safe_state" for item in gated), 1)
        self.assertEqual(len(recoveries), 1)
        recovery_call = next(
            item for item in calls if item["phase"] == "fifine_replug/restore/recovery"
        )
        self.assertEqual(
            recovery_call["expected"].different_instance_token, "fifine-19"
        )
        self.assertEqual(recovery_call["expected"].generation_after, 19)

        summary = hotplug.build_summary(
            sampler=summary_sampler(),
            campaign="fifine-replug",
            cycles=20,
            transitions=transitions,
            aborted=None,
            fast_limit_s=30.0,
            max_limit_s=60.0,
            started_wall=100.0,
        )
        self.assertEqual(summary["qualification_gate"], "PASS")

    def test_recovery_actions_use_explicit_acknowledgements(self) -> None:
        class Sampler:
            def mark_action(self, phase, cycle, instruction, connection_layout=None):
                return {
                    "phase": phase,
                    "cycle": cycle,
                    "instruction": instruction,
                    "action_id": "recovery-action",
                }

            def record_event(self, event_type, **values):
                return None

        for phase, acknowledgement in (
            ("lark_promotion/recovery", "RECOVER BOTH"),
            ("lark_fallback/recovery", "RECOVER FIFINE"),
            ("restore_fifine/recovery", "RECOVER FIFINE"),
            ("fifine_replug/restore/recovery", "RECOVER FIFINE"),
        ):
            with self.subTest(phase=phase):
                action = hotplug.operator_action(
                    Sampler(),
                    phase=phase,
                    cycle=1,
                    instruction="recover the active fixture",
                    input_fn=lambda value=acknowledgement: value,
                )
                self.assertEqual(action["required_acknowledgement"], acknowledgement)

    def test_actionable_safe_state_must_be_captured_by_deadline(self) -> None:
        class Sampler:
            def __init__(self, sample):
                self.sample = sample

            def samples_after(self, seq):
                return [self.sample] if self.sample["seq"] > seq else []

            def wait_for_new_sample(self, seq, timeout=1.0):
                return None

            def service_counts(self):
                return {unit: 0 for unit in hotplug.SERVICE_UNITS}

            def record_event(self, event_type, **values):
                return None

        action = {
            "monotonic": 0.0,
            "elapsed_s": 0.0,
            "after_seq": 0,
            "phase": "restore_lark",
            "cycle": 1,
            "action_id": "action-1",
            "timestamp": 100.0,
            "service_restart_counts": {unit: 0 for unit in hotplug.SERVICE_UNITS},
        }

        def observe(captured_at: float) -> dict:
            sample = {
                "seq": 1,
                "capture_started_monotonic": captured_at,
                "elapsed_s": captured_at,
                "state": "WAITING_MIC",
                "selection_reason": "microphone disconnected",
                "invariants": {"violations": [], "hfp_inputs": []},
            }
            with mock.patch.object(hotplug.time, "monotonic", return_value=61.0):
                return hotplug.wait_for_expectation(
                    Sampler(sample),
                    action,
                    hotplug.Expectation(state="ACTIVE", selected_id="lark-a1"),
                    timeout_s=60.0,
                    settle_s=0.0,
                    gate_kind=None,
                )

        self.assertEqual(observe(60.0)["outcome"], "safe_state")
        self.assertEqual(observe(60.001)["outcome"], "timeout")

    def test_service_restart_parser_and_delta_are_complete(self) -> None:
        # systemd does not preserve the requested property order; the Pi emits
        # NRestarts before Id in each blank-line-delimited unit block.
        text = "\n\n".join(
            f"NRestarts={index}\nId={unit}\nActiveState=active"
            for index, unit in enumerate(hotplug.SERVICE_UNITS)
        )
        counts = hotplug.parse_service_restart_counts(text)
        self.assertEqual(counts["bridge-supervisor.service"], 0)
        after = {unit: int(value or 0) + 1 for unit, value in counts.items()}
        self.assertEqual(
            hotplug.service_restart_delta(counts, after),
            {unit: 1 for unit in hotplug.SERVICE_UNITS},
        )

    def test_summary_distinguishes_configured_interval_from_measured_gap(self) -> None:
        transitions = timing_transitions("fifine_replug")
        summary = hotplug.build_summary(
            sampler=summary_sampler(max_gap=0.24),
            campaign="fifine-replug",
            cycles=20,
            transitions=transitions,
            aborted=None,
            fast_limit_s=90.0,
            max_limit_s=120.0,
            started_wall=100.0,
        )
        self.assertEqual(summary["sampling"]["gate"], "PASS")
        self.assertEqual(summary["qualification_gate"], "PASS")
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["thresholds"]["configured_interval_max_s"], 0.20)
        self.assertEqual(summary["thresholds"]["measured_start_gap_max_s"], 0.25)

        failed = hotplug.build_summary(
            sampler=summary_sampler(max_gap=0.251),
            campaign="fifine-replug",
            cycles=20,
            transitions=transitions,
            aborted=None,
            fast_limit_s=30.0,
            max_limit_s=60.0,
            started_wall=100.0,
        )
        self.assertEqual(failed["sampling"]["gate"], "FAIL")
        self.assertEqual(failed["qualification_gate"], "FAIL")

    def test_split_connection_plan_requires_ten_plus_ten_and_valid_handoff(
        self,
    ) -> None:
        for campaign in ("promotion-fallback", "fifine-replug"):
            with self.subTest(campaign=campaign):
                transitions = split_layout_transitions(campaign)
                summary = hotplug.build_summary(
                    sampler=summary_sampler(),
                    campaign=campaign,
                    cycles=20,
                    transitions=transitions,
                    aborted=None,
                    fast_limit_s=30.0,
                    max_limit_s=60.0,
                    started_wall=100.0,
                    connection_plan=hotplug.CONNECTION_PLAN_DIRECT10_HUB10,
                )
                self.assertEqual(summary["qualification_gate"], "PASS")
                self.assertEqual(summary["connection_layout_gate"]["verdict"], "PASS")
                self.assertEqual(
                    summary["connection_layout_gate"]["observed_cycles"][
                        hotplug.required_gate_kinds(campaign)[0]
                    ]["direct"],
                    list(range(1, 11)),
                )

                without_handoff = [
                    item
                    for item in transitions
                    if item["phase"] != hotplug.CONNECTION_LAYOUT_HANDOFF_PHASE
                ]
                gate = hotplug.summarize_connection_layout_gate(
                    without_handoff,
                    campaign=campaign,
                    connection_plan=hotplug.CONNECTION_PLAN_DIRECT10_HUB10,
                )
                self.assertEqual(gate["verdict"], "FAIL")

    def test_split_layout_gate_rejects_label_or_attestation_tampering(self) -> None:
        transitions = split_layout_transitions("promotion-fallback")
        transitions[0]["connection_layout"] = "powered_hub"
        handoff = next(
            item
            for item in transitions
            if item["phase"] == hotplug.CONNECTION_LAYOUT_HANDOFF_PHASE
        )
        handoff["operator_attestation"] = {
            "observation_kind": "usb_sysfs_ancestry_delta",
            "claim": "external_hub_power_supply_connected",
            "operator_acknowledged": True,
        }
        gate = hotplug.summarize_connection_layout_gate(
            transitions,
            campaign="promotion-fallback",
            connection_plan=hotplug.CONNECTION_PLAN_DIRECT10_HUB10,
        )
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertTrue(
            any("operator attestation" in error for error in gate["errors"])
        )

    def test_split_layout_gate_rejects_external_hub_during_direct_cycles(self) -> None:
        transitions = split_layout_transitions(
            "promotion-fallback", direct_external_hub=True
        )
        gate = hotplug.summarize_connection_layout_gate(
            transitions,
            campaign="promotion-fallback",
            connection_plan=hotplug.CONNECTION_PLAN_DIRECT10_HUB10,
        )
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertTrue(
            any("already on an external USB hub" in error for error in gate["errors"])
        )

    def test_nonqualifying_summary_status_is_incomplete_not_pass(self) -> None:
        summary = hotplug.build_summary(
            sampler=summary_sampler(),
            campaign="fifine-replug",
            cycles=1,
            transitions=timing_transitions("fifine_replug")[:1],
            aborted=None,
            fast_limit_s=90.0,
            max_limit_s=120.0,
            started_wall=100.0,
        )
        self.assertEqual(summary["qualification_gate"], "INCOMPLETE")
        self.assertEqual(summary["verdict"], "INCOMPLETE")

    def test_direct_runner_exits_nonzero_for_incomplete_qualification(self) -> None:
        def one_cycle(sampler, transitions, **_values):
            sampler.initial_service_counts = {unit: 0 for unit in hotplug.SERVICE_UNITS}
            sampler.final_service_counts = {unit: 0 for unit in hotplug.SERVICE_UNITS}
            sampler.total_samples = 1
            sampler.max_sample_gap = 0.24
            transitions.extend(timing_transitions("fifine_replug")[:1])

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "incomplete"
            with (
                mock.patch.object(hotplug.LiveSampler, "start", return_value=None),
                mock.patch.object(hotplug.LiveSampler, "stop", return_value=None),
                mock.patch.object(hotplug, "run_fifine_replug", side_effect=one_cycle),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = hotplug.main(
                    [
                        "--campaign",
                        "fifine-replug",
                        "--cycles",
                        "1",
                        "--out-dir",
                        str(output),
                    ]
                )
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(status, 1)
            self.assertEqual(summary["qualification_gate"], "INCOMPLETE")
            self.assertEqual(summary["verdict"], "INCOMPLETE")

    def test_cli_cannot_relax_fixed_qualification_deadlines(self) -> None:
        for arguments in (["--fast-limit", "30.001"], ["--timeout", "60.001"]):
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                hotplug.parse_args(arguments)

    def test_split_connection_plan_is_strictly_scoped_and_forces_twenty(self) -> None:
        for campaign in ("promotion-fallback", "fifine-replug"):
            args = hotplug.parse_args(
                [
                    "--campaign",
                    campaign,
                    "--connection-plan",
                    hotplug.CONNECTION_PLAN_DIRECT10_HUB10,
                ]
            )
            self.assertEqual(args.cycles, 20)
        for arguments in (
            ["--connection-plan", hotplug.CONNECTION_PLAN_DIRECT10_HUB10],
            [
                "--campaign",
                "promotion-fallback",
                "--connection-plan",
                hotplug.CONNECTION_PLAN_DIRECT10_HUB10,
                "--cycles",
                "10",
            ],
        ):
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                hotplug.parse_args(arguments)

    def test_direct_runner_refuses_existing_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "existing"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            stdout = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = hotplug.main(["--out-dir", str(output)])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 2)
            self.assertEqual(payload["failure"]["type"], "OutputDirectoryExists")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")
            self.assertFalse((output / "summary.json").exists())

    def test_unexpected_exception_writes_failure_summary_and_stops_sampler(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "new-run"
            with (
                mock.patch.object(hotplug.LiveSampler, "start", return_value=None),
                mock.patch.object(
                    hotplug.LiveSampler, "stop", return_value=None
                ) as stop,
                mock.patch.object(
                    hotplug,
                    "run_matrix",
                    side_effect=RuntimeError("unexpected boom"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = hotplug.main(["--out-dir", str(output)])
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(status, 1)
            self.assertEqual(summary["verdict"], "FAIL")
            self.assertEqual(summary["qualification_gate"], "FAIL")
            self.assertEqual(summary["failure"]["type"], "RuntimeError")
            self.assertIn("unexpected boom", summary["failure"]["message"])
            self.assertNotIn("timeline_jsonl", summary["artifacts"])
            stop.assert_called_once()

    def test_typed_action_acknowledgement_is_recorded(self) -> None:
        class Sampler:
            def __init__(self) -> None:
                self.events = []

            def mark_action(self, phase, cycle, instruction, connection_layout=None):
                return {
                    "phase": phase,
                    "cycle": cycle,
                    "instruction": instruction,
                    "action_id": "action-1",
                    "connection_layout": connection_layout,
                }

            def record_event(self, event_type, **values):
                self.events.append((event_type, values))

        sampler = Sampler()
        action = hotplug.operator_action(
            sampler,
            phase="lark_promotion",
            cycle=1,
            instruction="plug it in",
            connection_layout=hotplug.CONNECTION_LAYOUT_POWERED_HUB,
            input_fn=lambda: "PLUG LARK",
        )
        self.assertTrue(action["operator_acknowledged"])
        self.assertEqual(action["required_acknowledgement"], "PLUG LARK")
        self.assertEqual(
            action["connection_layout"], hotplug.CONNECTION_LAYOUT_POWERED_HUB
        )
        self.assertEqual(sampler.events[0][0], "operator_acknowledgement")
        self.assertEqual(
            sampler.events[0][1]["connection_layout"],
            hotplug.CONNECTION_LAYOUT_POWERED_HUB,
        )

    def test_action_waits_for_restart_snapshot_before_now_prompt(self) -> None:
        class Sampler:
            def mark_action(self, phase, cycle, instruction, connection_layout=None):
                raise hotplug.CampaignAbort("restart-count evidence failed")

            def record_event(self, event_type, **values):
                raise AssertionError("no acknowledgement may be recorded")

        output = io.StringIO()
        with (
            self.assertRaisesRegex(hotplug.CampaignAbort, "restart-count"),
            contextlib.redirect_stderr(output),
        ):
            hotplug.operator_action(
                Sampler(),
                phase="lark_promotion",
                cycle=1,
                instruction="plug it in",
                input_fn=lambda: "PLUG LARK",
            )
        self.assertIn(
            "do not perform the action until the NOW prompt", output.getvalue()
        )
        self.assertNotIn("NOW:", output.getvalue())

    def test_prechanged_usb_baseline_is_rejected_before_now_prompt(self) -> None:
        sampler = hotplug.LiveSampler(Path("status.json"), Path("timeline.jsonl"), 0.15)
        sampler._started_monotonic = time.monotonic()
        output = io.StringIO()
        with (
            feed_local_action_baseline(
                sampler,
                [usb_topology(lark=("1-1.3@9",))] * 3,
            ),
            mock.patch.object(
                sampler,
                "_query_service_counts",
                return_value=({unit: 0 for unit in hotplug.SERVICE_UNITS}, None),
            ),
            self.assertRaisesRegex(hotplug.CampaignAbort, "expected 0 lark-a1"),
            contextlib.redirect_stderr(output),
        ):
            hotplug.operator_action(
                sampler,
                phase="lark_promotion",
                cycle=1,
                instruction="plug it in",
                input_fn=lambda: "PLUG LARK",
            )
        self.assertNotIn("NOW:", output.getvalue())

    def test_local_action_boundaries_query_restart_counts_synchronously(self) -> None:
        sampler = hotplug.LiveSampler(Path("status.json"), Path("timeline.jsonl"), 0.15)
        sampler._started_monotonic = time.monotonic()
        sampler._service_counts = {unit: 99 for unit in hotplug.SERVICE_UNITS}
        with (
            feed_local_action_baseline(sampler, [usb_topology()] * 3),
            mock.patch.object(
                sampler,
                "_query_service_counts",
                side_effect=[
                    ({unit: 2 for unit in hotplug.SERVICE_UNITS}, None),
                    ({unit: 3 for unit in hotplug.SERVICE_UNITS}, None),
                ],
            ) as query,
        ):
            action = sampler.mark_action(
                "lark_promotion",
                1,
                "plug it in",
                connection_layout=hotplug.CONNECTION_LAYOUT_POWERED_HUB,
            )
            after = sampler.service_counts()

        self.assertEqual(query.call_count, 2)
        self.assertEqual(
            action["service_restart_counts"],
            {unit: 2 for unit in hotplug.SERVICE_UNITS},
        )
        self.assertEqual(
            action["connection_layout"], hotplug.CONNECTION_LAYOUT_POWERED_HUB
        )
        self.assertEqual(
            sampler._connection_layout, hotplug.CONNECTION_LAYOUT_POWERED_HUB
        )
        self.assertEqual(
            hotplug.service_restart_delta(action["service_restart_counts"], after),
            {unit: 1 for unit in hotplug.SERVICE_UNITS},
        )

    def test_in_flight_capture_keeps_pre_action_sequence_and_metadata(self) -> None:
        sampler = hotplug.LiveSampler(Path("status.json"), Path("timeline.jsonl"), 0.15)
        started = time.monotonic()
        sampler._started_monotonic = started - 1.0
        capture_entered = threading.Event()
        release_capture = threading.Event()
        boundary_ready = threading.Event()
        action: dict = {}

        def status_during_capture(_path):
            capture_entered.set()
            self.assertTrue(release_capture.wait(2))
            return active_status(), None

        original_baseline = sampler._action_usb_baseline_locked

        def baseline_after_reservation(phase, after_seq):
            self.assertEqual(after_seq, 1)
            boundary_ready.set()
            return original_baseline(phase, after_seq)

        def mark() -> None:
            action.update(sampler.mark_action("lark_promotion", 1, "plug it in"))

        with (
            mock.patch.object(
                hotplug, "read_status", side_effect=status_during_capture
            ),
            mock.patch.object(
                sampler,
                "_query_service_counts",
                return_value=({unit: 0 for unit in hotplug.SERVICE_UNITS}, None),
            ),
            mock.patch.object(hotplug, "command_output", return_value=("", None)),
            mock.patch.object(
                hotplug,
                "read_usb_microphones",
                return_value=(usb_topology(), None),
            ),
            mock.patch.object(hotplug, "parse_pw_links", return_value=active_links()),
            mock.patch.object(
                sampler,
                "_action_usb_baseline_locked",
                side_effect=baseline_after_reservation,
            ),
        ):
            capture_thread = threading.Thread(
                target=sampler._capture_sample, args=(started,)
            )
            capture_thread.start()
            self.assertTrue(capture_entered.wait(2))
            action_thread = threading.Thread(target=mark)
            action_thread.start()
            self.assertTrue(boundary_ready.wait(2))
            release_capture.set()
            capture_thread.join(2)
            self.assertFalse(capture_thread.is_alive())
            in_flight = sampler.latest()
            with sampler._condition:
                for seq in (2, 3, 4):
                    sampler._seq = seq
                    sampler._recent.append(
                        direct_usb_sample(
                            seq,
                            usb_topology(),
                            source_monotonic=started + seq * 0.15,
                        )
                    )
                sampler._condition.notify_all()
            action_thread.join(2)
            self.assertFalse(action_thread.is_alive())

        self.assertIsNotNone(in_flight)
        assert in_flight is not None
        self.assertEqual(in_flight["seq"], 1)
        self.assertEqual(action["after_seq"], 4)
        self.assertEqual(in_flight["phase"], "startup")
        self.assertIsNone(in_flight["action_id"])
        self.assertEqual(sampler.samples_after(action["after_seq"]), [])


if __name__ == "__main__":
    unittest.main()
