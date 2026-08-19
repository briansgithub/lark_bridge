from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge_supervisor as supervisor


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


class FakeHost:
    def __init__(self, _settings: object, _microphone: str, _output: str):
        self.running = False
        self.pid = None

    def start(self) -> None:
        self.running = True
        self.pid = 123

    def stop(self, _reason: str) -> None:
        self.running = False
        self.pid = None


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


if __name__ == "__main__":
    unittest.main()
