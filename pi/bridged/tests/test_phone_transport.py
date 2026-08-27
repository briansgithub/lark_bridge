"""The ADR-0009 phone transport contract and its measured E19 corrections.

These tests define what "transparent phone audio" means for this appliance. They intentionally
distinguish a requested route from one verified in a fresh PipeWire snapshot, and an ended call
from media that Android has actually resumed.

The contract in one line: the phone's A2DP media goes to the configured AUX output under supervisor
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

All of the above was confirmed live on 2026-08-27 except where a test says otherwise; the
media node is `bluez_input.<MAC>.2`, `media.class = Stream/Output/Audio`, codec `sbc`, and
it exists only while audio is actually flowing.
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
FOREIGN_PHONE_MAC = "11:22:33:44:55:66"
FOREIGN_MEDIA = f"bluez_input.{FOREIGN_PHONE_MAC.replace(':', '_')}.4"

FIFINE_NODE = "alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback"


def media_props(*, object_id: int | None = None) -> dict:
    """The phone's media stream, exactly as measured on 2026-08-27.

    It is a client STREAM, not a source node, and it exists ONLY while audio is flowing --
    pausing destroys it and resuming recreates it. So there is no "paused node" to inspect and
    no node.state to branch on: presence is the signal.
    """
    props = {
        "api.bluez5.profile": "a2dp-source",
        "media.class": "Stream/Output/Audio",
    }
    if object_id is not None:
        props["object.id"] = object_id
    return props


def call_settings() -> supervisor.Settings:
    return supervisor.Settings(aec=supervisor.AecSettings(enabled=True))


def idle_nodes(
    settings: supervisor.Settings,
    *,
    media: bool = True,
    media_object_id: int | None = None,
) -> dict:
    """Phone connected, no call. `media` controls whether audio is currently flowing."""
    nodes = {
        settings.lark_node: {},
        settings.wired_output: {},
    }
    if media:
        nodes[PHONE_MEDIA] = media_props(object_id=media_object_id)
    return nodes


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

    def test_media_class_is_required(self) -> None:
        props = media_props()
        props["media.class"] = "Audio/Source"
        self.assertIsNone(supervisor.find_phone_media_node({PHONE_MEDIA: props}, PHONE_MAC))

    def test_ambiguous_phone_streams_fail_closed(self) -> None:
        second = f"bluez_input.{PHONE_UNDERSCORED}.9"
        self.assertIsNone(
            supervisor.find_phone_media_node(
                {PHONE_MEDIA: media_props(), second: media_props()}, PHONE_MAC
            )
        )


class PhoneTransportStateTests(unittest.TestCase):
    """The state the operator is told the phone link is in."""

    def test_phone_absent_when_no_phone_nodes(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        graph.tick(
            {settings.lark_node: {}, settings.wired_output: {}},
            [],
            settings.lark_node,
            phone_connected=False,
        )
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.ABSENT)
        self.assertIsNone(graph.media_node)

    def test_media_is_routed_to_the_configured_aux_output(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(idle_nodes(settings), [], settings.lark_node)
        linker.assert_called_once_with(PHONE_MEDIA, settings.wired_output)
        self.assertEqual(graph.media_node, PHONE_MEDIA)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertFalse(graph.media_route_verified)

    def test_media_never_reaches_a_decoy_or_dynamic_call_output(self) -> None:
        """This release pins phone media to AUX, independent of default/call output selection."""
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = idle_nodes(settings)
        decoy = "alsa_output.usb-some-other-dac.analog-stereo"
        nodes[decoy] = {}
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(nodes, [], settings.lark_node, output_node=decoy)
        targets = {call.args[1] for call in linker.call_args_list}
        self.assertEqual(targets, {settings.wired_output})

    def test_media_active_while_the_stream_node_exists(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        graph.tick(idle_nodes(settings), links, settings.lark_node, phone_connected=True)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

    def test_connected_but_silent_is_media_ready_not_active(self) -> None:
        """Nothing is playing, so the node does not exist. ACTIVE here would be a false claim.

        Connectedness cannot come from the node map -- when nothing plays there are no phone
        nodes at all -- so it is passed explicitly, the way raw_hfp_sink_present already is.
        BlueZ is the authority, via the same path call_role_acceptance already uses.
        """
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        graph.tick(idle_nodes(settings, media=False), [], settings.lark_node, phone_connected=True)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_READY)

    def test_existing_link_is_not_duplicated(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(idle_nodes(settings), links, settings.lark_node)
        linker.assert_not_called()

    def test_wrong_target_is_removed_before_aux_is_created(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        decoy = "alsa_output.usb-decoy.analog-stereo"
        nodes = {**idle_nodes(settings), decoy: {}}
        with (
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
            mock.patch.object(supervisor, "link", return_value=True) as linker,
        ):
            graph.tick(nodes, [(PHONE_MEDIA, decoy)], settings.lark_node)
        unlinker.assert_called_once_with(PHONE_MEDIA, decoy)
        linker.assert_not_called()
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)

    def test_wrong_target_unlink_failure_is_retried(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        decoy = "alsa_output.usb-decoy.analog-stereo"
        nodes = {**idle_nodes(settings), decoy: {}}
        wrong = (PHONE_MEDIA, decoy)
        with mock.patch.object(supervisor, "unlink", return_value=False) as unlinker:
            graph.tick(nodes, [wrong], settings.lark_node)
            graph.tick(nodes, [wrong], settings.lark_node)
        self.assertEqual(unlinker.call_count, 2)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertIn("could not be removed", graph.phone_failure_reason or "")

    def test_stereo_port_repetition_is_one_healthy_node_level_route(self) -> None:
        """pw-link repeats FL/FR from both endpoint views; that is not four routes."""

        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        expected = (PHONE_MEDIA, settings.wired_output)
        with (
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
            mock.patch.object(supervisor, "link", return_value=True) as linker,
        ):
            graph.tick(idle_nodes(settings), [expected] * 4, settings.lark_node)
        unlinker.assert_not_called()
        linker.assert_not_called()
        self.assertTrue(graph.media_route_verified)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

    def test_missing_output_is_actionable_not_silent(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = {settings.lark_node: {}, PHONE_MEDIA: media_props()}
        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(nodes, [], settings.lark_node, output_node=None)
        linker.assert_not_called()
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertIsNotNone(graph.last_failure)
        self.assertIsNotNone(graph.phone_failure_reason)

    def test_wrong_class_a2dp_candidate_is_unlinked_and_quarantined(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        props = media_props()
        props["media.class"] = "Audio/Source"
        nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
            PHONE_MEDIA: props,
        }
        route = (PHONE_MEDIA, settings.wired_output)
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(nodes, [route], settings.lark_node, phone_connected=True)
        unlinker.assert_called_once_with(*route)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertFalse(graph.media_route_verified)
        self.assertIsNone(graph.media_node)
        self.assertIn("lacks", graph.phone_failure_reason or "")

    def test_media_aux_volume_is_independent_of_dynamic_call_output(self) -> None:
        settings = supervisor.Settings(
            aec=supervisor.AecSettings(enabled=True),
            wired_output_volume=0.95,
        )
        graph = supervisor.CallGraph(settings)
        decoy = "alsa_output.usb-decoy.analog-stereo"
        nodes = {**idle_nodes(settings), decoy: {}}
        links = [(PHONE_MEDIA, settings.wired_output)]
        with (
            mock.patch.object(
                supervisor,
                "set_and_verify_sink_volume",
                return_value=(False, 0.40, "AUX volume readback mismatch"),
            ) as volume,
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
        ):
            graph.tick(nodes, links, settings.lark_node, output_node=decoy)

        volume.assert_called_once_with(settings.wired_output, 0.95)
        unlinker.assert_called_once_with(PHONE_MEDIA, settings.wired_output)
        self.assertEqual(graph.output_node, decoy)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertFalse(graph.media_route_verified)
        status = graph.status(nodes, links, settings.lark_node)
        self.assertFalse(status["phone"]["route_verified"])
        self.assertEqual(status["phone"]["failure_reason"], "AUX volume readback mismatch")

    def test_no_phone_idle_does_not_take_independent_media_volume_ownership(self) -> None:
        settings = supervisor.Settings(
            aec=supervisor.AecSettings(enabled=True),
            wired_output_volume=0.95,
        )
        graph = supervisor.CallGraph(settings)
        decoy = "alsa_output.usb-decoy.analog-stereo"
        nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
            decoy: {},
        }
        with mock.patch.object(supervisor, "set_and_verify_sink_volume") as volume:
            graph.tick(
                nodes,
                [],
                settings.lark_node,
                output_node=decoy,
                phone_connected=False,
            )
        volume.assert_not_called()
        self.assertEqual(graph.output_node, decoy)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.ABSENT)


class ControllerBindingAndForeignMediaTests(unittest.TestCase):
    def test_connected_phone_on_rejected_controller_is_quarantined(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        reason = "Pixel is also connected on another controller"
        with (
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
            mock.patch.object(supervisor, "link", return_value=True) as linker,
        ):
            graph.tick(
                idle_nodes(settings),
                [route],
                settings.lark_node,
                phone_connected=True,
                phone_binding_accepted=False,
                phone_binding_error=reason,
            )
        unlinker.assert_called_once_with(*route)
        linker.assert_not_called()
        status = graph.status(idle_nodes(settings), [route], settings.lark_node)
        self.assertTrue(status["phone"]["connected"])
        self.assertFalse(status["phone"]["controller_binding_accepted"])
        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.DEGRADED.value)
        self.assertEqual(status["phone"]["failure_reason"], reason)

    def test_rejected_binding_preserves_raw_android_microphone_transport_truth(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        accepted_nodes = idle_nodes(settings, media=False)
        reason = "call controller identity is ambiguous"
        graph.tick(
            accepted_nodes,
            [],
            settings.lark_node,
            phone_connected=True,
            phone_binding_accepted=False,
            phone_binding_error=reason,
            raw_hfp_nodes_present=True,
        )
        status = graph.status(accepted_nodes, [], settings.lark_node)
        self.assertTrue(status["phone"]["android_microphone_transport"])
        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.DEGRADED.value)
        self.assertEqual(status["phone"]["failure_reason"], reason)

    def test_rejected_binding_with_foreign_media_cleans_both_route_sets(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        configured_route = (PHONE_MEDIA, settings.wired_output)
        foreign_route = (FOREIGN_MEDIA, settings.wired_output)
        reason = "call controller identity is ambiguous"
        nodes = {**idle_nodes(settings), FOREIGN_MEDIA: media_props()}

        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(
                nodes,
                [configured_route, foreign_route],
                settings.lark_node,
                phone_connected=True,
                phone_binding_accepted=False,
                phone_binding_error=reason,
            )

        self.assertEqual(
            {call.args for call in unlinker.call_args_list},
            {configured_route, foreign_route},
        )
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertIn(reason, graph.phone_failure_reason or "")

    def test_foreign_media_is_quarantined_when_configured_phone_is_absent(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
            FOREIGN_MEDIA: media_props(),
        }
        route = (FOREIGN_MEDIA, settings.wired_output)
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(nodes, [route], settings.lark_node, phone_connected=False)
        unlinker.assert_called_once_with(*route)
        status = graph.status(nodes, [route], settings.lark_node)
        self.assertFalse(status["phone"]["connected"])
        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.DEGRADED.value)
        self.assertIn("foreign A2DP", status["phone"]["failure_reason"])

    def test_partially_identified_foreign_media_route_is_quarantined(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
            FOREIGN_MEDIA: {"api.bluez5.profile": "a2dp-source"},
        }
        route = (FOREIGN_MEDIA, settings.wired_output)
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(nodes, [route], settings.lark_node, phone_connected=False)

        unlinker.assert_called_once_with(*route)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertIn("foreign A2DP", graph.phone_failure_reason or "")

    def test_foreign_hfp_input_is_not_swept_as_media(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        foreign_hfp = f"bluez_input.{FOREIGN_PHONE_MAC.replace(':', '_')}.7"
        nodes = {
            settings.lark_node: {},
            settings.wired_output: {},
            foreign_hfp: {
                "api.bluez5.profile": "headset-audio-gateway",
                "media.class": "Audio/Source",
            },
        }
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(nodes, [], settings.lark_node, phone_connected=False)

        unlinker.assert_not_called()
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.ABSENT)

    def test_foreign_media_does_not_displace_or_get_adopted_with_configured_phone(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        configured_route = (PHONE_MEDIA, settings.wired_output)
        foreign_route = (FOREIGN_MEDIA, settings.wired_output)
        nodes = {**idle_nodes(settings), FOREIGN_MEDIA: media_props()}
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(nodes, [configured_route, foreign_route], settings.lark_node)
        unlinker.assert_called_once_with(*foreign_route)
        self.assertNotIn(configured_route, [call.args for call in unlinker.call_args_list])
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)

        graph.tick(idle_nodes(settings), [configured_route], settings.lark_node)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)


class TransportTransitionTests(unittest.TestCase):
    """Break-before-make applies to the phone transport, not only to microphones."""

    def test_media_link_is_dropped_before_the_call_graph_is_built(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        graph.tick(idle_nodes(settings), [(PHONE_MEDIA, settings.wired_output)], settings.lark_node)

        nodes = dict(call_nodes(settings))
        nodes[PHONE_MEDIA] = media_props()
        with (
            mock.patch.object(supervisor, "NativeAecHost", side_effect=FakeHost) as host,
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
        ):
            graph.tick(nodes, [(PHONE_MEDIA, settings.wired_output)], settings.lark_node)
        self.assertIn(
            (PHONE_MEDIA, settings.wired_output),
            [call.args for call in unlinker.call_args_list],
        )
        host.assert_not_called()
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.SWITCHING)

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

    def test_call_teardown_waits_for_a_new_media_node_before_routing(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            build_call(graph, settings)
            with mock.patch.object(supervisor, "link", return_value=True) as linker:
                graph.tick(
                    idle_nodes(settings, media=False), [], settings.lark_node, phone_connected=True
                )
        linker.assert_not_called()
        self.assertIsNone(graph.media_node)
        self.assertEqual(
            graph.phone_transport,
            supervisor.PhoneTransport.MEDIA_RESTORED_APP_PAUSED,
        )

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
                idle_nodes(settings, media=False),
                [],
                settings.lark_node,
                phone_connected=True,
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
            graph.tick(
                idle_nodes(settings, media=False), [], settings.lark_node, phone_connected=True
            )
            graph.tick(idle_nodes(settings), links, settings.lark_node, phone_connected=True)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

    def test_lingering_pre_call_object_is_refused_after_hfp_teardown(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        graph.tick(
            idle_nodes(settings, media_object_id=101),
            [route],
            settings.lark_node,
        )

        call_with_lingering = {
            **call_nodes(settings),
            PHONE_MEDIA: media_props(object_id=101),
        }
        with mock.patch.object(supervisor, "unlink", return_value=True):
            graph.tick(call_with_lingering, [route], settings.lark_node)

        with (
            mock.patch.object(supervisor, "unlink", return_value=True) as unlinker,
            mock.patch.object(supervisor, "link", return_value=True) as linker,
        ):
            graph.tick(
                idle_nodes(settings, media_object_id=101),
                [route],
                settings.lark_node,
                phone_connected=True,
            )
        unlinker.assert_called_once_with(*route)
        linker.assert_not_called()
        self.assertEqual(
            graph.phone_transport,
            supervisor.PhoneTransport.MEDIA_RESTORED_APP_PAUSED,
        )
        self.assertFalse(graph.media_route_verified)

    def test_changed_post_call_object_id_is_accepted_as_fresh(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        graph.tick(
            idle_nodes(settings, media_object_id=101),
            [route],
            settings.lark_node,
        )
        call_with_old_media = {
            **call_nodes(settings),
            PHONE_MEDIA: media_props(object_id=101),
        }
        with mock.patch.object(supervisor, "unlink", return_value=True):
            graph.tick(call_with_old_media, [route], settings.lark_node)

        graph.tick(
            idle_nodes(settings, media_object_id=202),
            [route],
            settings.lark_node,
            phone_connected=True,
        )
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)
        self.assertTrue(graph.media_route_verified)

    def test_observed_absence_allows_fresh_arrival_without_object_id(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        graph.tick(idle_nodes(settings), [route], settings.lark_node)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
        ):
            graph.tick(call_nodes(settings), [], settings.lark_node)
            graph.tick(
                idle_nodes(settings),
                [route],
                settings.lark_node,
                phone_connected=True,
            )
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

    def test_media_route_reappearing_during_active_call_tears_down_before_rebuild(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        media_route = (PHONE_MEDIA, settings.wired_output)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            call_links = build_call(graph, settings)
            self.assertEqual(graph.state, supervisor.State.ACTIVE)

            conflicted_nodes = {**call_nodes(settings), PHONE_MEDIA: media_props(object_id=303)}
            with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
                graph.tick(
                    conflicted_nodes,
                    [*call_links, media_route],
                    settings.lark_node,
                )

            self.assertIn(media_route, [call.args for call in unlinker.call_args_list])
            self.assertIsNone(graph.aec_host)
            self.assertIsNone(graph.microphone)
            self.assertIsNone(graph.callout)
            self.assertFalse(graph.verified)
            self.assertEqual(graph.state, supervisor.State.SWITCHING)
            self.assertNotEqual(graph.phone_transport, supervisor.PhoneTransport.CALL)

            # Only a later snapshot with no media route may construct the replacement graph.
            graph.tick(call_nodes(settings), [], settings.lark_node)
            self.assertIsNotNone(graph.aec_host)
            self.assertEqual(graph.state, supervisor.State.BUILDING)

    def test_foreign_media_without_route_blocks_call_and_preserves_post_call_history(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            call_links = build_call(graph, settings)
            self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.CALL)

            conflicted_nodes = {**call_nodes(settings), FOREIGN_MEDIA: media_props()}
            with mock.patch.object(supervisor, "unlink", return_value=True):
                graph.tick(conflicted_nodes, call_links, settings.lark_node)

            self.assertIsNone(graph.aec_host)
            self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
            self.assertIn("foreign A2DP", graph.phone_failure_reason or "")

            graph.tick(
                idle_nodes(settings, media=False),
                [],
                settings.lark_node,
                phone_connected=True,
            )

        self.assertEqual(
            graph.phone_transport,
            supervisor.PhoneTransport.MEDIA_RESTORED_APP_PAUSED,
        )

    def test_disconnect_with_foreign_media_cleans_all_routes_and_resets_call_history(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        configured_route = (PHONE_MEDIA, settings.wired_output)
        foreign_route = (FOREIGN_MEDIA, settings.wired_output)
        graph.tick(idle_nodes(settings), [configured_route], settings.lark_node)

        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            build_call(graph, settings)
            disconnected_nodes = {
                settings.lark_node: {},
                settings.wired_output: {},
                FOREIGN_MEDIA: media_props(),
            }
            with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
                graph.tick(
                    disconnected_nodes,
                    [configured_route, foreign_route],
                    settings.lark_node,
                    phone_connected=False,
                )

            self.assertEqual(
                {call.args for call in unlinker.call_args_list},
                {configured_route, foreign_route},
            )
            self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)

            graph.tick(
                idle_nodes(settings, media=False),
                [],
                settings.lark_node,
                phone_connected=True,
            )

        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_READY)


class ReconnectTests(unittest.TestCase):
    def test_disconnect_requests_stale_route_removal_and_reports_pending_cleanup(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        graph.tick(idle_nodes(settings), [route], settings.lark_node)
        nodes = {settings.lark_node: {}, settings.wired_output: {}}
        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(nodes, [route], settings.lark_node, phone_connected=False)
        unlinker.assert_called_once_with(*route)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.ABSENT)
        self.assertIsNone(graph.media_node)
        status = graph.status(nodes, [], settings.lark_node)
        self.assertIn("awaiting", status["phone"]["failure_reason"])
        self.assertIn("removing", status["phone"]["transition_reason"])

    def test_disconnect_unlink_failure_is_exposed_and_retried(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        graph.tick(idle_nodes(settings), [route], settings.lark_node)
        nodes = {settings.lark_node: {}, settings.wired_output: {}}
        with mock.patch.object(supervisor, "unlink", return_value=False) as unlinker:
            graph.tick(nodes, [route], settings.lark_node, phone_connected=False)
            graph.tick(nodes, [route], settings.lark_node, phone_connected=False)
        self.assertEqual(unlinker.call_count, 2)
        status = graph.status(nodes, [], settings.lark_node)
        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.ABSENT.value)
        self.assertIn("could not be removed", status["phone"]["failure_reason"])

    def test_reconnect_cleans_stale_route_before_recreating_and_verifying(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        graph.tick(idle_nodes(settings), [route], settings.lark_node)
        disconnected = {settings.lark_node: {}, settings.wired_output: {}}
        with mock.patch.object(supervisor, "unlink", return_value=True):
            graph.tick(disconnected, [route], settings.lark_node, phone_connected=False)

        with mock.patch.object(supervisor, "unlink", return_value=True) as unlinker:
            graph.tick(
                idle_nodes(settings),
                [route],
                settings.lark_node,
                phone_connected=True,
            )
        unlinker.assert_called_once_with(*route)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)

        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(
                idle_nodes(settings),
                [],
                settings.lark_node,
                phone_connected=True,
            )
        linker.assert_called_once_with(*route)
        self.assertFalse(graph.media_route_verified)
        graph.tick(idle_nodes(settings), [route], settings.lark_node, phone_connected=True)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

    def test_media_link_failure_retries_until_a_fresh_snapshot_verifies(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        route = (PHONE_MEDIA, settings.wired_output)
        with mock.patch.object(supervisor, "link", return_value=False) as linker:
            graph.tick(idle_nodes(settings), [], settings.lark_node)
        linker.assert_called_once_with(*route)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.DEGRADED)
        self.assertIn("could not route", graph.phone_failure_reason or "")

        with mock.patch.object(supervisor, "link", return_value=True) as linker:
            graph.tick(idle_nodes(settings), [], settings.lark_node)
        linker.assert_called_once_with(*route)
        self.assertFalse(graph.media_route_verified)
        graph.tick(idle_nodes(settings), [route], settings.lark_node)
        self.assertEqual(graph.phone_transport, supervisor.PhoneTransport.MEDIA_ACTIVE)

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
        for media in (True, False):
            with self.subTest(media_playing=media):
                graph = supervisor.CallGraph(settings)
                with mock.patch.object(supervisor, "link", return_value=True) as linker:
                    graph.tick(
                        idle_nodes(settings, media=media),
                        [],
                        settings.lark_node,
                        phone_connected=True,
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
        self.assertEqual(status["phone"]["expected_target"], settings.wired_output)
        self.assertTrue(status["phone"]["route_verified"])
        self.assertIsNone(status["phone"]["failure_reason"])

    def test_graph_inspection_failure_revokes_a_previously_verified_media_claim(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = idle_nodes(settings)
        links = [(PHONE_MEDIA, settings.wired_output)]
        graph.tick(nodes, links, settings.lark_node)

        graph.fail("PipeWire graph could not be inspected")
        status = graph.status({}, [], settings.lark_node, phone_connected=True)

        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.DEGRADED.value)
        self.assertFalse(status["phone"]["route_verified"])
        self.assertEqual(status["phone"]["failure_reason"], "PipeWire graph could not be inspected")

    def test_startup_graph_inspection_failure_records_current_connected_phone(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        reason = "PipeWire graph could not be inspected"

        graph.fail(
            reason,
            phone_connected=True,
            phone_binding_accepted=True,
            phone_binding_error=None,
            raw_hfp_nodes_present=False,
        )
        status = graph.status(
            {},
            [],
            settings.lark_node,
            phone_connected=True,
            phone_binding_accepted=True,
            phone_binding_error=None,
            raw_hfp_nodes_present=False,
        )

        self.assertTrue(status["phone"]["connected"])
        self.assertTrue(status["phone"]["controller_binding_accepted"])
        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.DEGRADED.value)
        self.assertFalse(status["phone"]["route_verified"])
        self.assertEqual(status["phone"]["failure_reason"], reason)

    def test_call_output_volume_failure_is_actionable_phone_degraded(self) -> None:
        settings = supervisor.Settings(
            aec=supervisor.AecSettings(enabled=True),
            wired_output_volume=0.95,
        )
        graph = supervisor.CallGraph(settings)
        reason = "call AUX volume readback mismatch"
        with mock.patch.object(
            supervisor,
            "set_and_verify_sink_volume",
            return_value=(False, 0.30, reason),
        ):
            graph.tick(call_nodes(settings), [], settings.lark_node)
        status = graph.status(call_nodes(settings), [], settings.lark_node)
        self.assertEqual(status["state"], supervisor.State.SAFE.value)
        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.DEGRADED.value)
        self.assertFalse(status["phone"]["route_verified"])
        self.assertEqual(status["phone"]["failure_reason"], reason)

    def test_call_failure_persists_on_same_generation_backoff_tick(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        with (
            mock.patch.object(supervisor, "NativeAecHost", FakeHost),
            mock.patch.object(supervisor, "Loopback", FakeLoopback),
            mock.patch.object(supervisor, "set_aec_mute", return_value=True),
        ):
            build_call(graph, settings)
            graph.attempts = supervisor.MAX_BUILD_ATTEMPTS - 1
            graph.fail("native AEC owner exited")
            self.assertEqual(graph.state, supervisor.State.FAILED)
            graph.tick(call_nodes(settings), [], settings.lark_node)
        status = graph.status(call_nodes(settings), [], settings.lark_node)
        self.assertEqual(status["phone"]["transport"], supervisor.PhoneTransport.DEGRADED.value)
        self.assertEqual(status["phone"]["failure_reason"], "native AEC owner exited")

    def test_disconnected_status_is_not_confused_with_idle(self) -> None:
        settings = call_settings()
        graph = supervisor.CallGraph(settings)
        nodes = {settings.lark_node: {}, settings.wired_output: {}}
        graph.tick(nodes, [], settings.lark_node, phone_connected=False)
        status = graph.status(nodes, [], settings.lark_node)
        self.assertFalse(status["phone"]["connected"])
        self.assertEqual(
            status["phone"]["transport"],
            supervisor.PhoneTransport.ABSENT.value,
        )


def test_phone_media_stream_is_pinned_to_the_configured_output() -> None:
    """The guard ADR-0009 makes part of the decision rather than implementation detail.

    65-bridge-hfp-no-autolink.conf covers `headset-audio-gateway` only, and says in as many
    words that A2DP nodes keep WirePlumber's default policy. Restoring the a2dp_sink role
    without a rule here hands the phone's media to whatever sink happens to be default --
    measured 2026-08-27 against a decoy default sink, the unpinned stream followed the decoy.

    The mechanism is `target.object`, NOT `node.autoconnect = false`. That was ADR-0009's
    original instruction and it was measured to break the feature outright: the phone's media
    is a Stream/Output/Audio client stream whose transport is acquired *because* the session
    manager links it, so disabling autoconnect means nothing links it, the transport is never
    acquired, and no node ever appears. Transport idle, zero nodes, zero links, no audio.
    """
    policy = (
        ROOT / "pi" / "wireplumber" / "wireplumber.conf.d" / "66-bridge-a2dp-source-target.conf"
    ).read_text(encoding="utf-8")

    assert 'api.bluez5.profile = "a2dp-source"' in policy
    assert "target.object" in policy
    # The mechanism that was measured to kill the stream must never come back.
    assert "node.autoconnect" not in policy
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
