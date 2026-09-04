from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge_supervisor as supervisor
import btadapters
import controller_roles
import microphones

CALL_ADDRESS = "A0:AD:9F:73:6C:24"
ONBOARD_ADDRESS = "B8:27:EB:43:8D:51"
PHONE_ADDRESS = "5C:33:7B:CB:BF:C5"


def role_settings(*, volume: float | None = None) -> supervisor.Settings:
    roles = controller_roles.parse_controller_roles(
        {
            "bridge": {"mode": "bluetooth-wired"},
            "devices": {
                "phone": {
                    "address": PHONE_ADDRESS,
                    "adapter": CALL_ADDRESS,
                    "adapter_product": "ASUS USB-BT500",
                    "adapter_bus": "USB",
                    "adapter_usb_vendor_id": "0b05",
                    "adapter_usb_product_id": "1bf6",
                },
                "output": {"id": "wired:alsa_output.platform-test.stereo"},
            },
        }
    )
    return supervisor.Settings(
        aec=supervisor.AecSettings(),
        phone_mac=PHONE_ADDRESS,
        controller_roles=roles,
        wired_output_volume=volume,
    )


def role_adapter(
    hci: str,
    address: str = CALL_ADDRESS,
    *,
    bus: str = "USB",
    product_id: str = "1bf6",
) -> btadapters.Adapter:
    return btadapters.Adapter(
        hci,
        address,
        bus,
        int(hci.removeprefix("hci")),
        usb_vendor_id="0b05" if bus == "USB" else None,
        usb_product_id=product_id if bus == "USB" else None,
        usb_parent="1-1.2" if bus == "USB" else None,
        usb_interface="1-1.2:1.0" if bus == "USB" else None,
        driver="btusb" if bus == "USB" else "hci_uart",
    )


class SettingsTests(unittest.TestCase):
    def test_missing_config_is_safe_aec_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = supervisor.load_settings(Path(directory) / "missing.toml")
        self.assertFalse(settings.aec.enabled)
        self.assertEqual(settings.aec.rate, 48_000)
        self.assertEqual(settings.aec.channels, 1)

    def test_valid_aec_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                """
[audio.aec]
enabled = true
method = "webrtc"
rate = 48000
channels = 1
failure_policy = "fail_closed"
""",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                settings = supervisor.load_settings(config)
        self.assertTrue(settings.aec.enabled)

    def test_invalid_rate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text("[audio.aec]\nrate = 16000\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "48000"):
                supervisor.load_settings(config)

    def test_invalid_webrtc_boolean_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text('[audio.aec]\nnoise_suppression = "false"\n', encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "noise_suppression"):
                supervisor.load_settings(config)

    def test_single_call_controller_and_wired_output_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                """
[bridge]
mode = "bluetooth-wired"

[devices.phone]
address = "5C:33:7B:CB:BF:C5"
adapter = "A0:AD:9F:73:6C:24"
adapter_product = "ASUS USB-BT500"
adapter_bus = "USB"
adapter_usb_vendor_id = "0b05"
adapter_usb_product_id = "1bf6"

[devices.output]
id = "wired:alsa_output.platform-test.stereo"

[audio]
wired_output_volume = 0.85
""",
                encoding="utf-8",
            )
            settings = supervisor.load_settings(config)
        self.assertEqual(settings.desired_output, "wired:alsa_output.platform-test.stereo")
        self.assertIsNone(settings.speaker_adapter)
        self.assertIsNotNone(settings.controller_roles)
        assert settings.controller_roles is not None
        self.assertEqual(settings.controller_roles.call.address, CALL_ADDRESS)
        self.assertIsNone(settings.controller_roles.output)
        self.assertEqual(settings.wired_output_volume, 0.85)

    def test_invalid_wired_volume_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text("[audio]\nwired_output_volume = 1.1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "between"):
                supervisor.load_settings(config)

    def test_microphone_controls_and_legacy_candidate_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                "[audio]\nmic_gain_db = -6.0\nmic_muted = true\n",
                encoding="utf-8",
            )
            settings = supervisor.load_settings(config)
        self.assertEqual(settings.mic_gain_db, -6.0)
        self.assertTrue(settings.mic_muted)
        self.assertEqual([item.id for item in settings.microphone_candidates], ["lark-a1"])

    def test_non_numeric_microphone_gain_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text('[audio]\nmic_gain_db = "loud"\n', encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "mic_gain_db"):
                supervisor.load_settings(config)


class ControllerBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = role_settings()
        self.call = role_adapter("hci4")
        self.onboard = role_adapter("hci2", ONBOARD_ADDRESS, bus="UART")
        self.inventory = [self.onboard, self.call]

    def tree(
        self,
        *connected: btadapters.Adapter,
        include_stale_onboard: bool = False,
    ) -> dict[str, dict]:
        tree = {
            btadapters.path_for(adapter, PHONE_ADDRESS): {
                "org.bluez.Device1": {"Connected": {"data": True}}
            }
            for adapter in connected
        }
        if include_stale_onboard and self.onboard not in connected:
            tree[btadapters.path_for(self.onboard, PHONE_ADDRESS)] = {
                "org.bluez.Device1": {
                    "Connected": {"data": False},
                    "Paired": {"data": True},
                }
            }
        return tree

    def test_call_binding_accepts_only_the_resolved_bt500(self) -> None:
        accepted, error = supervisor.call_role_acceptance(
            self.settings,
            self.tree(self.call, include_stale_onboard=True),
            self.inventory,
        )
        self.assertTrue(accepted)
        self.assertIsNone(error)

    def test_connected_onboard_or_duplicate_connections_fail_closed(self) -> None:
        wrong = supervisor.call_role_acceptance(
            self.settings, self.tree(self.onboard), self.inventory
        )
        duplicate = supervisor.call_role_acceptance(
            self.settings, self.tree(self.call, self.onboard), self.inventory
        )
        self.assertFalse(wrong[0])
        self.assertIn("not connected", wrong[1] or "")
        self.assertFalse(duplicate[0])
        self.assertIn(ONBOARD_ADDRESS, duplicate[1] or "")

    def test_wrong_usb_identity_rejects_call_binding_and_status(self) -> None:
        wrong_id = role_adapter("hci4", product_id="1d70")
        status, _, _ = supervisor.inspect_controller_roles(
            self.settings,
            objects=self.tree(wrong_id),
            inventory=[self.onboard, wrong_id],
        )
        accepted, error = supervisor.call_role_acceptance(
            self.settings, self.tree(wrong_id), [self.onboard, wrong_id]
        )
        self.assertFalse(status["ready"])
        self.assertIn("controller_identity_mismatch", status["call"]["error"])
        self.assertFalse(accepted)
        self.assertIn("controller_identity_mismatch", error or "")

    def test_status_preserves_ready_wired_output_record(self) -> None:
        status, tree, inventory = supervisor.inspect_controller_roles(
            self.settings,
            objects=self.tree(self.call),
            inventory=self.inventory,
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["call"]["configured_address"], CALL_ADDRESS)
        self.assertEqual(status["call"]["observed_usb_id"], "0b05:1bf6")
        self.assertEqual(status["call"]["hci"], "hci4")
        self.assertFalse(status["output"]["required"])
        self.assertTrue(status["output"]["ready"])
        self.assertEqual(status["output"]["reason"], "wired-output")
        self.assertEqual(tree, self.tree(self.call))
        self.assertEqual(inventory, self.inventory)

    def test_wrong_controller_hfp_nodes_are_hidden_from_the_graph(self) -> None:
        nodes = {
            self.settings.hfp_source: {},
            self.settings.hfp_sink: {},
            self.settings.lark_node: {},
            self.settings.wired_output: {},
        }
        filtered = supervisor.accepted_call_nodes(nodes, self.settings, False)
        self.assertNotIn(self.settings.hfp_source, filtered)
        self.assertNotIn(self.settings.hfp_sink, filtered)
        self.assertIn(self.settings.lark_node, filtered)
        self.assertIn(self.settings.wired_output, filtered)


class IdentityAndGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = supervisor.Settings(aec=supervisor.AecSettings())

    def test_lark_falls_back_to_stable_usb_component(self) -> None:
        nodes = {
            "renamed.lark": {
                "media.class": "Audio/Source",
                "alsa.components": "USB3547:0407",
            }
        }
        self.assertEqual(supervisor.find_lark(nodes, self.settings), "renamed.lark")

    def test_direct_raw_mic_and_default_downlink_are_rejected(self) -> None:
        links = [
            ("lark", "hfp-sink"),
            ("hfp-source", "default-output"),
            (supervisor.AEC_SOURCE, "input.bridge.mic"),
        ]
        unexpected = supervisor.unexpected_call_links(
            links,
            lark="lark",
            hfp_source="hfp-source",
            hfp_sink="hfp-sink",
            microphone_input="input.bridge.mic",
            callout_input="input.bridge.callout",
            aec_enabled=True,
        )
        self.assertEqual(unexpected, links[:2])


class NativeAecTests(unittest.TestCase):
    def test_module_is_explicit_fail_closed_and_pi3_optimized(self) -> None:
        host = supervisor.NativeAecHost(
            supervisor.AecSettings(enabled=True), "stable-lark", "stable-output"
        )
        command = host.module_command()
        self.assertIn('target.object = "stable-lark"', command)
        self.assertIn('target.object = "stable-output"', command)
        self.assertEqual(command.count("node.dont-reconnect = true"), 2)
        self.assertIn("audio.rate = 48000", command)
        self.assertIn("audio.channels = 1", command)
        self.assertIn("webrtc.noise_suppression = false", command)
        self.assertIn("webrtc.gain_control = false", command)
        self.assertIn("webrtc.high_pass_filter = true", command)
        self.assertIn("webrtc.voice_detection = false", command)
        self.assertIn("webrtc.transient_suppression = true", command)
        self.assertNotIn("buffer.play_delay", command)

    def test_module_tuning_is_explicit(self) -> None:
        settings = supervisor.AecSettings(
            enabled=True,
            high_pass_filter=False,
            noise_suppression=True,
            gain_control=True,
            transient_suppression=False,
        )
        command = supervisor.NativeAecHost(settings, "lark", "output").module_command()
        self.assertIn("webrtc.high_pass_filter = false", command)
        self.assertIn("webrtc.noise_suppression = true", command)
        self.assertIn("webrtc.gain_control = true", command)
        self.assertIn("webrtc.transient_suppression = false", command)

    def test_bench_latency_request_is_explicit(self) -> None:
        command = supervisor.NativeAecHost(
            supervisor.AecSettings(enabled=True),
            "lark",
            "output",
            latency_frames=1024,
        ).module_command()
        self.assertIn("node.latency = 1024/48000", command)

    def test_bench_reference_delay_is_explicit(self) -> None:
        command = supervisor.NativeAecHost(
            supervisor.AecSettings(enabled=True),
            "lark",
            "output",
            play_delay_frames=21600,
        ).module_command()
        self.assertIn("buffer.play_delay = 21600/48000", command)

    def test_bench_reference_delay_cannot_be_negative(self) -> None:
        with self.assertRaises(ValueError):
            supervisor.NativeAecHost(
                supervisor.AecSettings(enabled=True),
                "lark",
                "output",
                play_delay_frames=-1,
            )


class FakeHost:
    last: FakeHost | None = None

    def __init__(
        self,
        _settings: object,
        _microphone: str,
        _output: str,
        *,
        latency_frames: int | None = None,
        play_delay_frames: int | None = None,
    ):
        self.latency_frames = latency_frames
        self.play_delay_frames = play_delay_frames
        self.running = False
        self.pid = None
        FakeHost.last = self

    def start(self) -> None:
        self.running = True
        self.pid = 123

    def stop(self, _reason: str) -> None:
        self.running = False
        self.pid = None


class FakeMicrophoneResolution:
    def __init__(
        self,
        node: str | None,
        token: str | None,
        *,
        selected_id: str = "lark-a1",
        blocked: bool = False,
        candidates: list[dict] | None = None,
        reason: str = "test selection",
    ) -> None:
        self.node = node
        self.instance_token = token
        self.blocked = blocked
        self._report = {
            "selected": (
                {
                    "id": selected_id,
                    "label": selected_id,
                    "priority": 0,
                    "node": node,
                    "identity": {},
                    "format": None,
                    "instance_token": token,
                }
                if node is not None
                else None
            ),
            "selection_reason": reason,
            "blocked": blocked,
            "candidates": candidates or [],
        }

    def as_dict(self) -> dict:
        return self._report


class FakeLoopback:
    def __init__(self, name: str, capture: str, playback: str, _channels: int):
        self.name = name
        self.capture = capture
        self.playback = playback
        self.running = False

    @property
    def out_node(self) -> str:
        return f"output.{self.name}"

    @property
    def in_node(self) -> str:
        return f"input.{self.name}"

    def start(self) -> None:
        self.running = True

    def stop(self, _reason: str) -> None:
        self.running = False

    def targets_verified(self, links: supervisor.LinkList) -> bool:
        return (self.capture, self.in_node) in links and (
            self.out_node,
            self.playback,
        ) in links


class WiredVolumeTests(unittest.TestCase):
    def nodes(self, settings: supervisor.Settings) -> dict[str, dict]:
        return {
            settings.lark_node: {},
            settings.wired_output: {},
            settings.hfp_source: {},
            settings.hfp_sink: {},
        }

    def test_pactl_set_is_followed_by_numeric_verification(self) -> None:
        replies = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(
                returncode=0,
                stdout=(
                    "Volume: front-left: 55705 / 85% / -4.22 dB, "
                    "front-right: 55705 / 85% / -4.22 dB\n"
                ),
                stderr="",
            ),
        ]
        with mock.patch.object(supervisor.subprocess, "run", side_effect=replies) as run:
            ok, observed, error = supervisor.set_and_verify_sink_volume("alsa_output.test", 0.85)
        self.assertTrue(ok)
        self.assertEqual(observed, 0.85)
        self.assertIsNone(error)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["pactl", "set-sink-volume", "alsa_output.test", "85.00%"],
        )

    def test_failed_volume_verification_holds_graph_safe_and_unbuilt(self) -> None:
        settings = role_settings(volume=0.85)
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(
                supervisor,
                "set_and_verify_sink_volume",
                return_value=(False, 1.0, "volume mismatch"),
            ),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
        ):
            graph.tick(self.nodes(settings), [], settings.lark_node)
        self.assertEqual(graph.state, supervisor.State.SAFE)
        self.assertEqual(graph.output_volume_observed, 1.0)
        self.assertFalse(graph.output_volume_verified)
        self.assertEqual(graph.output_volume_error, "volume mismatch")
        self.assertIsNone(graph.microphone)
        self.assertIsNone(graph.callout)

    def test_verified_volume_precedes_build_and_is_reported(self) -> None:
        settings = role_settings(volume=0.85)
        graph = supervisor.CallGraph(settings)
        events: list[str] = []

        class OrderedLoopback(FakeLoopback):
            def start(self) -> None:
                events.append("build")
                super().start()

        def verify(_target: str, _desired: float):
            events.append("volume")
            return True, 0.85, None

        with (
            mock.patch.object(supervisor, "set_and_verify_sink_volume", side_effect=verify),
            mock.patch.object(supervisor, "Loopback", OrderedLoopback),
        ):
            graph.tick(self.nodes(settings), [], settings.lark_node)
        self.assertEqual(events[0], "volume")
        self.assertEqual(graph.state, supervisor.State.BUILDING)
        report = graph.status(self.nodes(settings), [], settings.lark_node)
        self.assertEqual(
            report["wired_output_volume"],
            {
                "required": True,
                "target": settings.wired_output,
                "desired": 0.85,
                "observed": 0.85,
                "verified": True,
                "error": None,
            },
        )

    def test_call_down_applies_and_retains_verified_wired_volume(self) -> None:
        settings = role_settings(volume=0.85)
        graph = supervisor.CallGraph(settings)
        nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
        }

        with mock.patch.object(
            supervisor,
            "set_and_verify_sink_volume",
            return_value=(True, 0.85, None),
        ) as verify:
            graph.tick(nodes, [], settings.lark_node)
            graph.tick(nodes, [], settings.lark_node)

        self.assertEqual(graph.state, supervisor.State.CALL_DOWN)
        verify.assert_called_once_with(settings.wired_output, 0.85)
        self.assertEqual(
            graph.status(nodes, [], settings.lark_node)["wired_output_volume"],
            {
                "required": True,
                "target": settings.wired_output,
                "desired": 0.85,
                "observed": 0.85,
                "verified": True,
                "error": None,
            },
        )


class CallGraphLifecycleTests(unittest.TestCase):
    def test_aec_graph_is_atomic_and_tears_down_with_hfp(self) -> None:
        settings = supervisor.Settings(aec=supervisor.AecSettings(enabled=True))
        graph = supervisor.CallGraph(settings)
        base_nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
            settings.hfp_source: {},
            settings.hfp_sink: {},
        }

        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            graph.tick(base_nodes, [], settings.lark_node)
            self.assertEqual(graph.state, supervisor.State.BUILDING)
            self.assertIsNotNone(graph.aec_host)

            aec_nodes = dict(base_nodes)
            aec_nodes[supervisor.AEC_SOURCE] = {}
            aec_nodes[supervisor.AEC_SINK] = {}
            graph.tick(aec_nodes, [], settings.lark_node)
            self.assertIsNotNone(graph.microphone)
            self.assertIsNotNone(graph.callout)

            assert graph.microphone is not None and graph.callout is not None
            graph.routes_started -= supervisor.ATTACH_GRACE_SECONDS + 1
            links = [
                (settings.lark_node, supervisor.AEC_CAPTURE),
                (supervisor.AEC_PLAYBACK, settings.wired_output),
                (graph.microphone.capture, graph.microphone.in_node),
                (graph.microphone.out_node, graph.microphone.playback),
                (graph.callout.capture, graph.callout.in_node),
                (graph.callout.out_node, graph.callout.playback),
            ]
            graph.tick(aec_nodes, links, settings.lark_node)
            self.assertEqual(graph.state, supervisor.State.ACTIVE)
            self.assertTrue(graph.verified)

            no_call = {
                settings.lark_node: {},
                settings.wired_output: {},
            }
            graph.tick(no_call, [], settings.lark_node)
            self.assertEqual(graph.state, supervisor.State.CALL_DOWN)
            self.assertIsNone(graph.aec_host)
            self.assertIsNone(graph.microphone)
            self.assertIsNone(graph.callout)

    def test_aec_failure_never_builds_raw_lark_uplink(self) -> None:
        settings = supervisor.Settings(aec=supervisor.AecSettings(enabled=True))
        graph = supervisor.CallGraph(settings)
        nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
            settings.hfp_source: {},
            settings.hfp_sink: {},
        }
        with mock.patch.object(supervisor, "NativeAecHost", FakeHost):
            graph.tick(nodes, [], settings.lark_node)
        self.assertIsNone(graph.microphone)
        self.assertNotEqual(graph.state, supervisor.State.ACTIVE)


class PlaybackTimingTests(unittest.TestCase):
    """The AEC graph timing that decides whether the onboard jack crackles.

    Left unset, the WebRTC module asks for a 480-frame quantum and PipeWire drops the
    onboard sink to min-quantum 256, which underruns under call load. Measured on the
    unit: echo-cancel-playback logged 417 underruns in 20 s unset versus 5 at 1920.
    """

    def test_default_is_the_validated_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = supervisor.load_settings(Path(directory) / "missing.toml")
        self.assertEqual(settings.aec.node_latency_frames, 1920)
        self.assertIsNone(settings.aec.play_delay_frames)

    def test_production_module_command_pins_the_quantum(self) -> None:
        """The regression that caused the crackle: this was bench-only before."""
        settings = supervisor.Settings(aec=supervisor.AecSettings(enabled=True))
        graph = supervisor.CallGraph(settings)
        with mock.patch.object(supervisor, "NativeAecHost", FakeHost):
            graph.begin_build("lark")
        assert FakeHost.last is not None
        self.assertEqual(FakeHost.last.latency_frames, 1920)

    def test_configured_timing_reaches_the_module_arguments(self) -> None:
        command = supervisor.NativeAecHost(
            supervisor.AecSettings(enabled=True),
            "lark",
            "output",
            latency_frames=supervisor.AecSettings().node_latency_frames,
        ).module_command()
        self.assertIn("node.latency = 1920/48000", command)

    def test_config_can_override_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                """
[audio.aec]
enabled = true
node_latency_frames = 1440
play_delay_frames = 480
""",
                encoding="utf-8",
            )
            settings = supervisor.load_settings(config)
        self.assertEqual(settings.aec.node_latency_frames, 1440)
        self.assertEqual(settings.aec.play_delay_frames, 480)

    def test_nonsense_timing_is_rejected(self) -> None:
        bodies = (
            """
[audio.aec]
enabled = true
node_latency_frames = 0
""",
            """
[audio.aec]
enabled = true
play_delay_frames = -1
""",
        )
        for body in bodies:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "bridge.toml"
                config.write_text(body, encoding="utf-8")
                with self.assertRaises(ValueError):
                    supervisor.load_settings(config)

    def test_timing_must_be_an_integer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.toml"
            config.write_text(
                """
[audio.aec]
enabled = true
node_latency_frames = "1920"
""",
                encoding="utf-8",
            )
            with self.assertRaises(TypeError):
                supervisor.load_settings(config)


class OutputSelectionTests(unittest.TestCase):
    """Output identity is enforced, and an in-progress graph never keeps a stale target.

    This is a regression test for a defect that would have been invisible. The rebuild
    signature used to carry `output_up`, a bool meaning "some output is present". With one
    hardcoded output that was fine. With a selectable output it is wrong in the worst
    possible way: moving from the wired jack to a speaker leaves the bool True on both
    sides, so update_signature() sees no change, so nothing is torn down or rebuilt -- and
    the status file still reports ACTIVE and verified. Audio would keep coming out of the
    old device while every observable said the switch had worked.
    """

    SPEAKER = "bluez_output.C9_5C_FD_6E_28_46.1"

    def _graph(self) -> supervisor.CallGraph:
        settings = supervisor.Settings(aec=supervisor.AecSettings(enabled=False))
        return supervisor.CallGraph(settings)

    def _nodes(self, graph: supervisor.CallGraph) -> dict:
        return {
            "lark": {},
            graph.settings.hfp_sink: {},
            graph.settings.hfp_source: {},
            graph.settings.wired_output: {},
            self.SPEAKER: {},
        }

    def test_changing_output_during_build_restarts_that_build(self) -> None:
        graph = self._graph()
        nodes = self._nodes(graph)
        with mock.patch.object(supervisor, "Loopback", FakeLoopback):
            graph.tick(nodes, [], "lark", graph.settings.wired_output)
            first = graph.generation
            graph.tick(nodes, [], "lark", self.SPEAKER)
        self.assertGreater(graph.generation, first, "an output switch must rebuild the graph")
        self.assertEqual(graph.output_node, self.SPEAKER)

    def test_same_output_does_not_churn_the_graph(self) -> None:
        """The other half: a steady output must NOT rebuild on every poll."""
        graph = self._graph()
        nodes = self._nodes(graph)
        with mock.patch.object(supervisor, "Loopback", FakeLoopback):
            graph.tick(nodes, [], "lark", self.SPEAKER)
            first = graph.generation
            graph.tick(nodes, [], "lark", self.SPEAKER)
        self.assertEqual(graph.generation, first)

    def test_no_output_is_discovering_not_active(self) -> None:
        graph = self._graph()
        graph.tick(self._nodes(graph), [], "lark", None)
        self.assertEqual(graph.state, supervisor.State.DISCOVERING)

    def test_the_callout_targets_the_selected_output(self) -> None:
        graph = self._graph()
        with mock.patch.object(supervisor, "Loopback", FakeLoopback):
            graph.tick(self._nodes(graph), [], "lark", self.SPEAKER)
        self.assertIsNotNone(graph.callout)
        self.assertEqual(graph.callout.playback, self.SPEAKER)

    def test_omitting_the_output_keeps_the_preselection_behaviour(self) -> None:
        graph = self._graph()
        with mock.patch.object(supervisor, "Loopback", FakeLoopback):
            graph.tick(self._nodes(graph), [], "lark")
        self.assertEqual(graph.output_node, graph.settings.wired_output)


class LiveOutputSwitchTests(unittest.TestCase):
    OLD = "alsa_output.platform-old.mailbox"
    NEW = "bluez_output.C9_5C_FD_6E_28_46.1"

    def active_graph(self, *, aec: bool) -> tuple[supervisor.CallGraph, str]:
        settings = supervisor.Settings(aec=supervisor.AecSettings(enabled=aec))
        graph = supervisor.CallGraph(settings)
        lark = settings.lark_node
        graph.signature = (True, lark)
        graph.output_node = self.OLD
        graph.state = supervisor.State.ACTIVE
        graph.verified = True
        graph.generation = 1
        graph.microphone = FakeLoopback(
            "bridge.mic", supervisor.AEC_SOURCE if aec else lark, settings.hfp_sink, 1
        )
        graph.callout = FakeLoopback(
            "bridge.callout", settings.hfp_source, supervisor.AEC_SINK if aec else self.OLD, 1
        )
        graph.microphone.start()
        graph.callout.start()
        if aec:
            graph.aec_host = FakeHost(settings.aec, lark, self.OLD)
            graph.aec_host.start()
        return graph, lark

    def links_for(self, graph: supervisor.CallGraph, lark: str, output: str):
        assert graph.microphone is not None and graph.callout is not None
        links = [
            (graph.microphone.capture, graph.microphone.in_node),
            (graph.microphone.out_node, graph.microphone.playback),
            (graph.callout.capture, graph.callout.in_node),
            (graph.callout.out_node, graph.callout.playback),
        ]
        if graph.settings.aec.enabled:
            links.extend(
                [
                    (lark, supervisor.AEC_CAPTURE),
                    (supervisor.AEC_PLAYBACK, output),
                ]
            )
        return links

    def nodes_for(self, graph: supervisor.CallGraph, lark: str) -> dict:
        nodes = {
            lark: {},
            graph.settings.hfp_source: {},
            graph.settings.hfp_sink: {},
            self.OLD: {},
            self.NEW: {},
        }
        if graph.settings.aec.enabled:
            nodes[supervisor.AEC_SOURCE] = {}
            nodes[supervisor.AEC_SINK] = {}
        return nodes

    def test_aec_switch_is_make_before_break_and_keeps_uplink_alive(self) -> None:
        graph, lark = self.active_graph(aec=True)
        microphone = graph.microphone
        host = graph.aec_host
        events: list[tuple[str, str, str]] = []
        with (
            mock.patch.object(
                supervisor,
                "link",
                side_effect=lambda source, target: events.append(("link", source, target)) or True,
            ),
            mock.patch.object(
                supervisor,
                "unlink",
                side_effect=lambda source, target: events.append(("unlink", source, target))
                or True,
            ),
            mock.patch.object(supervisor, "set_aec_mute") as mute,
        ):
            graph.tick(
                self.nodes_for(graph, lark),
                self.links_for(graph, lark, self.OLD),
                lark,
                self.NEW,
            )

        self.assertEqual(
            events,
            [
                ("link", supervisor.AEC_PLAYBACK, self.NEW),
                ("unlink", supervisor.AEC_PLAYBACK, self.OLD),
            ],
        )
        self.assertIs(graph.microphone, microphone)
        self.assertTrue(graph.microphone.running)
        self.assertIs(graph.aec_host, host)
        self.assertTrue(graph.aec_host.running)
        self.assertEqual(graph.output_node, self.NEW)
        self.assertEqual(graph.state, supervisor.State.SWITCHING)
        mute.assert_not_called()

    def test_non_aec_switch_retargets_only_downlink_loopback(self) -> None:
        graph, _lark = self.active_graph(aec=False)
        microphone = graph.microphone
        assert graph.callout is not None
        source = graph.callout.out_node
        with (
            mock.patch.object(supervisor, "link", return_value=True) as create,
            mock.patch.object(supervisor, "unlink", return_value=True) as remove,
        ):
            self.assertTrue(graph.switch_output_live(self.NEW))
        create.assert_called_once_with(source, self.NEW)
        remove.assert_called_once_with(source, self.OLD)
        self.assertIs(graph.microphone, microphone)
        self.assertTrue(graph.microphone.running)
        self.assertEqual(graph.callout.playback, self.NEW)

    def test_failed_break_rolls_back_new_link(self) -> None:
        graph, _lark = self.active_graph(aec=False)
        assert graph.callout is not None
        source = graph.callout.out_node
        with (
            mock.patch.object(supervisor, "link", return_value=True),
            mock.patch.object(supervisor, "unlink", side_effect=[False, True]) as remove,
        ):
            self.assertFalse(graph.switch_output_live(self.NEW))
        self.assertEqual(
            remove.call_args_list,
            [mock.call(source, self.OLD), mock.call(source, self.NEW)],
        )
        self.assertEqual(graph.output_node, self.OLD)


class OutputWakeTests(unittest.TestCase):
    def test_output_file_change_wakes_before_the_full_poll(self) -> None:
        with (
            mock.patch.object(supervisor, "desire_stamp", side_effect=[10, 11]),
            mock.patch.object(supervisor.time, "monotonic", return_value=0.0),
            mock.patch.object(supervisor.time, "sleep") as sleep,
        ):
            supervisor.wait_for_next_tick(10, lambda: False)
        sleep.assert_called_once_with(supervisor.OUTPUT_EVENT_POLL_SECONDS)

    def test_lark_liveness_change_wakes_before_the_full_poll(self) -> None:
        with (
            mock.patch.object(supervisor, "desire_stamp", return_value=10),
            mock.patch.object(supervisor.time, "monotonic", return_value=0.0),
            mock.patch.object(supervisor.time, "sleep") as sleep,
        ):
            supervisor.wait_for_next_tick(10, lambda: False, wake=lambda: True)
        sleep.assert_not_called()


class LarkPcmLivenessTests(unittest.TestCase):
    @staticmethod
    def candidates_and_sources(*, generation: str = "lark@1") -> tuple:
        lark = microphones.MicrophoneCandidate(
            id="lark-a1",
            label="Hollyland Lark A1",
            node_name=supervisor.DEFAULT_LARK,
            usb_vendor_id="3547",
            usb_product_id="0407",
            usb_product="Wireless Microphone",
            required_rate=48_000,
            required_format="S16LE",
            required_channels=2,
            capture_only=True,
        )
        fifine_node = "alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback"
        fifine = microphones.MicrophoneCandidate(
            id="fifine-k054",
            label="FIFINE K054",
            node_name=fifine_node,
            usb_vendor_id="0c76",
            usb_product_id="161e",
            usb_product="USB PnP Audio Device",
            required_rate=48_000,
            required_format="S16LE",
            required_channels=1,
            capture_only=True,
        )
        lark_source = microphones.ObservedSource(
            node=supervisor.DEFAULT_LARK,
            pipewire_id="41",
            pipewire_object_serial=generation,
            device_id="40",
            device_object_serial=generation,
            alsa_components=("USB3547:0407",),
            usb_vendor_id="3547",
            usb_product_id="0407",
            usb_product="Wireless Microphone",
            usb_instance_generation=generation,
            formats=(microphones.MicrophoneFormat(48_000, "S16LE", 2),),
            device_has_playback=False,
        )
        fifine_source = microphones.ObservedSource(
            node=fifine_node,
            pipewire_id="51",
            pipewire_object_serial="fifine@1",
            device_id="50",
            device_object_serial="fifine@1",
            alsa_components=("USB0C76:161E",),
            usb_vendor_id="0c76",
            usb_product_id="161e",
            usb_product="USB PnP Audio Device",
            usb_instance_generation="fifine@1",
            formats=(microphones.MicrophoneFormat(48_000, "S16LE", 1),),
            device_has_playback=False,
        )
        return (lark, fifine), (lark_source, fifine_source)

    def test_policy_requires_explicit_first_priority_lark_and_any_fallback(self) -> None:
        candidates, _sources = self.candidates_and_sources()
        k053 = mock.Mock(id="fifine-k053", legacy=False)
        self.assertTrue(supervisor.automatic_lark_liveness_enabled(candidates))
        self.assertTrue(
            supervisor.automatic_lark_liveness_enabled((candidates[0], k053, candidates[1]))
        )
        self.assertFalse(supervisor.automatic_lark_liveness_enabled(candidates[:1]))
        self.assertFalse(
            supervisor.automatic_lark_liveness_enabled(
                (mock.Mock(id="lark-a1", legacy=True), candidates[1])
            )
        )
        self.assertFalse(
            supervisor.automatic_lark_liveness_enabled((k053, candidates[0]))
        )

    def test_exact_zero_is_inactive_and_any_nonzero_bit_is_active(self) -> None:
        states = {
            "tx1": b"\x01\x00\x00\x00",
            "tx2": b"\x00\x00\x01\x00",
            "both": b"\x01\x00\x01\x00",
        }
        for label, window in states.items():
            with self.subTest(label=label):
                detector = supervisor.PcmActivityDebouncer(
                    4,
                    active_windows=1,
                    inactive_windows=1,
                )
                detector.feed(window)
                self.assertEqual(detector.state, "active")

        detector = supervisor.PcmActivityDebouncer(
            4,
            active_windows=1,
            inactive_windows=1,
        )
        self.assertFalse(detector.feed(b""))
        self.assertEqual(detector.state, "unknown")
        detector.feed(b"\x00" * 4)
        self.assertEqual(detector.state, "inactive")

    def test_presence_and_loss_require_consecutive_windows(self) -> None:
        detector = supervisor.PcmActivityDebouncer(
            4,
            active_windows=2,
            inactive_windows=3,
        )
        detector.feed(b"\x01\x00\x00\x00")
        self.assertEqual(detector.state, "unknown")
        detector.feed(b"\x01\x00\x00\x00")
        self.assertEqual(detector.state, "active")

        detector.feed(b"\x00" * 8)
        self.assertEqual(detector.state, "active")
        detector.feed(b"\x01\x00\x00\x00")
        detector.feed(b"\x00" * 8)
        self.assertEqual(detector.state, "active")
        detector.feed(b"\x00" * 4)
        self.assertEqual(detector.state, "inactive")

    def test_monitor_remains_bound_to_lark_while_final_selection_is_fifine(self) -> None:
        candidates, sources = self.candidates_and_sources()
        physical = microphones.resolve(candidates, sources)
        proc = mock.MagicMock()
        proc.poll.return_value = None
        proc.stdout.fileno.return_value = 7
        proc.pid = 123
        popen = mock.Mock(return_value=proc)
        monitor = supervisor.LarkPcmLivenessMonitor(
            popen_factory=popen,
            clock=lambda: 0.0,
        )
        with (
            mock.patch.object(supervisor.os, "set_blocking"),
            mock.patch.object(supervisor.os, "read", side_effect=BlockingIOError),
        ):
            availability = monitor.reconcile(physical, [], enabled=True)
            final = microphones.resolve(
                candidates,
                sources,
                {availability.candidate_id: availability},
            )
            monitor.reconcile(
                physical,
                [(supervisor.DEFAULT_LARK, supervisor.LARK_PCM_MONITOR_NODE)],
                enabled=True,
            )
        self.assertEqual(final.selected.candidate.id, "fifine-k054")
        self.assertEqual(availability.state, "unknown")
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--target") + 1], supervisor.DEFAULT_LARK)
        self.assertIn("node.dont-reconnect=true", command[command.index("--properties") + 1])
        monitor.close()

    def test_active_pcm_cannot_promote_lark_before_exact_link_verification(self) -> None:
        candidates, sources = self.candidates_and_sources()
        physical = microphones.resolve(candidates, sources)
        now = [0.0]
        proc = mock.MagicMock()
        proc.poll.return_value = None
        proc.stdout.fileno.return_value = 7
        monitor = supervisor.LarkPcmLivenessMonitor(
            popen_factory=mock.Mock(return_value=proc),
            clock=lambda: now[0],
        )
        with (
            mock.patch.object(supervisor.os, "set_blocking"),
            mock.patch.object(supervisor.os, "read", side_effect=BlockingIOError),
        ):
            monitor.reconcile(physical, [], enabled=True)
            pcm_window = b"\x01" + b"\x00" * 3_839
            monitor.feed_pcm(pcm_window * supervisor.LARK_PCM_ACTIVE_WINDOWS)
            now[0] = 0.36
            unverified = monitor.reconcile(physical, [], enabled=True)
            fallback = microphones.resolve(
                candidates,
                sources,
                {unverified.candidate_id: unverified},
            )
            now[0] = 0.51
            verified = monitor.reconcile(
                physical,
                [(supervisor.DEFAULT_LARK, supervisor.LARK_PCM_MONITOR_NODE)],
                enabled=True,
            )
            promoted = microphones.resolve(
                candidates,
                sources,
                {verified.candidate_id: verified},
            )
        self.assertEqual(unverified.state, "unknown")
        self.assertEqual(fallback.selected.candidate.id, "fifine-k054")
        self.assertEqual(verified.state, "active")
        self.assertEqual(promoted.selected.candidate.id, "lark-a1")
        monitor.close()

    def test_same_node_new_generation_restarts_monitor_as_unknown(self) -> None:
        candidates, sources = self.candidates_and_sources(generation="lark@1")
        first = microphones.resolve(candidates, sources)
        _same_candidates, replacement_sources = self.candidates_and_sources(generation="lark@2")
        replacement = microphones.resolve(candidates, replacement_sources)
        first_proc = mock.MagicMock()
        first_proc.poll.return_value = None
        first_proc.stdout.fileno.return_value = 7
        second_proc = mock.MagicMock()
        second_proc.poll.return_value = None
        second_proc.stdout.fileno.return_value = 8
        monitor = supervisor.LarkPcmLivenessMonitor(
            popen_factory=mock.Mock(side_effect=[first_proc, second_proc]),
            clock=lambda: 0.0,
        )
        with mock.patch.object(supervisor.os, "set_blocking"):
            before = monitor.reconcile(first, [], enabled=True)
            after = monitor.reconcile(replacement, [], enabled=True)
        self.assertNotEqual(before.instance_token, after.instance_token)
        self.assertEqual(after.state, "unknown")
        first_proc.terminate.assert_called_once()
        monitor.close()


class DesireFileTests(unittest.TestCase):
    """Runtime selection is a file on tmpfs, so it costs no LARKDATA writes."""

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge-output.json"
            self.assertIsNone(supervisor.read_desire(path))
            supervisor.write_desire("a2dp:C9:5C:FD:6E:28:46", "cli", path)
            self.assertEqual(supervisor.read_desire(path), "a2dp:C9:5C:FD:6E:28:46")

    def test_clearing_reverts_to_the_configured_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge-output.json"
            supervisor.write_desire("a2dp:AA:BB:CC:DD:EE:FF", "cli", path)
            supervisor.write_desire(None, "cli", path)
            self.assertIsNone(supervisor.read_desire(path))

    def test_corrupt_file_reads_as_no_desire_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge-output.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(supervisor.read_desire(path))


class FailedIsNotTerminalTests(unittest.TestCase):
    """FAILED must not strand a live call.

    E13 measured the old behaviour: five AEC host deaths in a burst left the unit
    permanently dead with the call still up, the far end arriving on the downlink at
    -12 dBFS, and the speaker at -200 dBFS. update_signature() only resets `attempts`
    when (call_up, lark, output_up) changes, and nothing had changed, so nothing ever
    retried.
    """

    # (call_up, lark) -- the endpoint tuple tick() computes. Pre-set it so the first
    # tick does not look like a fresh generation and reset attempts, which is what a real
    # supervisor mid-call would already have done.
    #
    # Output identity deliberately is not part of this tuple now: an output-only change is
    # retargeted live and must not reset endpoint failure history or tear down the uplink.
    SIGNATURE = (True, "lark")

    def _failed_graph(self) -> supervisor.CallGraph:
        settings = supervisor.Settings(aec=supervisor.AecSettings(enabled=True))
        graph = supervisor.CallGraph(settings)
        graph.signature = self.SIGNATURE
        for _ in range(supervisor.MAX_BUILD_ATTEMPTS):
            graph.fail("native AEC owner exited")
        return graph

    def test_reaching_failed_still_schedules_a_retry(self) -> None:
        graph = self._failed_graph()
        self.assertEqual(graph.state, supervisor.State.FAILED)
        self.assertGreater(
            graph.next_attempt, 0.0, "FAILED must schedule a retry, not stop forever"
        )

    def test_failed_does_not_retry_immediately(self) -> None:
        """The cap exists to stop a hot rebuild loop; that must survive the fix."""
        graph = self._failed_graph()
        nodes = {
            "lark": {},
            graph.settings.hfp_sink: {},
            graph.settings.hfp_source: {},
            graph.settings.wired_output: {},
        }
        with mock.patch.object(supervisor, "NativeAecHost", FakeHost):
            graph.tick(nodes, [], "lark")
        self.assertEqual(graph.state, supervisor.State.FAILED)
        self.assertEqual(graph.attempts, supervisor.MAX_BUILD_ATTEMPTS)

    def test_failed_retries_once_the_interval_elapses(self) -> None:
        graph = self._failed_graph()
        graph.next_attempt = 0.0  # pretend FAILED_RETRY_SECONDS has passed
        nodes = {
            "lark": {},
            graph.settings.hfp_sink: {},
            graph.settings.hfp_source: {},
            graph.settings.wired_output: {},
        }
        with mock.patch.object(supervisor, "NativeAecHost", FakeHost):
            graph.tick(nodes, [], "lark")
        self.assertNotEqual(
            graph.state,
            supervisor.State.FAILED,
            "FAILED must be escapable without a signature change",
        )
        self.assertEqual(graph.attempts, 0)


class EarlyAutolinkSweepTests(unittest.TestCase):
    """The session manager wires a source to the HFP sink the instant it appears.

    E13 measured the consequence: when the Lark came back mid-call, WirePlumber linked it
    straight to bluez_output and the supervisor left that standing for 6.4 s -- raw
    un-cancelled mic audio to the far end, and a closed acoustic loop through the speaker.
    The sweep must therefore run before any build logic, not after the graph is finished.
    """

    def _graph(self) -> supervisor.CallGraph:
        settings = supervisor.Settings(aec=supervisor.AecSettings(enabled=True))
        return supervisor.CallGraph(settings)

    def test_lark_to_hfp_sink_is_cut_during_building(self) -> None:
        graph = self._graph()
        nodes = {
            "lark": {},
            graph.settings.hfp_sink: {},
            graph.settings.hfp_source: {},
            graph.settings.wired_output: {},
        }
        dangerous = [("lark", graph.settings.hfp_sink)]
        removed: list[tuple[str, str]] = []
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "unlink", lambda s, t: removed.append((s, t))),
        ):
            graph.tick(nodes, dangerous, "lark")
        self.assertIn(
            ("lark", graph.settings.hfp_sink),
            removed,
            "the raw Lark uplink must be cut on the same tick it appears, not after the build",
        )

    def test_sweep_waits_for_a_clean_snapshot_before_build(self) -> None:
        graph = self._graph()
        nodes = {
            "lark": {},
            graph.settings.hfp_sink: {},
            graph.settings.hfp_source: {},
            graph.settings.wired_output: {},
        }
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "unlink", lambda s, t: None),
        ):
            graph.tick(nodes, [("lark", graph.settings.hfp_sink)], "lark")
            self.assertIsNone(graph.aec_host)
            graph.tick(nodes, [], "lark")
        self.assertIsNotNone(graph.aec_host)

    def test_sweep_uses_raw_hfp_presence_when_controller_rejects_call(self) -> None:
        graph = self._graph()
        # accepted_call_nodes deliberately hid both HFP endpoints, but the raw
        # PipeWire snapshot still contained the sink and an unsafe microphone link.
        accepted_nodes = {
            "lark": {},
            graph.settings.wired_output: {},
        }
        dangerous = [("lark", graph.settings.hfp_sink)]
        removed: list[tuple[str, str]] = []
        with mock.patch.object(
            supervisor,
            "unlink",
            lambda source, target: removed.append((source, target)),
        ):
            graph.tick(
                accepted_nodes,
                dangerous,
                "lark",
                raw_hfp_sink_present=True,
            )
        self.assertEqual(removed, dangerous)
        self.assertEqual(graph.state, supervisor.State.CALL_DOWN)
        self.assertIsNone(graph.microphone)


class MicrophoneDiscoveryAndControlTests(unittest.TestCase):
    def test_usb_serial_does_not_fall_back_to_pipewire_device_serial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sysfs_root = Path(directory) / "sys"
            usb_parent = sysfs_root / "devices" / "usb1" / "1-1" / "1-1.2"
            usb_parent.mkdir(parents=True)
            (usb_parent / "idVendor").write_text("0c76\n", encoding="utf-8")
            (usb_parent / "idProduct").write_text("161e\n", encoding="utf-8")
            (usb_parent / "product").write_text("USB PnP Audio Device\n", encoding="utf-8")
            (usb_parent / "devnum").write_text("7\n", encoding="utf-8")
            device = {
                "id": 8,
                "type": "PipeWire:Interface:Device",
                "info": {
                    "props": {
                        "object.serial": 108,
                        "device.sysfs.path": "/devices/usb1/1-1/1-1.2",
                        "device.vendor.id": "usb:ffff",
                        "device.product.id": "usb:ffff",
                        "device.product.name": "synthetic product",
                        "device.serial": "0c76_USB_PnP_Audio_Device",
                    }
                },
            }

            facts = supervisor.microphone_sysfs_by_device([device], sysfs_root=sysfs_root)["8"]

        self.assertEqual(facts["usb_vendor_id"], "0c76")
        self.assertEqual(facts["usb_product_id"], "161e")
        self.assertEqual(facts["usb_product"], "USB PnP Audio Device")
        self.assertIsNone(facts["usb_serial"])
        self.assertEqual(facts["usb_port_path"], "1-1.2")
        self.assertEqual(facts["usb_instance_generation"], "1-1.2@7")

    def test_discovery_joins_device_identity_and_structured_capabilities(self) -> None:
        candidate = microphones.MicrophoneCandidate(
            id="fifine-k054",
            label="FIFINE K054",
            node_name="fifine",
            usb_vendor_id="0c76",
            usb_product_id="161e",
            usb_product="USB PnP Audio Device",
            required_rate=48000,
            required_format="S16LE",
            required_channels=1,
            capture_only=True,
        )
        device = {
            "id": 8,
            "type": "PipeWire:Interface:Device",
            "info": {
                "props": {
                    "object.serial": 108,
                    "device.sysfs.path": "/devices/usb1/1-1/1-1.2",
                    "device.vendor.id": "usb:0c76",
                    "device.product.id": "usb:161e",
                    "device.product.name": "USB PnP Audio Device",
                }
            },
        }
        node = {
            "id": 19,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "node.name": "fifine",
                    "media.class": "Audio/Source",
                    "object.serial": 119,
                    "device.id": 8,
                    "alsa.components": "USB0C76:161E",
                },
                "params": {
                    "EnumFormat": [
                        {
                            "mediaType": "audio",
                            "mediaSubtype": "raw",
                            "format": "S16LE",
                            "rate": 48000,
                            "channels": 1,
                        }
                    ]
                },
            },
        }
        settings = supervisor.Settings(
            aec=supervisor.AecSettings(),
            microphone_candidates=(candidate,),
        )
        with tempfile.TemporaryDirectory() as directory:
            sysfs_root = Path(directory) / "sys"
            usb_parent = sysfs_root / "devices" / "usb1" / "1-1" / "1-1.2"
            usb_parent.mkdir(parents=True)
            (usb_parent / "idVendor").write_text("0c76\n", encoding="utf-8")
            (usb_parent / "idProduct").write_text("161e\n", encoding="utf-8")
            (usb_parent / "product").write_text("USB PnP Audio Device\n", encoding="utf-8")
            (usb_parent / "devnum").write_text("7\n", encoding="utf-8")
            with mock.patch.object(supervisor.subprocess, "run") as run:
                observations, resolution = supervisor.discover_microphones(
                    [node, device],
                    settings,
                    capability_cache={},
                    sysfs_root=sysfs_root,
                )
        self.assertEqual([source.node for source in observations], ["fifine"])
        self.assertEqual(resolution.node, "fifine")
        run.assert_not_called()

    def test_enum_format_parser_extracts_required_tuple(self) -> None:
        output = """
        Prop: key Spa:Pod:Object:Param:Format:Audio:format (65537)
          Id 259 (Spa:Enum:AudioFormat:S16LE)
        Prop: key Spa:Pod:Object:Param:Format:Audio:rate (65539)
          Int 48000
        Prop: key Spa:Pod:Object:Param:Format:Audio:channels (65540)
          Int 1
        """
        self.assertEqual(
            supervisor.parse_enum_format_output(output),
            ({"rate": 48000, "format": "S16LE", "channels": 1},),
        )

    def test_structured_pw_dump_enum_format_avoids_an_extra_query(self) -> None:
        node = {
            "id": 19,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "node.name": "fifine",
                    "media.class": "Audio/Source",
                    "object.serial": 119,
                    "device.id": 8,
                },
                "params": {
                    "EnumFormat": [
                        {
                            "mediaType": "audio",
                            "mediaSubtype": "raw",
                            "format": {"default": "S16LE", "alt1": "S16LE"},
                            "rate": {"default": 48000, "min": 48000, "max": 48000},
                            "channels": 1,
                        }
                    ]
                },
            },
        }
        device = {
            "id": 8,
            "type": "PipeWire:Interface:Device",
            "info": {"props": {"object.serial": 108}},
        }
        with mock.patch.object(supervisor.subprocess, "run") as run:
            found = supervisor.microphone_capabilities_by_node([device, node], cache={})
        self.assertEqual(
            found["fifine"],
            ({"rate": 48000, "format": "S16LE", "channels": 1},),
        )
        run.assert_not_called()

    def test_failed_capability_query_is_not_cached(self) -> None:
        node = {
            "id": 19,
            "type": "PipeWire:Interface:Node",
            "info": {
                "props": {
                    "node.name": "fifine",
                    "media.class": "Audio/Source",
                    "object.serial": 119,
                    "device.id": 8,
                }
            },
        }
        device = {
            "id": 8,
            "type": "PipeWire:Interface:Device",
            "info": {"props": {"object.serial": 108}},
        }
        cache: dict = {}
        with mock.patch.object(
            supervisor.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="unavailable"),
        ):
            found = supervisor.microphone_capabilities_by_node([device, node], cache=cache)
        self.assertEqual(found["fifine"], ())
        self.assertEqual(cache, {})

    def test_loopback_pins_both_streams_and_starts_microphone_muted(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(supervisor.subprocess, "Popen", return_value=process) as popen:
            loopback = supervisor.Loopback("bridge.mic", "capture", "playback", 1)
            loopback.defer_playback = True
            loopback.start()
        command = popen.call_args.args[0]
        capture_props = command[command.index("--capture-props") + 1]
        playback_props = command[command.index("--playback-props") + 1]
        for props in (capture_props, playback_props):
            self.assertIn("node.dont-reconnect = true", props)
            self.assertIn("node.passive = true", props)
        self.assertIn("node.autoconnect = false", playback_props)
        self.assertNotIn("--playback", command)

    def test_microphone_gain_and_mute_are_set_then_read_back(self) -> None:
        replies = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="Volume: 0.501 [MUTED]\n", stderr=""),
        ]
        with mock.patch.object(supervisor.subprocess, "run", side_effect=replies) as run:
            result = supervisor.set_and_verify_microphone_control(
                {"output.bridge.mic": {"object.id": 77}},
                "output.bridge.mic",
                -6.0,
                True,
            )
        self.assertTrue(result[0])
        self.assertAlmostEqual(result[1] or 0.0, -6.0, places=1)
        self.assertTrue(result[2])
        self.assertEqual(run.call_args_list[0].args[0], ["wpctl", "set-mute", "77", "1"])
        self.assertEqual(run.call_args_list[-1].args[0], ["wpctl", "get-volume", "77"])

    def test_control_failure_holds_graph_safe(self) -> None:
        settings = supervisor.Settings(
            aec=supervisor.AecSettings(),
            mic_gain_db=0.0,
            mic_muted=False,
        )
        graph = supervisor.CallGraph(settings)
        nodes = {
            "mic": {},
            settings.wired_output: {},
            settings.hfp_source: {},
            settings.hfp_sink: {},
            "output.bridge.mic": {"object.id": 88},
        }
        with mock.patch.object(supervisor, "Loopback", FakeLoopback):
            graph.tick(nodes, [], "mic")
            assert graph.microphone is not None and graph.callout is not None
            graph.routes_started -= supervisor.ATTACH_GRACE_SECONDS + 1
            with mock.patch.object(
                supervisor,
                "set_and_verify_microphone_control",
                return_value=(False, None, True, "readback failed"),
            ):
                graph.tick(nodes, [], "mic")
        self.assertEqual(graph.state, supervisor.State.SAFE)
        self.assertTrue(graph.mic_control_blocked)
        self.assertEqual(graph.last_failure, "readback failed")
        self.assertIsNone(graph.microphone)

    def test_microphone_is_muted_before_link_and_unmuted_only_after_validation(self) -> None:
        settings = supervisor.Settings(
            aec=supervisor.AecSettings(),
            mic_gain_db=0.0,
            mic_muted=False,
        )
        graph = supervisor.CallGraph(settings)
        nodes = {
            "mic": {},
            settings.wired_output: {},
            settings.hfp_source: {},
            settings.hfp_sink: {},
            "output.bridge.mic": {"object.id": 88},
        }
        events: list[str] = []

        def controls(_nodes, _node, _gain, muted):
            events.append(f"mute:{muted}")
            return True, 0.0, muted, None

        def create_link(source, target):
            events.append(f"link:{source}->{target}")
            return True

        with (
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(
                supervisor,
                "set_and_verify_microphone_control",
                side_effect=controls,
            ),
            mock.patch.object(supervisor, "link", side_effect=create_link),
        ):
            graph.tick(nodes, [], "mic")
            graph.tick(nodes, [], "mic")
            assert graph.microphone is not None and graph.callout is not None
            graph.routes_started -= supervisor.ATTACH_GRACE_SECONDS + 1
            links = [
                ("mic", graph.microphone.in_node),
                (graph.microphone.out_node, settings.hfp_sink),
                (settings.hfp_source, graph.callout.in_node),
                (graph.callout.out_node, settings.wired_output),
            ]
            graph.tick(nodes, links, "mic")
        self.assertEqual(events[0], "mute:True")
        self.assertTrue(events[1].startswith("link:output.bridge.mic->"))
        self.assertEqual(events[2], "mute:False")
        self.assertEqual(graph.state, supervisor.State.ACTIVE)


class MicrophonePriorityLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        self.hosts: list[FakeHost] = []
        test = self

        class LifecycleHost(FakeHost):
            def __init__(
                self,
                settings: object,
                microphone: str,
                output: str,
                *,
                latency_frames: int | None = None,
                play_delay_frames: int | None = None,
            ) -> None:
                super().__init__(
                    settings,
                    microphone,
                    output,
                    latency_frames=latency_frames,
                    play_delay_frames=play_delay_frames,
                )
                self.microphone = microphone
                self.output = output
                test.hosts.append(self)

            def start(self) -> None:
                test.events.append(f"start:aec:{self.microphone}")
                super().start()

            def stop(self, reason: str) -> None:
                test.events.append(f"stop:aec:{self.microphone}")
                super().stop(reason)

        class LifecycleLoopback(FakeLoopback):
            def start(self) -> None:
                test.events.append(f"start:{self.name}")
                super().start()

            def stop(self, reason: str) -> None:
                test.events.append(f"stop:{self.name}")
                super().stop(reason)

        self.host_type = LifecycleHost
        self.loopback_type = LifecycleLoopback

    def settings(self) -> supervisor.Settings:
        return supervisor.Settings(
            aec=supervisor.AecSettings(enabled=True),
            mic_gain_db=-3.0,
            mic_muted=False,
        )

    def nodes(
        self,
        graph: supervisor.CallGraph,
        microphones: tuple[str, ...] = ("lark", "fifine"),
        *,
        aec_ready: bool = False,
        control_ready: bool = False,
    ) -> dict[str, dict]:
        nodes = {
            graph.settings.wired_output: {},
            graph.settings.hfp_source: {},
            graph.settings.hfp_sink: {},
        }
        nodes.update({microphone: {} for microphone in microphones})
        if aec_ready:
            nodes[supervisor.AEC_SOURCE] = {}
            nodes[supervisor.AEC_SINK] = {}
        if control_ready:
            nodes["output.bridge.mic"] = {"object.id": 88}
        return nodes

    def record_aec_mute(self, muted: bool) -> bool:
        self.events.append(f"aec-mute:{muted}")
        return True

    def record_control(
        self,
        _nodes: supervisor.NodeMap,
        _node: str,
        gain_db: float,
        muted: bool,
    ) -> tuple[bool, float, bool, None]:
        self.events.append(f"control:{muted}")
        return True, gain_db, muted, None

    def record_link(self, source: str, target: str) -> bool:
        self.events.append(f"link:{source}->{target}")
        return True

    def record_unlink(self, source: str, target: str) -> bool:
        self.events.append(f"unlink:{source}->{target}")
        return True

    def active_links(
        self,
        graph: supervisor.CallGraph,
        selected: str,
    ) -> supervisor.LinkList:
        assert graph.microphone is not None and graph.callout is not None
        return [
            (selected, supervisor.AEC_CAPTURE),
            (supervisor.AEC_PLAYBACK, graph.settings.wired_output),
            (graph.microphone.capture, graph.microphone.in_node),
            (graph.microphone.out_node, graph.settings.hfp_sink),
            (graph.callout.capture, graph.callout.in_node),
            (graph.callout.out_node, graph.callout.playback),
        ]

    def finish_active_build(
        self,
        graph: supervisor.CallGraph,
        resolution: FakeMicrophoneResolution,
        present: tuple[str, ...],
    ) -> tuple[supervisor.NodeMap, supervisor.LinkList]:
        candidate_nodes = present
        ready_nodes = self.nodes(
            graph,
            present,
            aec_ready=True,
            control_ready=True,
        )
        graph.tick(
            ready_nodes,
            [],
            resolution,
            candidate_nodes=candidate_nodes,
        )
        assert graph.microphone is not None and graph.callout is not None
        self.assertTrue(graph.microphone.defer_playback)
        graph.tick(
            ready_nodes,
            [],
            resolution,
            candidate_nodes=candidate_nodes,
        )
        graph.routes_started -= supervisor.ATTACH_GRACE_SECONDS + 1
        links = self.active_links(graph, resolution.node or "")
        graph.tick(
            ready_nodes,
            links,
            resolution,
            candidate_nodes=candidate_nodes,
        )
        self.assertEqual(graph.state, supervisor.State.ACTIVE)
        self.assertTrue(graph.verified)
        self.assertTrue(graph.mic_control_verified)
        self.assertEqual(graph.mic_gain_observed_db, -3.0)
        self.assertIs(graph.mic_mute_observed, False)
        self.assertEqual(self.events[-1], "aec-mute:False")
        status = graph.status(
            ready_nodes,
            links,
            resolution,
        )
        self.assertEqual(
            [
                pair
                for pair in status["graph"]["expected_links"]
                if pair[1] == graph.settings.hfp_sink
            ],
            [(graph.microphone.out_node, graph.settings.hfp_sink)],
        )
        self.assertEqual(
            [
                pair
                for pair in status["graph"]["expected_links"]
                if pair[1] == supervisor.AEC_CAPTURE
            ],
            [(resolution.node, supervisor.AEC_CAPTURE)],
        )
        self.assertEqual(status["graph"]["missing_links"], [])
        self.assertEqual(status["graph"]["unexpected_links"], [])
        return ready_nodes, links

    def build_active(
        self,
        graph: supervisor.CallGraph,
        resolution: FakeMicrophoneResolution,
        present: tuple[str, ...],
    ) -> tuple[supervisor.NodeMap, supervisor.LinkList]:
        graph.tick(
            self.nodes(graph, present),
            [],
            resolution,
            candidate_nodes=present,
        )
        self.assertEqual(graph.state, supervisor.State.BUILDING)
        return self.finish_active_build(graph, resolution, present)

    def assert_active_switch(
        self,
        *,
        source: str,
        source_id: str,
        source_present: tuple[str, ...],
        target: str,
        target_id: str,
        target_present: tuple[str, ...],
    ) -> None:
        graph = supervisor.CallGraph(self.settings())
        before = FakeMicrophoneResolution(
            source,
            f"{source}-generation-1",
            selected_id=source_id,
        )
        after = FakeMicrophoneResolution(
            target,
            f"{target}-generation-2",
            selected_id=target_id,
        )
        with (
            mock.patch.object(supervisor, "NativeAecHost", self.host_type),
            mock.patch.object(supervisor, "Loopback", self.loopback_type),
            mock.patch.object(
                supervisor,
                "set_and_verify_microphone_control",
                side_effect=self.record_control,
            ),
            mock.patch.object(
                supervisor,
                "set_aec_mute",
                side_effect=self.record_aec_mute,
            ),
            mock.patch.object(supervisor, "link", side_effect=self.record_link),
            mock.patch.object(supervisor, "unlink", side_effect=self.record_unlink),
        ):
            _nodes, old_links = self.build_active(
                graph,
                before,
                source_present,
            )
            old_generation = graph.generation
            old_host = graph.aec_host
            old_microphone = graph.microphone
            old_callout = graph.callout
            old_token = graph.selected_microphone_token
            assert graph.microphone_report is not None
            old_report_token = graph.microphone_report["selected"]["instance_token"]
            self.events.clear()

            graph.tick(
                self.nodes(
                    graph,
                    target_present,
                    aec_ready=True,
                    control_ready=True,
                ),
                old_links,
                after,
                candidate_nodes=target_present,
            )

            self.assertEqual(graph.state, supervisor.State.DISCOVERING)
            self.assertEqual(graph.generation, old_generation + 1)
            self.assertIsNone(graph.aec_host)
            self.assertIsNone(graph.microphone)
            self.assertIsNone(graph.callout)
            self.assertEqual(
                self.events[:4],
                [
                    "stop:bridge.mic",
                    "aec-mute:True",
                    "stop:bridge.callout",
                    f"stop:aec:{source}",
                ],
            )
            self.assertIn(
                f"unlink:output.bridge.mic->{graph.settings.hfp_sink}",
                self.events,
            )

            self.events.clear()
            graph.tick(
                self.nodes(graph, target_present),
                [],
                after,
                candidate_nodes=target_present,
            )
            self.assertEqual(graph.state, supervisor.State.BUILDING)
            self.assertIsNot(graph.aec_host, old_host)
            assert graph.aec_host is not None
            self.assertEqual(graph.aec_host.microphone, target)
            self.finish_active_build(
                graph,
                after,
                target_present,
            )

        self.assertIsNot(graph.microphone, old_microphone)
        self.assertIsNot(graph.callout, old_callout)
        self.assertEqual(len(self.hosts), 2)
        self.assertNotEqual(graph.selected_microphone_token, old_token)
        self.assertEqual(graph.selected_microphone_token, after.instance_token)
        assert graph.microphone_report is not None
        self.assertNotEqual(
            graph.microphone_report["selected"]["instance_token"],
            old_report_token,
        )
        self.assertEqual(
            graph.microphone_report["selected"]["instance_token"],
            after.instance_token,
        )
        muted = self.events.index("control:True")
        linked = self.events.index(f"link:output.bridge.mic->{graph.settings.hfp_sink}")
        unmuted = self.events.index("control:False")
        self.assertLess(muted, linked)
        self.assertLess(linked, unmuted)
        self.assertEqual(
            [
                event
                for event in self.events
                if event == f"link:output.bridge.mic->{graph.settings.hfp_sink}"
            ],
            [f"link:output.bridge.mic->{graph.settings.hfp_sink}"],
        )

    def test_active_call_lark_to_fifine_fallback_rebuilds_once(self) -> None:
        self.assert_active_switch(
            source="lark",
            source_id="lark-a1",
            source_present=("lark", "fifine"),
            target="fifine",
            target_id="fifine-k054",
            target_present=("fifine",),
        )

    def test_active_call_fifine_to_lark_promotion_rebuilds_once(self) -> None:
        self.assert_active_switch(
            source="fifine",
            source_id="fifine-k054",
            source_present=("fifine",),
            target="lark",
            target_id="lark-a1",
            target_present=("lark", "fifine"),
        )

    def test_active_call_lark_to_k053_fallback_rebuilds_once(self) -> None:
        self.assert_active_switch(
            source="lark",
            source_id="lark-a1",
            source_present=("lark", "k053", "k054"),
            target="k053",
            target_id="fifine-k053",
            target_present=("k053", "k054"),
        )

    def test_active_call_k053_to_lark_promotion_rebuilds_once(self) -> None:
        self.assert_active_switch(
            source="k053",
            source_id="fifine-k053",
            source_present=("k053", "k054"),
            target="lark",
            target_id="lark-a1",
            target_present=("lark", "k053", "k054"),
        )

    def test_active_call_k053_to_k054_fallback_rebuilds_once(self) -> None:
        self.assert_active_switch(
            source="k053",
            source_id="fifine-k053",
            source_present=("k053", "k054"),
            target="k054",
            target_id="fifine-k054",
            target_present=("k054",),
        )

    def test_active_call_k054_to_k053_promotion_rebuilds_once(self) -> None:
        self.assert_active_switch(
            source="k054",
            source_id="fifine-k054",
            source_present=("k054",),
            target="k053",
            target_id="fifine-k053",
            target_present=("k053", "k054"),
        )

    def test_active_call_k053_replug_rebuilds_a_fresh_generation(self) -> None:
        self.assert_active_switch(
            source="k053",
            source_id="fifine-k053",
            source_present=("k053", "k054"),
            target="k053",
            target_id="fifine-k053",
            target_present=("k053", "k054"),
        )

    def assert_inactive_hotplug_does_not_churn(
        self,
        *,
        selected: str,
        selected_id: str,
        before: tuple[str, ...],
        absent: tuple[str, ...],
        restored: tuple[str, ...],
    ) -> None:
        graph = supervisor.CallGraph(self.settings())
        resolution = FakeMicrophoneResolution(
            selected,
            f"{selected}-generation",
            selected_id=selected_id,
        )
        with (
            mock.patch.object(supervisor, "NativeAecHost", self.host_type),
            mock.patch.object(supervisor, "Loopback", self.loopback_type),
            mock.patch.object(
                supervisor,
                "set_and_verify_microphone_control",
                side_effect=self.record_control,
            ),
            mock.patch.object(
                supervisor,
                "set_aec_mute",
                side_effect=self.record_aec_mute,
            ),
            mock.patch.object(supervisor, "link", side_effect=self.record_link),
            mock.patch.object(supervisor, "unlink", side_effect=self.record_unlink),
        ):
            _nodes, links = self.build_active(
                graph,
                resolution,
                before,
            )
            generation = graph.generation
            owners = (graph.aec_host, graph.microphone, graph.callout)
            token = graph.selected_microphone_token
            assert graph.microphone_report is not None
            report_token = graph.microphone_report["selected"]["instance_token"]
            self.events.clear()

            graph.tick(
                self.nodes(
                    graph,
                    absent,
                    aec_ready=True,
                    control_ready=True,
                ),
                links,
                resolution,
                candidate_nodes=absent,
            )
            graph.tick(
                self.nodes(
                    graph,
                    restored,
                    aec_ready=True,
                    control_ready=True,
                ),
                links,
                resolution,
                candidate_nodes=restored,
            )

        self.assertEqual(graph.state, supervisor.State.ACTIVE)
        self.assertEqual(graph.generation, generation)
        self.assertEqual((graph.aec_host, graph.microphone, graph.callout), owners)
        self.assertEqual(len(self.hosts), 1)
        self.assertEqual(graph.selected_microphone_token, token)
        assert graph.microphone_report is not None
        self.assertEqual(
            graph.microphone_report["selected"]["instance_token"],
            report_token,
        )
        self.assertEqual(self.events, [])

    def test_inactive_fifine_hotplug_does_not_churn_active_lark(self) -> None:
        self.assert_inactive_hotplug_does_not_churn(
            selected="lark",
            selected_id="lark-a1",
            before=("lark", "fifine"),
            absent=("lark",),
            restored=("lark", "fifine"),
        )

    def test_inactive_k054_hotplug_does_not_churn_active_k053(self) -> None:
        self.assert_inactive_hotplug_does_not_churn(
            selected="k053",
            selected_id="fifine-k053",
            before=("k053", "k054"),
            absent=("k053",),
            restored=("k053", "k054"),
        )

    def test_active_call_with_neither_microphone_waits_after_teardown(self) -> None:
        graph = supervisor.CallGraph(self.settings())
        selected = FakeMicrophoneResolution("lark", "lark-generation")
        missing = FakeMicrophoneResolution(None, None)
        with (
            mock.patch.object(supervisor, "NativeAecHost", self.host_type),
            mock.patch.object(supervisor, "Loopback", self.loopback_type),
            mock.patch.object(
                supervisor,
                "set_and_verify_microphone_control",
                side_effect=self.record_control,
            ),
            mock.patch.object(
                supervisor,
                "set_aec_mute",
                side_effect=self.record_aec_mute,
            ),
            mock.patch.object(supervisor, "link", side_effect=self.record_link),
            mock.patch.object(supervisor, "unlink", side_effect=self.record_unlink),
        ):
            _nodes, links = self.build_active(graph, selected, ("lark",))
            generation = graph.generation
            self.events.clear()
            graph.tick(
                self.nodes(
                    graph,
                    (),
                    aec_ready=True,
                    control_ready=True,
                ),
                links,
                missing,
                candidate_nodes=(),
            )

        self.assertEqual(graph.state, supervisor.State.WAITING_MIC)
        self.assertEqual(graph.generation, generation + 1)
        self.assertIsNone(graph.aec_host)
        self.assertIsNone(graph.microphone)
        self.assertIsNone(graph.callout)
        self.assertEqual(self.events[0], "stop:bridge.mic")
        self.assertIn(
            f"unlink:output.bridge.mic->{graph.settings.hfp_sink}",
            self.events,
        )

    def test_active_fifine_absence_then_same_name_recovery_rebuilds(self) -> None:
        graph = supervisor.CallGraph(self.settings())
        before = FakeMicrophoneResolution(
            "fifine",
            "fifine-generation-1",
            selected_id="fifine-k054",
        )
        missing = FakeMicrophoneResolution(None, None)
        recovered = FakeMicrophoneResolution(
            "fifine",
            "fifine-generation-2",
            selected_id="fifine-k054",
        )
        with (
            mock.patch.object(supervisor, "NativeAecHost", self.host_type),
            mock.patch.object(supervisor, "Loopback", self.loopback_type),
            mock.patch.object(
                supervisor,
                "set_and_verify_microphone_control",
                side_effect=self.record_control,
            ),
            mock.patch.object(
                supervisor,
                "set_aec_mute",
                side_effect=self.record_aec_mute,
            ),
            mock.patch.object(supervisor, "link", side_effect=self.record_link),
            mock.patch.object(supervisor, "unlink", side_effect=self.record_unlink),
        ):
            _nodes, old_links = self.build_active(graph, before, ("fifine",))
            active_generation = graph.generation
            old_host = graph.aec_host
            old_microphone = graph.microphone
            old_callout = graph.callout
            old_token = graph.selected_microphone_token
            assert graph.microphone_report is not None
            old_report_token = graph.microphone_report["selected"]["instance_token"]
            self.events.clear()

            graph.tick(
                self.nodes(
                    graph,
                    (),
                    aec_ready=True,
                    control_ready=True,
                ),
                old_links,
                missing,
                candidate_nodes=(),
            )

            self.assertEqual(graph.state, supervisor.State.WAITING_MIC)
            self.assertEqual(graph.generation, active_generation + 1)
            self.assertTrue(graph.break_before_make)
            self.assertIsNone(graph.aec_host)
            self.assertIsNone(graph.microphone)
            self.assertIsNone(graph.callout)
            self.assertEqual(
                self.events[:4],
                [
                    "stop:bridge.mic",
                    "aec-mute:True",
                    "stop:bridge.callout",
                    "stop:aec:fifine",
                ],
            )
            self.assertIn(
                f"unlink:output.bridge.mic->{graph.settings.hfp_sink}",
                self.events,
            )
            waiting_status = graph.status(
                self.nodes(graph, ()),
                [],
                missing,
            )
            self.assertEqual(waiting_status["graph"]["expected_links"], [])
            self.assertEqual(
                [
                    pair
                    for pair in waiting_status["graph"]["expected_links"]
                    if pair[1] == graph.settings.hfp_sink
                ],
                [],
            )

            self.events.clear()
            graph.tick(
                self.nodes(graph, ("fifine",)),
                [],
                recovered,
                candidate_nodes=("fifine",),
            )
            self.assertEqual(graph.state, supervisor.State.BUILDING)
            self.assertEqual(graph.generation, active_generation + 2)
            self.assertEqual(
                graph.selected_microphone_token,
                "fifine-generation-2",
            )
            self.assertIsNot(graph.aec_host, old_host)
            assert graph.aec_host is not None
            self.assertEqual(graph.aec_host.microphone, "fifine")
            self.finish_active_build(
                graph,
                recovered,
                ("fifine",),
            )

        self.assertIsNot(graph.microphone, old_microphone)
        self.assertIsNot(graph.callout, old_callout)
        self.assertEqual(len(self.hosts), 2)
        self.assertNotEqual(graph.selected_microphone_token, old_token)
        self.assertEqual(graph.selected_microphone_token, recovered.instance_token)
        assert graph.microphone_report is not None
        self.assertNotEqual(
            graph.microphone_report["selected"]["instance_token"],
            old_report_token,
        )
        self.assertEqual(
            graph.microphone_report["selected"]["instance_token"],
            recovered.instance_token,
        )
        muted = self.events.index("control:True")
        linked = self.events.index(f"link:output.bridge.mic->{graph.settings.hfp_sink}")
        unmuted = self.events.index("control:False")
        self.assertLess(muted, linked)
        self.assertLess(linked, unmuted)
        self.assertEqual(
            [
                event
                for event in self.events
                if event == f"link:output.bridge.mic->{graph.settings.hfp_sink}"
            ],
            [f"link:output.bridge.mic->{graph.settings.hfp_sink}"],
        )

    def test_active_call_higher_priority_ambiguity_holds_safe(self) -> None:
        graph = supervisor.CallGraph(self.settings())
        selected = FakeMicrophoneResolution(
            "fifine",
            "fifine-generation",
            selected_id="fifine-k054",
        )
        ambiguity_reason = (
            "lark-a1 ambiguous: 2 physical devices match; " "configure usb_serial or usb_port_path"
        )
        ambiguous = FakeMicrophoneResolution(
            None,
            None,
            blocked=True,
            candidates=[
                {
                    "id": "lark-a1",
                    "state": "ambiguous",
                    "matched_nodes": ["lark-a", "lark-b"],
                    "reason": ambiguity_reason,
                }
            ],
            reason=ambiguity_reason,
        )
        with (
            mock.patch.object(supervisor, "NativeAecHost", self.host_type),
            mock.patch.object(supervisor, "Loopback", self.loopback_type),
            mock.patch.object(
                supervisor,
                "set_and_verify_microphone_control",
                side_effect=self.record_control,
            ),
            mock.patch.object(
                supervisor,
                "set_aec_mute",
                side_effect=self.record_aec_mute,
            ),
            mock.patch.object(supervisor, "link", side_effect=self.record_link),
            mock.patch.object(supervisor, "unlink", side_effect=self.record_unlink),
        ):
            _nodes, links = self.build_active(graph, selected, ("fifine",))
            generation = graph.generation
            self.events.clear()
            graph.tick(
                self.nodes(
                    graph,
                    ("lark-a", "lark-b", "fifine"),
                    aec_ready=True,
                    control_ready=True,
                ),
                links,
                ambiguous,
                candidate_nodes=("lark-a", "lark-b", "fifine"),
            )

        self.assertEqual(graph.state, supervisor.State.SAFE)
        self.assertEqual(graph.generation, generation + 1)
        self.assertIsNone(graph.aec_host)
        self.assertIsNone(graph.microphone)
        self.assertIsNone(graph.callout)
        self.assertEqual(self.events[0], "stop:bridge.mic")
        self.assertEqual(graph.last_failure, ambiguity_reason)
        assert graph.microphone_report is not None
        self.assertEqual(
            graph.microphone_report["selection_reason"],
            ambiguity_reason,
        )
        status = graph.status(
            self.nodes(graph, ("lark-a", "lark-b", "fifine")),
            [],
            ambiguous,
        )
        self.assertEqual(status["microphone"]["selection_reason"], ambiguity_reason)

    def test_live_call_without_a_candidate_waits_without_uplink(self) -> None:
        graph = supervisor.CallGraph(supervisor.Settings(aec=supervisor.AecSettings()))
        resolution = FakeMicrophoneResolution(None, None)
        graph.tick(self.nodes(graph), [], resolution)
        self.assertEqual(graph.state, supervisor.State.WAITING_MIC)
        self.assertIsNone(graph.microphone)

    def test_ambiguous_candidate_is_safe_even_without_a_call(self) -> None:
        graph = supervisor.CallGraph(supervisor.Settings(aec=supervisor.AecSettings()))
        resolution = FakeMicrophoneResolution(None, None, blocked=True)
        graph.tick({graph.settings.wired_output: {}}, [], resolution)
        self.assertEqual(graph.state, supervisor.State.SAFE)
        self.assertIsNone(graph.microphone)

    def test_all_candidate_raw_uplinks_are_cut_before_build(self) -> None:
        graph = supervisor.CallGraph(supervisor.Settings(aec=supervisor.AecSettings(enabled=True)))
        removed: list[tuple[str, str]] = []
        resolution = FakeMicrophoneResolution("lark", "lark-generation")
        links = [
            ("lark", graph.settings.hfp_sink),
            ("fifine", graph.settings.hfp_sink),
        ]
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(
                supervisor, "unlink", lambda source, target: removed.append((source, target))
            ),
        ):
            graph.tick(
                self.nodes(graph),
                links,
                resolution,
                candidate_nodes=("lark", "fifine"),
            )
        self.assertEqual(set(removed), set(links))
        self.assertIsNone(graph.aec_host)
        with mock.patch.object(supervisor, "NativeAecHost", FakeHost):
            graph.tick(
                self.nodes(graph),
                [],
                resolution,
                candidate_nodes=("lark", "fifine"),
            )
        self.assertIsNotNone(graph.aec_host)

    def test_same_node_replug_rebuilds_a_fresh_active_generation(self) -> None:
        self.assert_active_switch(
            source="fifine",
            source_id="fifine-k054",
            source_present=("fifine",),
            target="fifine",
            target_id="fifine-k054",
            target_present=("fifine",),
        )

    def test_status_keeps_actual_lark_separate_from_selected_fifine(self) -> None:
        candidates = [
            {
                "id": "lark-a1",
                "label": "Lark",
                "priority": 0,
                "state": "usable",
                "matched_nodes": ["lark"],
                "reason": "usable",
            },
            {
                "id": "fifine-k054",
                "label": "FIFINE",
                "priority": 1,
                "state": "selected",
                "matched_nodes": ["fifine"],
                "reason": "selected",
            },
        ]
        resolution = FakeMicrophoneResolution(
            "fifine",
            "generation",
            selected_id="fifine-k054",
            candidates=candidates,
        )
        graph = supervisor.CallGraph(supervisor.Settings(aec=supervisor.AecSettings()))
        graph.tick({"lark": {}, "fifine": {}, graph.settings.wired_output: {}}, [], resolution)
        report = graph.status(
            {"lark": {}, "fifine": {}, graph.settings.wired_output: {}},
            [],
            resolution,
        )
        self.assertEqual(report["endpoints"]["microphone"], "fifine")
        self.assertEqual(report["endpoints"]["lark"], "lark")
        self.assertEqual(report["microphone"]["selected"]["id"], "fifine-k054")


if __name__ == "__main__":
    unittest.main()
