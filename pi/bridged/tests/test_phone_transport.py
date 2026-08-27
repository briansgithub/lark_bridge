"""The ADR-0009 phone transport contract, encoded before it is implemented.

These tests define what "transparent phone audio" means for this appliance, and they are
deliberately written to fail until Steps 6-8 of E19 implement it. They are not a description
of current behaviour.

The contract in one line: the phone's A2DP media goes to the selected output under supervisor
ownership, the microphone is present exactly when Android opens a communication transport, and
the status surface never claims a microphone is live when Android is not consuming it.

WHY THE PROFILE STRING IS THE DISCRIMINATOR, NOT THE NODE INDEX
---------------------------------------------------------------
Both the phone's HFP uplink and the phone's A2DP media stream arrive as `bluez_input.<MAC>.<N>`.
The trailing number is a profile index and is not a constant -- `outputs.find_a2dp_node` already
carries that warning for speakers, and it applies here for the same reason. The two are told
apart by `api.bluez5.profile`, never by the suffix.

`a2dp-source` is the profile a REMOTE device presents when it streams media to us. It is the
mirror of `a2dp-sink`, which is what a speaker we play to presents. Enabling our own `a2dp_sink`
role is what makes the former appear at all; it does not change what the Pixel advertises, so
`outputs.py` keeps working unchanged.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from test_bridge_supervisor import FakeHost, FakeLoopback, FakeMicrophoneResolution

import bridge_supervisor as supervisor

ROOT = Path(__file__).resolve().parents[3]

PHONE_MAC = supervisor.DEFAULT_PHONE_MAC
PHONE_UNDERSCORED = PHONE_MAC.replace(":", "_")

# The phone's media stream. The .1 here is arbitrary on purpose: any suffix must work, and one
# of the tests below pins that.
PHONE_MEDIA = f"bluez_input.{PHONE_UNDERSCORED}.1"
SPEAKER_MAC = "C9:5C:FD:6E:28:46"
SPEAKER_NODE = f"bluez_output.{SPEAKER_MAC.replace(':', '_')}.1"

FIFINE_NODE = "alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback"


def media_props(state: str = "running") -> dict:
    return {"api.bluez5.profile": "a2dp-source", "node.state": state}


def call_settings() -> supervisor.Settings:
    return supervisor.Settings(aec=supervisor.AecSettings(enabled=True))


def idle_nodes(settings: supervisor.Settings, *, media_state: str = "running") -> dict:
    """Phone connected and presenting media; no call."""
    return {
        settings.lark_node: {},
        settings.wired_output: {},
        PHONE_MEDIA: media_props(media_state),
    }


def call_nodes(settings: supervisor.Settings) -> dict:
    """Android has opened a communication transport."""
    return {
        settings.lark_node: {},
        settings.wired_output: {},
        settings.hfp_source: {},
        settings.hfp_sink: {},
    }


def build_call(graph: supervisor.CallGraph, settings: supervisor.Settings) -> list:
    """Drive the existing call path to ACTIVE and return its verified link list."""
    nodes = call_nodes(settings)
    graph.tick(nodes, [], settings.lark_node)
    aec_nodes = dict(nodes)
    aec_nodes[supervisor.AEC_SOURCE] = {}
    aec_nodes[supervisor.AEC_SINK] = {}
    graph.tick(aec_nodes, [], settings.lark_node)
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
    return links


class PhoneMediaNodeDiscoveryTests(unittest.TestCase):
    """Finding the phone's media stream, and refusing to confuse it with anything else."""

    def test_media_node_found_by_mac_and_profile(self) -> None:
        nodes = {PHONE_MEDIA: media_props()}
        self.assertEqual(
            supervisor.find_phone_media_node(nodes, PHONE_MAC),
            PHONE_MEDIA,
        )

    def test_profile_index_suffix_is_not_hardcoded(self) -> None:
        """Any suffix must resolve. Hardcoding .1 breaks on the first device that differs."""
        odd = f"bluez_input.{PHONE_UNDERSCORED}.7"
        self.assertEqual(supervisor.find_phone_media_node({odd: media_props()}, PHONE_MAC), odd)

    def test_hfp_uplink_is_not_mistaken_for_media(self) -> None:
        """The HFP source shares the bluez_input.<MAC>.<N> shape. Only the profile separates them."""
        settings = call_settings()
        nodes = {settings.hfp_source: {"api.bluez5.profile": "headset-audio-gateway"}}
        self.assertIsNone(supervisor.find_phone_media_node(nodes, PHONE_MAC))

    def test_speaker_is_not_mistaken_for_phone_media(self) -> None:
        """A speaker we play to presents a2dp-sink and belongs to outputs.py, not here."""
        nodes = {SPEAKER_NODE: {"api.bluez5.profile": "a2dp-sink"}}
        self.assertIsNone(supervisor.find_phone_media_node(nodes, PHONE_MAC))

    def test_other_phone_mac_is_ignored(self) -> None:
        stranger = "bluez_input.11_22_33_44_55_66.1"
        self.assertIsNone(supervisor.find_phone_media_node({stranger: media_props()}, PHONE_MAC))


class PhoneTransportStateTests(unittest.TestCase):
    """The state the operator is told the phone link is in."""

    def test_phone_absent_when_no_phone_nodes(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        graph.tick({settings.lark_node: {}, settings.wired_output: {}}, [], settings.lark_node)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.ABSENT)
        self.assertIsNone(graph.media_node)

    def test_media_is_routed_to_the_selected_output(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(idle_nodes(settings), [], settings.lark_node)
        linker.assert_called_once_with(PHONE_MEDIA, settings.wired_output)
        self.assertEqual(graph.media_node, PHONE_MEDIA)

    def test_media_never_reaches_an_output_we_did_not_select(self) -> None:
        """Determinism, not default-device luck. A second sink must not attract the stream."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = idle_nodes(settings)
        nodes["alsa_output.usb-some-other-dac.analog-stereo"] = {}
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(nodes, [], settings.lark_node)
        targets = {call.args[1] for call in linker.call_args_list}
        self.assertEqual(targets, {settings.wired_output})

    def test_media_active_only_while_the_stream_runs(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        graph.tick(idle_nodes(settings, media_state="running"), links, settings.lark_node)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

    def test_connected_but_silent_is_media_ready_not_active(self) -> None:
        """Nothing is playing. Saying ACTIVE here would be the first false claim."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        graph.tick(idle_nodes(settings, media_state="suspended"), links, settings.lark_node)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_READY)

    def test_existing_link_is_not_duplicated(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(idle_nodes(settings), links, settings.lark_node)
        linker.assert_not_called()

    def test_missing_output_is_actionable_not_silent(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = {settings.lark_node: {}, PHONE_MEDIA: media_props()}
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(nodes, [], settings.lark_node, output_node=None)
        linker.assert_not_called()
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_READY)
        self.assertIsNotNone(graph.last_failure)


class TransportTransitionTests(unittest.TestCase):
    """Break-before-make applies to the phone transport, not only to microphones."""

    def test_media_link_is_dropped_before_the_call_graph_is_built(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        graph.tick(idle_nodes(settings), [(PHONE_MEDIA, settings.wired_output)], settings.lark_node)

        nodes = dict(call_nodes(settings))
        nodes[PHONE_MEDIA] = media_props()
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
        ):
            graph.tick(nodes, [(PHONE_MEDIA, settings.wired_output)], settings.lark_node)
        self.assertIn(
            (PHONE_MEDIA, settings.wired_output),
            [call.args for call in unlinker.call_args_list],
        )

    def test_media_and_call_never_feed_the_output_together(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            build_call(graph, settings)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.CALL)
        self.assertIsNone(graph.media_node)

    def test_call_teardown_restores_the_media_route(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            build_call(graph, settings)
            with mock.patch.object(supervisor, "link", return_value=True) as linker:
                graph.tick(idle_nodes(settings, media_state="suspended"), [], settings.lark_node)
        linker.assert_any_call(PHONE_MEDIA, settings.wired_output)

    def test_restored_route_does_not_claim_playback_resumed(self) -> None:
        """Android decides whether the app resumes. Restoring a route is not resuming audio."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            build_call(graph, settings)
            graph.tick(
                idle_nodes(settings, media_state="suspended"),
                [(PHONE_MEDIA, settings.wired_output)],
                settings.lark_node,
            )
        self.assertEqual(
            graph.phone_transport,
            supervisor.PhoneTransport.MEDIA_RESTORED_APP_PAUSED,
        )

    def test_resumed_playback_leaves_the_paused_state(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            build_call(graph, settings)
            graph.tick(idle_nodes(settings, media_state="suspended"), links, settings.lark_node)
            graph.tick(idle_nodes(settings, media_state="running"), links, settings.lark_node)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)


class ReconnectTests(unittest.TestCase):
    def test_disconnect_clears_the_media_route(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        graph.tick(idle_nodes(settings), [(PHONE_MEDIA, settings.wired_output)], settings.lark_node)
        graph.tick({settings.lark_node: {}, settings.wired_output: {}}, [], settings.lark_node)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.ABSENT)
        self.assertIsNone(graph.media_node)

    def test_reconnect_does_not_leave_a_duplicate_link(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        graph.tick(idle_nodes(settings), [(PHONE_MEDIA, settings.wired_output)], settings.lark_node)
        graph.tick({settings.lark_node: {}, settings.wired_output: {}}, [], settings.lark_node)
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(idle_nodes(settings), [], settings.lark_node)
        self.assertEqual(linker.call_count, 1)


class MicrophoneInteractionTests(unittest.TestCase):
    """The microphone guarantees are untouched, and they do not disturb media."""

    def test_microphone_change_does_not_churn_the_media_graph(self) -> None:
        """Promoting the Lark or falling back to the FIFINE must not restart the music."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = idle_nodes(settings)
        nodes[FIFINE_NODE] = {}
        links = [(PHONE_MEDIA, settings.wired_output)]
        graph.tick(nodes, links, settings.lark_node)
        with (
            mock.patch.object(supervisor, "link", return_value=True) as linker,
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
        ):
            graph.tick(nodes, links, FIFINE_NODE)
        self.assertNotIn(
            (PHONE_MEDIA, settings.wired_output),
            [call.args for call in unlinker.call_args_list],
        )
        linker.assert_not_called()

    def test_lark_present_without_a_live_transmitter_does_not_disturb_media(self) -> None:
        """Liveness gating is a microphone concern. Music keeps playing."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        resolution = FakeMicrophoneResolution(None, None, reason="no live Lark transmitter")
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(idle_nodes(settings), links, resolution)
        self.assertNotIn(
            (PHONE_MEDIA, settings.wired_output),
            [call.args for call in unlinker.call_args_list],
        )
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

    def test_ambiguous_microphone_does_not_silence_media(self) -> None:
        """Fail closed on the uplink. There is no microphone in the media path to fail closed on."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        blocked = FakeMicrophoneResolution(
            None, None, blocked=True, reason="two devices match lark-a1"
        )
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(idle_nodes(settings), links, blocked)
        self.assertNotIn(
            (PHONE_MEDIA, settings.wired_output),
            [call.args for call in unlinker.call_args_list],
        )
        self.assertEqual(graph.state, supervisor.State.SAFE)

    def test_no_physical_microphone_ever_reaches_the_phone(self) -> None:
        """The bypass this whole design exists to prevent, checked in every transport state."""
        settings = call_settings()
        forbidden = {
            (settings.lark_node, settings.hfp_sink),
            (FIFINE_NODE, settings.hfp_sink),
        }
        for media_state in ("running", "suspended"):
            with self.subTest(media_state=media_state):
                graph = supervisor.CallGraph(settings)
                with mock.patch.object(supervisor, "link", return_value=True) as linker:
                    graph.tick(
                        idle_nodes(settings, media_state=media_state), [], settings.lark_node
                    )
                self.assertFalse(forbidden & {call.args for call in linker.call_args_list})


class StatusHonestyTests(unittest.TestCase):
    """Step 8's hardest requirement: never imply Android is using a microphone it is not."""

    def test_idle_status_says_android_has_no_microphone_transport(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = idle_nodes(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        graph.tick(nodes, links, settings.lark_node)
        status = graph.status(nodes, links, settings.lark_node)
        self.assertFalse(status["phone"]["android_microphone_transport"])
        self.assertTrue(status["phone"]["connected"])

    def test_idle_status_still_reports_the_microphone_as_selected_and_ready(self) -> None:
        """Ready is true and useful. Live would be a lie."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = idle_nodes(settings)
        graph.tick(nodes, [], settings.lark_node)
        status = graph.status(nodes, [], settings.lark_node)
        self.assertEqual(status["endpoints"]["microphone"], settings.lark_node)
        self.assertFalse(status["phone"]["android_microphone_transport"])
        self.assertIsNotNone(status["phone"]["microphone_transport_reason"])

    def test_call_status_reports_a_live_microphone_transport(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            links = build_call(graph, settings)
            nodes = dict(call_nodes(settings))
            nodes[supervisor.AEC_SOURCE] = {}
            nodes[supervisor.AEC_SINK] = {}
            status = graph.status(nodes, links, settings.lark_node)
        self.assertTrue(status["phone"]["android_microphone_transport"])

    def test_status_reports_the_transport_state(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = idle_nodes(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        graph.tick(nodes, links, settings.lark_node)
        status = graph.status(nodes, links, settings.lark_node)
        self.assertEqual(
            status["phone"]["transport"],
            supervisor.PhoneTransport.MEDIA_ACTIVE.value,
        )
        self.assertEqual(status["phone"]["media_node"], PHONE_MEDIA)
        self.assertTrue(status["phone"]["media_routed"])

    def test_disconnected_status_is_not_confused_with_idle(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = {settings.lark_node: {}, settings.wired_output: {}}
        graph.tick(nodes, [], settings.lark_node)
        status = graph.status(nodes, [], settings.lark_node)
        self.assertFalse(status["phone"]["connected"])
        self.assertEqual(
            status["phone"]["transport"],
            supervisor.PhoneTransport.ABSENT.value,
        )


def test_phone_media_nodes_are_not_autoconnected() -> None:
    """The guard ADR-0009 makes part of the decision rather than implementation detail.

    65-bridge-hfp-no-autolink.conf covers `headset-audio-gateway` only, and says in as many
    words that A2DP nodes keep WirePlumber's default policy. Restoring the a2dp_sink role
    without this file hands the phone's media node to whatever the default sink happens to be.
    """
    policy = (
        ROOT
        / "pi"
        / "wireplumber"
        / "wireplumber.conf.d"
        / "66-bridge-a2dp-source-no-autolink.conf"
    ).read_text(encoding="utf-8")

    assert 'api.bluez5.profile = "a2dp-source"' in policy
    assert "node.autoconnect = false" in policy
    # The speaker path must keep its normal policy; this rule is about the phone only.
    assert '"a2dp-sink"' not in policy


def test_a2dp_sink_role_is_advertised() -> None:
    """Without this the Pixel has no Media audio target and the contract is unreachable."""
    policy = (
        ROOT / "pi" / "wireplumber" / "wireplumber.conf.d" / "50-bridge-bluez.conf"
    ).read_text(encoding="utf-8")

    roles_line = next(
        line for line in policy.splitlines() if line.strip().startswith("bluez5.roles")
    )
    assert "a2dp_sink" in roles_line
    # The roles we already depend on must survive the edit.
    for role in ("a2dp_source", "hfp_hf", "hsp_hs"):
        assert role in roles_line
