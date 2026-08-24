from __future__ import annotations

import unittest
from argparse import Namespace
from unittest import mock

import bridgectl
import btadapters


def candidate(output_id: str, label: str, kind: str = "a2dp", present: bool = True) -> dict:
    return {
        "id": output_id,
        "kind": kind,
        "label": label,
        "node": "node" if present else None,
        "present": present,
        "connected": present,
        "adapter": "hci1" if kind == "a2dp" else None,
        "address": output_id.split(":", 1)[1] if kind == "a2dp" else None,
    }


WIRED = candidate("wired:alsa_output.platform-x.mailbox", "Built-in Audio Stereo", "wired")
BOOMBOX = candidate("a2dp:C9:5C:FD:6E:28:46", "Boombox")
IWORLD = candidate("a2dp:50:D7:1B:74:34:D6", "iWorld", present=False)
SOUNDCORE = candidate("a2dp:98:47:44:CD:73:DE", "Soundcore Space A40", present=False)
ALL = [WIRED, BOOMBOX, IWORLD, SOUNDCORE]


class SelectorTests(unittest.TestCase):
    """A selector a person cannot address in their own words is not a usable selector."""

    def test_exact_id_wins_before_any_fuzzy_matching(self) -> None:
        self.assertEqual(bridgectl.resolve_selector(BOOMBOX["id"], ALL), BOOMBOX)

    def test_list_index_is_one_based_like_the_printed_list(self) -> None:
        self.assertEqual(bridgectl.resolve_selector("2", ALL), BOOMBOX)

    def test_index_out_of_range_is_refused_with_the_count(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            bridgectl.resolve_selector("9", ALL)
        self.assertIn("there are 4", str(caught.exception))

    def test_name_fragment_is_case_insensitive(self) -> None:
        self.assertEqual(bridgectl.resolve_selector("boombox", ALL), BOOMBOX)
        self.assertEqual(bridgectl.resolve_selector("BOOM", ALL), BOOMBOX)

    def test_wired_is_a_reserved_word_for_the_jack(self) -> None:
        """The onboard sink's ALSA name contains nothing a person would type."""
        self.assertEqual(bridgectl.resolve_selector("wired", ALL), WIRED)

    def test_ambiguity_is_refused_rather_than_guessed(self) -> None:
        pair = [candidate("a2dp:AA:AA:AA:AA:AA:AA", "Kitchen speaker"),
                candidate("a2dp:BB:BB:BB:BB:BB:BB", "Bedroom speaker")]
        with self.assertRaises(SystemExit) as caught:
            bridgectl.resolve_selector("speaker", pair)
        self.assertIn("ambiguous", str(caught.exception))

    def test_no_match_names_the_help_command(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            bridgectl.resolve_selector("garage", ALL)
        self.assertIn("bridgectl output", str(caught.exception))

    def test_an_offline_speaker_is_still_selectable(self) -> None:
        """Choosing something switched off is the whole point of listing it."""
        self.assertEqual(bridgectl.resolve_selector("iworld", ALL), IWORLD)

    def test_matching_by_mac_fragment_works_for_scripts(self) -> None:
        self.assertEqual(bridgectl.resolve_selector("C9:5C", ALL), BOOMBOX)


class StatusParsingTests(unittest.TestCase):
    def test_missing_output_block_explains_the_likely_cause(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            bridgectl.outputs_of({"state": "ACTIVE"})
        self.assertIn("outputs.py", str(caught.exception))

    def test_call_detection_reads_the_hfp_flag(self) -> None:
        self.assertTrue(bridgectl._call_is_up({"call": {"hfp_nodes_present": True}}))
        self.assertFalse(bridgectl._call_is_up({"call": {"hfp_nodes_present": False}}))
        self.assertFalse(bridgectl._call_is_up({}))


class SelectionSafetyTests(unittest.TestCase):
    def test_trust_failure_does_not_record_the_selection(self) -> None:
        adapter = btadapters.Adapter("hci1", "A0:AD:9F:73:6C:24", "USB", 1)
        failed = btadapters.TrustPinResult(False, failures=("D-Bus refused",))
        status = {
            "call": {"hfp_nodes_present": False},
            "output": {"candidates": [BOOMBOX]},
        }
        args = Namespace(selector="boombox", connect=True, force=False, chime=False)
        with (
            mock.patch.object(bridgectl, "read_status", return_value=status),
            mock.patch.object(bridgectl.btadapters, "adapters", return_value=[adapter]),
            mock.patch.object(
                bridgectl.btadapters, "pin_to_adapter", return_value=failed
            ),
            mock.patch.object(bridgectl.supervisor, "write_desire") as write_desire,
        ):
            self.assertEqual(bridgectl.do_set(args), 1)
        write_desire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
