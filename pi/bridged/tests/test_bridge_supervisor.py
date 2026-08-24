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
    """Switching the output must actually rebuild the graph.

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

    def test_changing_the_output_changes_the_signature(self) -> None:
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

    # (call_up, lark, output_node) -- the tuple tick() computes. Pre-set it so the first
    # tick does not look like a fresh generation and reset attempts, which is what a real
    # supervisor mid-call would already have done.
    #
    # The third element was `True` until output selection landed: a bool saying "an output
    # is present". It is now the output NODE NAME, because presence cannot distinguish the
    # wired jack from a speaker and so could not trigger a rebuild when the user switched
    # between them. A stale bool here silently defeats this test's own premise -- it makes
    # the first tick look like a generation change, which resets attempts and escapes
    # FAILED for the wrong reason.
    SIGNATURE = (True, "lark", supervisor.DEFAULT_WIRED_OUT)

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
        nodes = {"lark": {}, graph.settings.hfp_sink: {}, graph.settings.hfp_source: {},
                 graph.settings.wired_output: {}}
        with mock.patch.object(supervisor, "NativeAecHost", FakeHost):
            graph.tick(nodes, [], "lark")
        self.assertEqual(graph.state, supervisor.State.FAILED)
        self.assertEqual(graph.attempts, supervisor.MAX_BUILD_ATTEMPTS)

    def test_failed_retries_once_the_interval_elapses(self) -> None:
        graph = self._failed_graph()
        graph.next_attempt = 0.0  # pretend FAILED_RETRY_SECONDS has passed
        nodes = {"lark": {}, graph.settings.hfp_sink: {}, graph.settings.hfp_source: {},
                 graph.settings.wired_output: {}}
        with mock.patch.object(supervisor, "NativeAecHost", FakeHost):
            graph.tick(nodes, [], "lark")
        self.assertNotEqual(
            graph.state, supervisor.State.FAILED, "FAILED must be escapable without a signature change"
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

    def test_sweep_does_not_stall_the_build(self) -> None:
        """Cutting the link must not abort the tick; that would extend the exposure."""
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
        self.assertIsNotNone(graph.aec_host, "the build must still start on this tick")


if __name__ == "__main__":
    unittest.main()
