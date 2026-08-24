from __future__ import annotations

import unittest

import outputs


def sink(name: str, description: str = "", profile: str | None = None) -> dict:
    props = {"media.class": "Audio/Sink", "node.description": description or name}
    if profile:
        props["api.bluez5.profile"] = profile
    return props


def device(
    address: str,
    *,
    alias: str = "",
    connected: bool = False,
    a2dp: bool = True,
    paired: bool | None = None,
    extra_uuids: tuple[str, ...] = (),
) -> dict:
    uuids = list(extra_uuids)
    if a2dp:
        uuids.append(outputs.A2DP_SINK_UUID)
    properties = {
        "Address": {"data": address},
        "Alias": {"data": alias or address},
        "Connected": {"data": connected},
        "UUIDs": {"data": uuids},
    }
    if paired is not None:
        properties["Paired"] = {"data": paired}
    return {
        "org.bluez.Device1": {
            **properties,
        }
    }


def adapter(address: str) -> dict:
    return {"org.bluez.Adapter1": {"Address": {"data": address}}}


PHONE = "5C:33:7B:CB:BF:C5"
BOOMBOX = "C9:5C:FD:6E:28:46"
IWORLD = "50:D7:1B:74:34:D6"
ONBOARD = "alsa_output.platform-3f00b840.mailbox.stereo-fallback"
USB_DAC = "alsa_output.usb-Generic_AB13X_USB_Audio-00.analog-stereo"


class WiredTests(unittest.TestCase):
    def test_only_alsa_audio_sinks_qualify(self) -> None:
        nodes = {
            ONBOARD: sink(ONBOARD, "Built-in Audio Stereo"),
            "alsa_input.usb-Hollyland": {"media.class": "Audio/Source"},
            "bridge.aec.sink": {"media.class": "Audio/Sink"},
        }
        found = outputs.wired_outputs(nodes)
        self.assertEqual([o.node for o in found], [ONBOARD])
        self.assertEqual(found[0].kind, "wired")
        self.assertTrue(found[0].present)
        self.assertTrue(found[0].connected)

    def test_onboard_pwm_sorts_after_a_usb_dac(self) -> None:
        nodes = {ONBOARD: sink(ONBOARD), USB_DAC: sink(USB_DAC)}
        self.assertEqual([o.node for o in outputs.wired_outputs(nodes)], [USB_DAC, ONBOARD])

    def test_label_prefers_the_human_description(self) -> None:
        nodes = {ONBOARD: sink(ONBOARD, "Built-in Audio Stereo")}
        self.assertEqual(outputs.wired_outputs(nodes)[0].label, "Built-in Audio Stereo")


class NodeMatchingTests(unittest.TestCase):
    def test_profile_suffix_is_not_assumed_to_be_one(self) -> None:
        """The trailing number is a profile index, not a constant."""
        nodes = {f"bluez_output.{BOOMBOX.replace(':', '_')}.7": sink("x", profile="a2dp-sink")}
        self.assertIsNotNone(outputs.find_a2dp_node(nodes, BOOMBOX))

    def test_the_phones_hfp_sink_is_never_matched(self) -> None:
        """Routing the far end into the phone's own HFP sink would close a feedback loop."""
        nodes = {
            f"bluez_output.{PHONE.replace(':', '_')}.1": sink(
                "phone", profile="headset-audio-gateway"
            )
        }
        self.assertIsNone(outputs.find_a2dp_node(nodes, PHONE))

    def test_absent_speaker_has_no_node(self) -> None:
        self.assertIsNone(outputs.find_a2dp_node({}, BOOMBOX))


class A2dpEnumerationTests(unittest.TestCase):
    def test_phone_is_excluded_because_it_is_not_an_a2dp_sink(self) -> None:
        tree = {
            "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
            f"/org/bluez/hci0/dev_{PHONE.replace(':', '_')}": device(
                PHONE, alias="Pixel 7a", connected=True, a2dp=False
            ),
        }
        self.assertEqual(outputs.a2dp_outputs({}, tree), [])

    def test_offline_speakers_are_still_listed(self) -> None:
        """A selector that hides what is switched off cannot be used to switch it on."""
        tree = {
            f"/org/bluez/hci1/dev_{BOOMBOX.replace(':', '_')}": device(BOOMBOX, alias="MP43247")
        }
        found = outputs.a2dp_outputs({}, tree)
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].present)
        self.assertFalse(found[0].connected)
        self.assertEqual(found[0].label, "MP43247")

    def test_device_bonded_on_two_adapters_yields_one_entry(self) -> None:
        key = f"dev_{IWORLD.replace(':', '_')}"
        tree = {
            f"/org/bluez/hci0/{key}": device(IWORLD, alias="iWorld", connected=False),
            f"/org/bluez/hci1/{key}": device(IWORLD, alias="iWorld", connected=True),
        }
        found = outputs.a2dp_outputs({}, tree)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].adapter, "hci1", "must prefer the adapter it is connected on")

    def test_hci_name_is_not_accepted_as_speaker_controller_identity(self) -> None:
        key = f"dev_{IWORLD.replace(':', '_')}"
        tree = {
            f"/org/bluez/hci0/{key}": device(IWORLD),
            f"/org/bluez/hci1/{key}": device(IWORLD),
        }
        found = outputs.a2dp_outputs({}, tree, speaker_adapter="hci1")
        self.assertEqual(found[0].adapter, "hci0")
        self.assertEqual(found[0].setup_state, "needs_setup")

    def test_permanent_adapter_address_survives_hci_renumbering(self) -> None:
        key = f"dev_{IWORLD.replace(':', '_')}"
        speaker_radio = "A0:AD:9F:73:6C:24"
        tree = {
            "/org/bluez/hci4": adapter("B8:27:EB:43:8D:51"),
            "/org/bluez/hci7": adapter(speaker_radio),
            f"/org/bluez/hci4/{key}": device(IWORLD),
            f"/org/bluez/hci7/{key}": device(IWORLD),
        }
        found = outputs.a2dp_outputs({}, tree, speaker_adapter=speaker_radio)
        self.assertEqual(found[0].adapter, "hci7")
        self.assertEqual(found[0].adapter_address, speaker_radio)
        self.assertEqual(found[0].as_dict()["adapter_address"], speaker_radio)

    def test_connected_speakers_sort_first(self) -> None:
        tree = {
            f"/org/bluez/hci1/dev_{IWORLD.replace(':', '_')}": device(IWORLD, alias="ZZZ"),
            f"/org/bluez/hci1/dev_{BOOMBOX.replace(':', '_')}": device(
                BOOMBOX, alias="AAA", connected=True
            ),
        }
        self.assertEqual([o.label for o in outputs.a2dp_outputs({}, tree)], ["AAA", "ZZZ"])

    def test_wrong_controller_bond_is_visible_but_never_available_or_routable(self) -> None:
        speaker_radio = "A0:AD:9F:73:6C:24"
        node = f"bluez_output.{BOOMBOX.replace(':', '_')}.3"
        tree = {
            "/org/bluez/hci0": adapter("B8:27:EB:43:8D:51"),
            "/org/bluez/hci1": adapter(speaker_radio),
            f"/org/bluez/hci0/dev_{BOOMBOX.replace(':', '_')}": device(
                BOOMBOX, alias="Boombox", connected=True, paired=True
            ),
        }
        found = outputs.a2dp_outputs(
            {node: sink(node, profile="a2dp-sink")}, tree, speaker_adapter=speaker_radio
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].setup_state, "needs_setup")
        self.assertFalse(found[0].connected)
        self.assertFalse(found[0].present)
        self.assertIsNone(found[0].node)

        wired = outputs.Output("wired:jack", "wired", "jack", "jack", True)
        resolved = outputs.resolve(found[0].id, [wired, found[0]])
        self.assertEqual(resolved.chosen, wired)
        self.assertEqual(resolved.desired_id, found[0].id)

    def test_dedicated_bond_outranks_connected_wrong_controller_duplicate(self) -> None:
        key = f"dev_{IWORLD.replace(':', '_')}"
        speaker_radio = "A0:AD:9F:73:6C:24"
        tree = {
            "/org/bluez/hci0": adapter("B8:27:EB:43:8D:51"),
            "/org/bluez/hci7": adapter(speaker_radio),
            f"/org/bluez/hci0/{key}": device(IWORLD, connected=True, paired=True),
            f"/org/bluez/hci7/{key}": device(IWORLD, connected=False, paired=True),
        }
        found = outputs.a2dp_outputs({}, tree, speaker_adapter=speaker_radio)
        self.assertEqual(found[0].adapter, "hci7")
        self.assertEqual(found[0].setup_state, "ready")
        self.assertFalse(found[0].connected)


class DiscoveryResultTests(unittest.TestCase):
    def test_results_sort_shape_confidence_and_duplicate_discriminators(self) -> None:
        speaker_radio = "A0:AD:9F:73:6C:24"
        target = outputs.btadapters.Adapter("hci7", speaker_radio, "USB", 4)
        first = "AA:BB:CC:DD:28:46"
        second = "11:22:33:44:28:47"
        third = "22:33:44:55:66:77"
        tree = {
            outputs.btadapters.path_for(target, first): device(
                first, alias="Same Name", paired=True
            ),
            outputs.btadapters.path_for(target, second): device(
                second, alias="same name", a2dp=False, paired=False
            ),
            outputs.btadapters.path_for(target, third): {
                "org.bluez.Device1": {
                    "Address": {"data": third},
                    "Alias": {"data": ""},
                    "Name": {"data": ""},
                    "Class": {"data": 0x0400},
                    "Paired": {"data": False},
                    "UUIDs": {"data": []},
                }
            },
        }
        shaped = outputs.discovery_results(
            {first: -70, second: -40, third: None}, tree, target
        )
        self.assertEqual([item["output_id"] for item in shaped], [
            outputs.a2dp_id(second), outputs.a2dp_id(first), outputs.a2dp_id(third)
        ])
        self.assertEqual(shaped[0]["audio_confidence"], "unknown")
        self.assertEqual(shaped[1]["audio_confidence"], "confirmed")
        self.assertEqual(shaped[1]["setup_state"], "ready")
        self.assertEqual(shaped[2]["audio_confidence"], "likely")
        self.assertEqual(shaped[2]["label"], "Bluetooth device 66:77")
        self.assertEqual(shaped[0]["duplicate_name_discriminator"], "28:47")
        self.assertEqual(shaped[1]["duplicate_name_discriminator"], "28:46")


class ResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wired = outputs.Output(
            id=outputs.wired_id(ONBOARD), kind="wired", label="jack", node=ONBOARD, connected=True
        )
        self.live_speaker = outputs.Output(
            id=outputs.a2dp_id(BOOMBOX),
            kind="a2dp",
            label="MP43247",
            node="bluez_output.C9_5C_FD_6E_28_46.1",
            connected=True,
            address=BOOMBOX,
        )
        self.absent_speaker = outputs.Output(
            id=outputs.a2dp_id(IWORLD), kind="a2dp", label="iWorld", node=None,
            connected=False, address=IWORLD,
        )

    def test_available_desire_wins(self) -> None:
        got = outputs.resolve(self.live_speaker.id, [self.wired, self.live_speaker])
        self.assertEqual(got.chosen, self.live_speaker)
        self.assertTrue(got.desired_available)

    def test_unavailable_desire_falls_back_but_is_remembered(self) -> None:
        """The user's choice must survive the device being switched off."""
        got = outputs.resolve(self.absent_speaker.id, [self.wired, self.absent_speaker])
        self.assertEqual(got.chosen, self.wired)
        self.assertFalse(got.desired_available)
        self.assertEqual(got.desired_id, self.absent_speaker.id, "the desire must not be rewritten")

    def test_mode1_prefers_a_connected_speaker_when_the_desire_is_gone(self) -> None:
        got = outputs.resolve(
            self.absent_speaker.id,
            [self.wired, self.live_speaker, self.absent_speaker],
            prefer_speaker=True,
        )
        self.assertEqual(got.chosen, self.live_speaker)

    def test_mode1w_does_not_wander_onto_a_speaker_that_is_merely_switched_on(self) -> None:
        """Mode 1W is the proven configuration and its whole point is the wired output.

        Auto-selecting any bonded speaker that happened to be powered on nearby would be a
        regression in the shipped fallback wearing a feature's clothes.
        """
        got = outputs.resolve(None, [self.wired, self.live_speaker], prefer_speaker=False)
        self.assertEqual(got.chosen, self.wired)

    def test_an_explicit_choice_outranks_the_mode_default(self) -> None:
        got = outputs.resolve(
            self.live_speaker.id, [self.wired, self.live_speaker], prefer_speaker=False
        )
        self.assertEqual(got.chosen, self.live_speaker, "the user's choice must win")

    def test_no_desire_still_resolves(self) -> None:
        self.assertEqual(outputs.resolve(None, [self.wired]).chosen, self.wired)

    def test_fail_closed_returns_nothing_rather_than_guessing(self) -> None:
        got = outputs.resolve(
            self.absent_speaker.id, [self.wired, self.absent_speaker], fallback=False
        )
        self.assertIsNone(got.chosen)
        self.assertEqual(got.desired_id, self.absent_speaker.id)

    def test_unknown_desired_id_does_not_raise(self) -> None:
        got = outputs.resolve("a2dp:00:00:00:00:00:00", [self.wired])
        self.assertEqual(got.chosen, self.wired)
        self.assertFalse(got.desired_available)

    def test_nothing_present_at_all(self) -> None:
        got = outputs.resolve(None, [self.absent_speaker])
        self.assertIsNone(got.chosen)
        self.assertIn("no output", got.reason)


if __name__ == "__main__":
    unittest.main()
