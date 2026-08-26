from __future__ import annotations

import unittest

import microphones

LARK_NODE = microphones.DEFAULT_LARK_NODE
FIFINE_NODE = "alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback"


def candidate(
    candidate_id: str = "lark-a1",
    *,
    node: str = LARK_NODE,
    vendor: str = "3547",
    product_id: str = "0407",
    product: str | None = "Wireless Microphone",
    serial: str | None = None,
    port: str | None = None,
    channels: int = 2,
    capture_only: bool = True,
) -> microphones.MicrophoneCandidate:
    return microphones.MicrophoneCandidate(
        id=candidate_id,
        label=candidate_id,
        node_name=node,
        usb_vendor_id=vendor,
        usb_product_id=product_id,
        usb_product=product,
        usb_serial=serial,
        usb_port_path=port,
        required_rate=48_000,
        required_format="S16_LE",
        required_channels=channels,
        capture_only=capture_only,
    )


def source(
    node: str = LARK_NODE,
    *,
    vendor: str | None = "3547",
    product_id: str | None = "0407",
    product: str | None = "Wireless Microphone",
    serial: str | None = None,
    port: str | None = "1-1.4",
    generation: str | None = "1-1.4@8",
    device_id: str = "41",
    device_serial: str = "400",
    object_serial: str = "401",
    channels: int = 2,
    audio_format: str = "S16LE",
    rate: int = 48_000,
    component: str = "USB3547:0407",
    playback: bool | None = False,
) -> microphones.ObservedSource:
    formats = (
        microphones.MicrophoneFormat(rate, audio_format, channels),
    )
    return microphones.ObservedSource(
        node=node,
        pipewire_id=object_serial,
        pipewire_object_serial=object_serial,
        device_id=device_id,
        device_object_serial=device_serial,
        alsa_components=(component,),
        usb_vendor_id=vendor,
        usb_product_id=product_id,
        usb_product=product,
        usb_serial=serial,
        usb_port_path=port,
        usb_instance_generation=generation,
        formats=formats,
        device_has_playback=playback,
    )


def fifine_candidate(**overrides: object) -> microphones.MicrophoneCandidate:
    values: dict[str, object] = {
        "candidate_id": "fifine-k054",
        "node": FIFINE_NODE,
        "vendor": "0c76",
        "product_id": "161e",
        "product": "USB PnP Audio Device",
        "channels": 1,
    }
    values.update(overrides)
    return candidate(**values)  # type: ignore[arg-type]


def fifine_source(**overrides: object) -> microphones.ObservedSource:
    values: dict[str, object] = {
        "node": FIFINE_NODE,
        "vendor": "0c76",
        "product_id": "161e",
        "product": "USB PnP Audio Device",
        "port": "1-1.2",
        "generation": "1-1.2@11",
        "device_id": "51",
        "device_serial": "500",
        "object_serial": "501",
        "channels": 1,
        "component": "USB0c76:161e",
    }
    values.update(overrides)
    return source(**values)  # type: ignore[arg-type]


class NormalizationTests(unittest.TestCase):
    def test_usb_ids_accept_common_spellings(self) -> None:
        self.assertEqual(microphones.normalize_usb_id("0xC76"), "0c76")
        self.assertEqual(microphones.normalize_usb_id("USB:0407"), "0407")
        self.assertEqual(microphones.normalize_usb_id(0x1BF6), "1bf6")

    def test_invalid_usb_ids_are_rejected(self) -> None:
        for value in ("", "0c76:161e", "zzzz", -1, 0x10000, True):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                microphones.normalize_usb_id(value)

    def test_audio_format_comparison_ignores_separator_spelling(self) -> None:
        self.assertEqual(
            microphones.MicrophoneFormat(48_000, "S16_LE", 1),
            microphones.MicrophoneFormat(48_000, "s16le", 1),
        )


class ConfigurationTests(unittest.TestCase):
    def explicit_document(self) -> dict:
        return {
            "devices": {
                "microphones": [
                    {
                        "id": "lark-a1",
                        "label": "Hollyland Lark A1",
                        "node_name": LARK_NODE,
                        "usb_vendor_id": "3547",
                        "usb_product_id": "0407",
                        "usb_product": "Wireless Microphone",
                        "required_rate": 48_000,
                        "required_format": "S16LE",
                        "required_channels": 2,
                        "capture_only": True,
                    },
                    {
                        "id": "fifine-k054",
                        "label": "FIFINE K054",
                        "node_name": FIFINE_NODE,
                        "usb_vendor_id": "0C76",
                        "usb_product_id": "161E",
                        "usb_product": "USB PnP Audio Device",
                        "usb_serial": "",
                        "usb_port_path": "",
                        "required_rate": 48_000,
                        "required_format": "S16_LE",
                        "required_channels": 1,
                        "capture_only": True,
                    },
                ],
                "lark": {"usb_vendor_id": "ffff", "usb_product_id": "ffff"},
            }
        }

    def test_explicit_array_preserves_priority_and_ignores_legacy_table(self) -> None:
        parsed = microphones.parse_microphone_candidates(self.explicit_document(), {})
        self.assertEqual([item.id for item in parsed], ["lark-a1", "fifine-k054"])
        self.assertEqual(parsed[1].usb_vendor_id, "0c76")
        self.assertEqual(parsed[1].required_format, "S16LE")
        self.assertIsNone(parsed[1].usb_serial)
        self.assertFalse(any(item.legacy for item in parsed))

    def test_environment_changes_only_explicit_lark_profile_not_hard_identity(self) -> None:
        parsed = microphones.parse_microphone_candidates(
            self.explicit_document(),
            {"BRIDGE_LARK": "alternate.lark", "BRIDGE_LARK_COMPONENT": "USB9999:0001"},
        )
        self.assertEqual(parsed[0].node_name, "alternate.lark")
        self.assertEqual(parsed[0].alsa_component, "USB9999:0001")
        self.assertEqual((parsed[0].usb_vendor_id, parsed[0].usb_product_id), ("3547", "0407"))
        self.assertEqual(parsed[1].node_name, FIFINE_NODE)

    def test_missing_array_synthesizes_legacy_lark(self) -> None:
        parsed = microphones.parse_microphone_candidates({}, {})
        self.assertEqual(len(parsed), 1)
        self.assertTrue(parsed[0].legacy)
        self.assertEqual(parsed[0].node_name, LARK_NODE)
        self.assertEqual(parsed[0].alsa_component, "USB3547:0407")
        self.assertIsNone(parsed[0].required_capability)
        self.assertFalse(parsed[0].capture_only)

    def test_legacy_precedence_is_environment_then_nonblank_config_then_default(self) -> None:
        document = {
            "devices": {
                "lark": {
                    "node_name": "configured.node",
                    "usb_vendor_id": "0c76",
                    "usb_product_id": "161e",
                    "usb_serial": "unit-a",
                }
            }
        }
        configured = microphones.parse_microphone_candidates(document, {})[0]
        self.assertEqual(configured.node_name, "configured.node")
        self.assertEqual(configured.alsa_component, "USB0C76:161E")
        self.assertEqual(configured.usb_serial, "unit-a")
        overridden = microphones.parse_microphone_candidates(
            document,
            {"BRIDGE_LARK": "environment.node", "BRIDGE_LARK_COMPONENT": "USB3547:0407"},
        )[0]
        self.assertEqual(overridden.node_name, "environment.node")
        self.assertEqual(overridden.usb_vendor_id, "3547")

    def test_partial_legacy_usb_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "set together"):
            microphones.parse_microphone_candidates(
                {"devices": {"lark": {"usb_vendor_id": "3547"}}}, {}
            )

    def test_explicit_candidates_require_identity_and_capabilities(self) -> None:
        entry = self.explicit_document()["devices"]["microphones"][0]
        for missing in ("usb_vendor_id", "usb_product_id", "required_rate", "required_format", "required_channels"):
            with self.subTest(missing=missing):
                incomplete = dict(entry)
                incomplete.pop(missing)
                with self.assertRaises((TypeError, ValueError)):
                    microphones.parse_microphone_candidates(
                        {"devices": {"microphones": [incomplete]}}, {}
                    )

    def test_duplicate_ids_are_rejected(self) -> None:
        entry = self.explicit_document()["devices"]["microphones"][0]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            microphones.parse_microphone_candidates(
                    {"devices": {"microphones": [entry, dict(entry)]}}, {}
            )

    def test_partial_manual_capability_constraint_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be set together"):
            microphones.MicrophoneCandidate(
                id="unsafe",
                label="Unsafe partial candidate",
                node_name="source",
                usb_vendor_id="0c76",
                usb_product_id="161e",
                required_rate=48_000,
            )


class ObservationTests(unittest.TestCase):
    def test_pw_dump_joins_source_to_device_and_enrichment(self) -> None:
        dump = [
            {
                "id": 70,
                "type": "PipeWire:Interface:Device",
                "info": {
                    "props": {
                        "object.serial": 700,
                        "device.product.name": "untrusted PipeWire label",
                        "device.serial": "0c76_USB_PnP_Audio_Device",
                        "device.vendor.id": "0xffff",
                        "device.product.id": "0xffff",
                    }
                },
            },
            {
                "id": 71,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "node.name": FIFINE_NODE,
                        "media.class": "Audio/Source",
                        "object.serial": 701,
                        "device.id": 70,
                        "alsa.components": "USB0c76:161e",
                    }
                },
            },
        ]
        observed = microphones.observations_from_pw_dump(
            dump,
            sysfs_by_device={
                70: {
                    "idVendor": "0C76",
                    "idProduct": "161E",
                    "product": "USB PnP Audio Device",
                    "serial": "",
                    "usb_port_path": "1-1.2",
                    "usb_instance_generation": "1-1.2@14",
                }
            },
            capabilities_by_node={
                FIFINE_NODE: {"rate": 48_000, "format": "S16_LE", "channels": 1}
            },
        )
        self.assertEqual(len(observed), 1)
        item = observed[0]
        self.assertEqual(item.device_id, "70")
        self.assertEqual(item.device_object_serial, "700")
        self.assertEqual(item.pipewire_object_serial, "701")
        self.assertEqual(item.usb_vendor_id, "0c76")
        self.assertEqual(item.usb_product, "USB PnP Audio Device")
        self.assertIsNone(item.usb_serial)
        self.assertEqual(item.usb_instance_generation, "1-1.2@14")
        self.assertFalse(item.device_has_playback)
        self.assertEqual(item.formats[0].as_dict(), {"rate": 48_000, "format": "S16LE", "channels": 1})

    def test_pipewire_device_properties_are_not_usb_identity_evidence(self) -> None:
        dump = [
            {
                "id": 70,
                "type": "PipeWire:Interface:Device",
                "info": {
                    "props": {
                        "object.serial": 700,
                        "device.vendor.id": "0x0c76",
                        "device.product.id": "0x161e",
                        "device.product.name": "USB PnP Audio Device",
                        "device.serial": "0c76_USB_PnP_Audio_Device",
                    }
                },
            },
            {
                "id": 71,
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {
                        "node.name": FIFINE_NODE,
                        "media.class": "Audio/Source",
                        "object.serial": 701,
                        "device.id": 70,
                        "alsa.components": "USB0c76:161e",
                    }
                },
            },
        ]

        item = microphones.observations_from_pw_dump(dump)[0]

        self.assertIsNone(item.usb_vendor_id)
        self.assertIsNone(item.usb_product_id)
        self.assertIsNone(item.usb_product)
        self.assertIsNone(item.usb_serial)

    def test_capture_only_fingerprint_observes_a_sink_on_the_same_device(self) -> None:
        dump = [
            {"id": 10, "type": "PipeWire:Interface:Device", "info": {"props": {}}},
            {
                "id": 11,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": "source", "media.class": "Audio/Source", "device.id": 10}},
            },
            {
                "id": 12,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": "sink", "media.class": "Audio/Sink", "device.id": 10}},
            },
        ]
        observed = microphones.observations_from_pw_dump(dump)
        self.assertTrue(observed[0].device_has_playback)

    def test_format_enrichment_accepts_enumerated_lists(self) -> None:
        dump = [
            {
                "id": 1,
                "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": "source", "media.class": "Audio/Source"}},
            }
        ]
        observed = microphones.observations_from_pw_dump(
            dump,
            capabilities_by_node={
                "source": {"rate": [44_100, 48_000], "format": ["S16LE"], "channels": [1, 2]}
            },
        )
        self.assertEqual(len(observed[0].formats), 4)

    def test_node_map_adapter_is_stable_and_detects_playback(self) -> None:
        nodes = {
            FIFINE_NODE: {
                "media.class": "Audio/Source",
                "device.id": 70,
                "object.serial": 701,
                "alsa.components": "USB0c76:161e",
            },
            "alsa_output.same-device": {"media.class": "Audio/Sink", "device.id": 70},
        }
        observed = microphones.observations_from_node_map(
            nodes,
            identities_by_node={
                FIFINE_NODE: {
                    "usb_vendor_id": "0c76",
                    "usb_product_id": "161e",
                    "usb_product": "USB PnP Audio Device",
                }
            },
            capabilities_by_node={
                FIFINE_NODE: {"rate": 48_000, "format": "S16LE", "channels": 1}
            },
        )
        self.assertEqual(observed[0].pipewire_id, FIFINE_NODE)
        self.assertTrue(observed[0].device_has_playback)


class ResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lark = candidate()
        self.fifine = fifine_candidate()
        self.lark_source = source()
        self.fifine_source = fifine_source()

    def test_both_present_selects_lark_and_reports_fifine_usable(self) -> None:
        result = microphones.resolve(
            [self.lark, self.fifine], [self.fifine_source, self.lark_source]
        )
        self.assertEqual(result.selected.candidate.id, "lark-a1")  # type: ignore[union-attr]
        self.assertEqual([item.state for item in result.diagnostics], ["selected", "usable"])
        self.assertEqual(result.reason, "using lark-a1")

    def test_reversed_enumeration_has_identical_resolution(self) -> None:
        forward = microphones.resolve(
            [self.lark, self.fifine], [self.lark_source, self.fifine_source]
        )
        reversed_result = microphones.resolve(
            [self.lark, self.fifine], [self.fifine_source, self.lark_source]
        )
        self.assertEqual(forward.as_dict(), reversed_result.as_dict())

    def test_absent_lark_falls_back_with_actionable_reason(self) -> None:
        result = microphones.resolve([self.lark, self.fifine], [self.fifine_source])
        self.assertEqual(result.node, FIFINE_NODE)
        self.assertEqual(result.reason, "lark-a1 absent; using fifine-k054")
        self.assertFalse(result.blocked)

    def test_capability_mismatch_allows_lower_priority_fallback(self) -> None:
        wrong_lark = source(channels=1)
        result = microphones.resolve([self.lark, self.fifine], [wrong_lark, self.fifine_source])
        self.assertEqual(result.node, FIFINE_NODE)
        self.assertEqual(result.diagnostics[0].state, "capability_mismatch")

    def test_exact_node_cannot_bypass_explicit_hard_identity(self) -> None:
        impostor = source(vendor="0c76", product_id="161e", component="USB0c76:161e")
        result = microphones.resolve([self.lark], [impostor])
        self.assertIsNone(result.selected)
        self.assertEqual(result.diagnostics[0].state, "absent")

    def test_component_is_required_even_when_sysfs_identity_matches(self) -> None:
        missing_component = source(component="USBaabb:ccdd")
        result = microphones.resolve([self.lark], [missing_component])
        self.assertEqual(result.diagnostics[0].state, "absent")

    def test_product_serial_and_port_constraints_are_hard(self) -> None:
        constrained = candidate(serial="serial-a", port="1-1.2")
        for observed in (
            source(serial=None, port="1-1.2"),
            source(serial="serial-b", port="1-1.2"),
            source(serial="serial-a", port="1-1.3"),
            source(serial="serial-a", port="1-1.2", product="lookalike"),
        ):
            with self.subTest(observed=observed):
                self.assertEqual(
                    microphones.resolve([constrained], [observed]).diagnostics[0].state,
                    "absent",
                )

    def test_duplicate_physical_units_are_ambiguous_and_block_fallback(self) -> None:
        duplicate = source(
            device_id="42",
            device_serial="410",
            object_serial="411",
            port="1-1.3",
            generation="1-1.3@12",
        )
        result = microphones.resolve(
            [self.lark, self.fifine],
            [self.lark_source, duplicate, self.fifine_source],
        )
        self.assertIsNone(result.selected)
        self.assertTrue(result.blocked)
        self.assertEqual(result.diagnostics[0].state, "ambiguous")
        self.assertIn("configure usb_serial or usb_port_path", result.reason)

    def test_serial_or_port_constraint_disambiguates_duplicate_units(self) -> None:
        pinned = candidate(port="1-1.4")
        duplicate = source(
            device_id="42",
            device_serial="410",
            object_serial="411",
            port="1-1.3",
            generation="1-1.3@12",
        )
        result = microphones.resolve([pinned], [duplicate, self.lark_source])
        self.assertEqual(result.node, LARK_NODE)
        self.assertFalse(result.blocked)

    def test_preferred_node_selects_one_profile_of_one_physical_device(self) -> None:
        alternate = source(
            node="alternate.profile",
            object_serial="402",
            device_id=self.lark_source.device_id or "",
            device_serial=self.lark_source.device_object_serial or "",
        )
        result = microphones.resolve([self.lark], [alternate, self.lark_source])
        self.assertEqual(result.node, LARK_NODE)

    def test_multiple_profiles_without_a_matching_preference_fail_closed(self) -> None:
        no_profile = candidate(node="missing.profile")
        first = source(node="profile.one")
        second = source(node="profile.two", object_serial="402")
        result = microphones.resolve([no_profile], [first, second])
        self.assertTrue(result.blocked)
        self.assertEqual(result.diagnostics[0].state, "ambiguous")

    def test_overlapping_candidate_definitions_are_conflicts(self) -> None:
        alias = candidate("lark-alias", node="another.profile")
        result = microphones.resolve([self.lark, alias, self.fifine], [self.fifine_source])
        self.assertTrue(result.blocked)
        self.assertEqual(result.diagnostics[0].state, "conflict")
        self.assertEqual(result.diagnostics[1].state, "conflict")
        self.assertIsNone(result.selected)

    def test_lower_priority_conflict_does_not_disrupt_a_distinct_selected_device(self) -> None:
        fifine_alias = fifine_candidate(candidate_id="generic-usb-mic")
        result = microphones.resolve(
            [self.lark, self.fifine, fifine_alias],
            [self.lark_source, self.fifine_source],
        )
        self.assertEqual(result.node, LARK_NODE)
        self.assertEqual(result.diagnostics[1].state, "conflict")

    def test_capture_only_and_unknown_capability_evidence_fail_safe(self) -> None:
        playback_device = source(playback=True)
        unknown_format = microphones.ObservedSource(
            node=LARK_NODE,
            device_id="41",
            alsa_components=("USB3547:0407",),
            usb_vendor_id="3547",
            usb_product_id="0407",
            usb_product="Wireless Microphone",
            device_has_playback=False,
        )
        for observed in (playback_device, unknown_format):
            with self.subTest(observed=observed):
                result = microphones.resolve([self.lark], [observed])
                self.assertEqual(result.diagnostics[0].state, "capability_mismatch")

    def test_neither_present_reports_no_selection_without_blocking(self) -> None:
        result = microphones.resolve([self.lark, self.fifine], [])
        self.assertIsNone(result.selected)
        self.assertFalse(result.blocked)
        self.assertEqual([item.state for item in result.diagnostics], ["absent", "absent"])

    def test_same_node_replug_changes_instance_token(self) -> None:
        before = microphones.resolve([self.fifine], [self.fifine_source])
        after_source = fifine_source(
            object_serial="601",
            device_serial="600",
            generation="1-1.2@19",
        )
        after = microphones.resolve([self.fifine], [after_source])
        self.assertNotEqual(before.instance_token, after.instance_token)
        self.assertIn("fifine-k054", after.instance_token or "")
        self.assertIn("1-1.2@19", after.instance_token or "")

    def test_status_shape_contains_identity_format_and_diagnostics(self) -> None:
        status = microphones.resolve([self.fifine], [self.fifine_source]).as_dict()
        self.assertEqual(status["selected"]["id"], "fifine-k054")
        self.assertEqual(status["selected"]["identity"]["usb_vendor_id"], "0c76")
        self.assertEqual(
            status["selected"]["format"],
            {"rate": 48_000, "format": "S16LE", "channels": 1},
        )
        self.assertEqual(status["candidates"][0]["state"], "selected")
        self.assertEqual(status["candidates"][0]["matched_nodes"], [FIFINE_NODE])
        self.assertEqual(status["candidates"][0]["node"], FIFINE_NODE)
        self.assertIsNone(status["candidates"][0]["unavailable_reason"])
        self.assertEqual(
            status["candidates"][0]["identity"]["usb_product_id"], "161e"
        )
        self.assertEqual(
            status["candidates"][0]["format"],
            {"rate": 48_000, "format": "S16LE", "channels": 1},
        )

    def test_legacy_exact_node_and_component_fallback_are_preserved(self) -> None:
        legacy = microphones.parse_microphone_candidates({}, {})[0]
        exact_without_identity = microphones.ObservedSource(node=LARK_NODE)
        renamed_component = microphones.ObservedSource(
            node="renamed.lark",
            alsa_components=("USB3547:0407",),
        )
        self.assertEqual(
            microphones.resolve([legacy], [exact_without_identity]).node,
            LARK_NODE,
        )
        self.assertEqual(
            microphones.resolve([legacy], [renamed_component]).node,
            "renamed.lark",
        )


if __name__ == "__main__":
    unittest.main()
