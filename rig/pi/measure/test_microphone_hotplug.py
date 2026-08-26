from __future__ import annotations

import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from rig.pi.measure import microphone_hotplug as hotplug

HFP_SINK = "bluez_output.phone.headset-head-unit"
LARK = "alsa_input.usb-LARK.analog-stereo"
FIFINE = "alsa_input.usb-FIFINE.mono-fallback"


def usb_device(candidate_id: str, generation: str) -> dict:
    port, raw_devnum = generation.rsplit("@", 1)
    vendor_id, product_id = hotplug.USB_MICROPHONE_FINGERPRINTS[candidate_id]
    return {
        "id": candidate_id,
        "usb_vendor_id": vendor_id,
        "usb_product_id": product_id,
        "usb_product": None,
        "usb_serial": None,
        "usb_port_path": port,
        "usb_devnum": int(raw_devnum),
        "usb_instance_generation": generation,
    }


def usb_topology(
    *,
    lark: tuple[str, ...] = (),
    fifine: tuple[str, ...] = ("1-1.2@4",),
) -> dict[str, list[dict]]:
    return {
        "lark-a1": [usb_device("lark-a1", value) for value in lark],
        "fifine-k054": [usb_device("fifine-k054", value) for value in fifine],
    }


def usb_baseline(topology: dict[str, list[dict]]) -> dict:
    return {
        "seq": 1,
        "remote_seq": None,
        "capture_started_monotonic": 99.0,
        "usb_microphones": topology,
        "usb_error": None,
    }


def active_status(*, selected_id: str = "fifine-k054") -> dict:
    selected_node = FIFINE if selected_id == "fifine-k054" else LARK
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
                    "usb_vendor_id": "0c76" if selected_id == "fifine-k054" else "3547",
                    "usb_product_id": "161e"
                    if selected_id == "fifine-k054"
                    else "0407",
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


def timing_transitions(kind: str) -> list[dict]:
    transitions = [
        {
            "gate_kind": kind,
            "outcome": "completed",
            "timing_origin": hotplug.USB_TIMING_ORIGIN,
            "transition_latency_s": 30.0,
            "safety_clean": True,
            "restart_clean": True,
        }
        for _ in range(hotplug.QUALIFICATION_REQUIRED_FAST)
    ]
    transitions.append(
        {
            "gate_kind": kind,
            "outcome": "safe_state",
            "timing_origin": hotplug.USB_TIMING_ORIGIN,
            "transition_latency_s": None,
            "safety_clean": True,
            "restart_clean": True,
        }
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
        }
        matched = sample_for(active_status(selected_id="lark-a1"), active_links(LARK))
        matched.update(
            {
                "seq": 2,
                "timestamp": 128.25,
                "elapsed_s": 28.25,
                "capture_started_monotonic": 128.25,
                "usb_microphones": usb_topology(lark=("1-1.3@9",)),
                "usb_error": None,
            }
        )
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
                Sampler([event, matched]),
                action,
                hotplug.Expectation(state="ACTIVE", selected_id="lark-a1"),
                timeout_s=60.0,
                settle_s=0.0,
                gate_kind="promotion",
            )

        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["timing_origin"], hotplug.USB_TIMING_ORIGIN)
        self.assertEqual(result["action_to_usb_s"], 25.0)
        self.assertEqual(result["operator_latency_s"], 25.0)
        self.assertEqual(result["transition_latency_s"], 3.25)
        self.assertEqual(result["settled_latency_s"], 3.25)
        self.assertEqual(result["usb_event"]["candidate_id"], "lark-a1")

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
        transitions = [
            {
                "gate_kind": "promotion",
                "outcome": "completed",
                "timing_origin": hotplug.USB_TIMING_ORIGIN,
                "transition_latency_s": 30.0,
                "safety_clean": True,
                "restart_clean": True,
            }
            for _ in range(19)
        ]
        transitions.append(
            {
                "gate_kind": "promotion",
                "outcome": "safe_state",
                "timing_origin": hotplug.USB_TIMING_ORIGIN,
                "transition_latency_s": None,
                "safety_clean": True,
                "restart_clean": True,
            }
        )
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
        transitions = [
            {
                "gate_kind": "promotion",
                "outcome": "completed",
                "timing_origin": hotplug.USB_TIMING_ORIGIN,
                "transition_latency_s": 45.0,
                "safety_clean": True,
                "restart_clean": True,
            }
            for _ in range(20)
        ]
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
        transition = {
            "gate_kind": "promotion",
            "outcome": "completed",
            "timing_origin": hotplug.USB_TIMING_ORIGIN,
            "transition_latency_s": 30.0,
            "safety_clean": True,
            "restart_clean": True,
        }
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
        transition = {
            "gate_kind": "promotion",
            "outcome": "completed",
            "timing_origin": "operator_now",
            "transition_latency_s": 1.0,
            "safety_clean": True,
            "restart_clean": True,
        }
        gate = hotplug.summarize_timing_gate(
            [transition], kind="promotion", expected_cycles=1
        )
        self.assertEqual(gate["verdict"], "FAIL")
        self.assertEqual(gate["usb_timed_cycles"], 0)

    def test_gate_transition_may_return_actionable_safe_state(self) -> None:
        class Sampler:
            def mark_action(self, phase, cycle, instruction):
                return {
                    "phase": phase,
                    "cycle": cycle,
                    "instruction": instruction,
                    "action_id": "action-1",
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
            def mark_action(self, phase, cycle, instruction):
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

            def mark_action(self, phase, cycle, instruction):
                return {
                    "phase": phase,
                    "cycle": cycle,
                    "instruction": instruction,
                    "action_id": "action-1",
                }

            def record_event(self, event_type, **values):
                self.events.append((event_type, values))

        sampler = Sampler()
        action = hotplug.operator_action(
            sampler,
            phase="lark_promotion",
            cycle=1,
            instruction="plug it in",
            input_fn=lambda: "PLUG LARK",
        )
        self.assertTrue(action["operator_acknowledged"])
        self.assertEqual(action["required_acknowledgement"], "PLUG LARK")
        self.assertEqual(sampler.events[0][0], "operator_acknowledgement")

    def test_action_waits_for_restart_snapshot_before_now_prompt(self) -> None:
        class Sampler:
            def mark_action(self, phase, cycle, instruction):
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
        sampler._recent.append(
            {
                "seq": 1,
                "capture_started_monotonic": time.monotonic(),
                "usb_microphones": usb_topology(lark=("1-1.3@9",)),
                "usb_error": None,
            }
        )
        output = io.StringIO()
        with (
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
        sampler._recent.append(
            {
                "seq": 0,
                "capture_started_monotonic": time.monotonic(),
                "usb_microphones": usb_topology(),
                "usb_error": None,
            }
        )
        with mock.patch.object(
            sampler,
            "_query_service_counts",
            side_effect=[
                ({unit: 2 for unit in hotplug.SERVICE_UNITS}, None),
                ({unit: 3 for unit in hotplug.SERVICE_UNITS}, None),
            ],
        ) as query:
            action = sampler.mark_action("lark_promotion", 1, "plug it in")
            after = sampler.service_counts()

        self.assertEqual(query.call_count, 2)
        self.assertEqual(
            action["service_restart_counts"],
            {unit: 2 for unit in hotplug.SERVICE_UNITS},
        )
        self.assertEqual(
            hotplug.service_restart_delta(action["service_restart_counts"], after),
            {unit: 1 for unit in hotplug.SERVICE_UNITS},
        )

    def test_in_flight_capture_keeps_pre_action_sequence_and_metadata(self) -> None:
        sampler = hotplug.LiveSampler(Path("status.json"), Path("timeline.jsonl"), 0.15)
        started = time.monotonic()
        sampler._started_monotonic = started - 1.0
        sampler._recent.append(
            {
                "seq": 0,
                "capture_started_monotonic": started - 0.1,
                "usb_microphones": usb_topology(),
                "usb_error": None,
            }
        )
        action: dict = {}

        def status_during_capture(_path):
            action.update(sampler.mark_action("lark_promotion", 1, "plug it in"))
            return active_status(), None

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
        ):
            sampler._capture_sample(started)

        sample = sampler.latest()
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample["seq"], 1)
        self.assertEqual(action["after_seq"], 1)
        self.assertEqual(sample["phase"], "startup")
        self.assertIsNone(sample["action_id"])
        self.assertEqual(sampler.samples_after(action["after_seq"]), [])


if __name__ == "__main__":
    unittest.main()
