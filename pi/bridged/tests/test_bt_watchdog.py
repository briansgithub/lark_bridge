from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import bt_watchdog
import btadapters


class SpeakerReconnectTests(unittest.TestCase):
    def test_speaker_retry_runs_when_due_and_resets_after_success(self) -> None:
        wanted = ("C9:5C:FD:6E:28:46", "A0:AD:9F:73:6C:24", "hci1")
        with (
            mock.patch.object(bt_watchdog, "desired_speaker", return_value=wanted),
            mock.patch.object(bt_watchdog, "reconnect_speaker", return_value=True) as reconnect,
            mock.patch.object(bt_watchdog.time, "monotonic", side_effect=[100.0, 101.0]),
        ):
            attempts, next_try = bt_watchdog.service_speaker_reconnect(2, 99.0)
        reconnect.assert_called_once_with(*wanted)
        self.assertEqual(attempts, 0)
        self.assertEqual(next_try, 101.0 + bt_watchdog.SPEAKER_RETRY_SECONDS)

    def test_speaker_retry_does_not_depend_on_call_controller_probe(self) -> None:
        wanted = ("C9:5C:FD:6E:28:46", "A0:AD:9F:73:6C:24", "hci1")
        with (
            mock.patch.object(bt_watchdog, "desired_speaker", return_value=wanted),
            mock.patch.object(bt_watchdog, "controller_answers", return_value=False) as probe,
            mock.patch.object(bt_watchdog, "reconnect_speaker", return_value=False) as reconnect,
            mock.patch.object(bt_watchdog.time, "monotonic", side_effect=[100.0, 101.0]),
        ):
            attempts, next_try = bt_watchdog.service_speaker_reconnect(0, 0.0)
        probe.assert_not_called()
        reconnect.assert_called_once_with(*wanted)
        self.assertEqual(attempts, 1)
        self.assertEqual(next_try, 101.0 + bt_watchdog.SPEAKER_RETRY_SECONDS)

    def test_busy_radio_lock_skips_without_spending_attempt_or_deadline(self) -> None:
        wanted = ("C9:5C:FD:6E:28:46", "A0:AD:9F:73:6C:24", "hci1")
        with (
            mock.patch.object(bt_watchdog, "desired_speaker", return_value=wanted),
            mock.patch.object(
                bt_watchdog.btadapters,
                "speaker_radio_lock",
                return_value=nullcontext(False),
            ) as lock,
            mock.patch.object(bt_watchdog, "reconnect_speaker") as reconnect,
            mock.patch.object(bt_watchdog.time, "monotonic", return_value=100.0),
        ):
            attempts, next_try = bt_watchdog.service_speaker_reconnect(2, 99.0)
        self.assertEqual((attempts, next_try), (2, 99.0))
        reconnect.assert_not_called()
        lock.assert_called_once_with(bt_watchdog.RADIO_LOCK_PATH, blocking=False)

    def test_status_round_trips_permanent_adapter_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            status.write_text(
                json.dumps(
                    {
                        "output": {
                            "desired_id": "a2dp:C9:5C:FD:6E:28:46",
                            "candidates": [
                                {
                                    "id": "a2dp:C9:5C:FD:6E:28:46",
                                    "address": "C9:5C:FD:6E:28:46",
                                    "adapter_address": "A0:AD:9F:73:6C:24",
                                    "adapter": "hci7",
                                    "connected": False,
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(bt_watchdog, "STATUS_PATH", str(status)):
                wanted = bt_watchdog.desired_speaker()
        self.assertEqual(
            wanted,
            ("C9:5C:FD:6E:28:46", "A0:AD:9F:73:6C:24", "hci7"),
        )

    def test_trust_failure_prevents_a_misleading_connect_attempt(self) -> None:
        adapter = btadapters.Adapter("hci1", "A0:AD:9F:73:6C:24", "USB", 1)
        failed = btadapters.TrustPinResult(False, failures=("D-Bus refused",))
        with (
            mock.patch.object(
                bt_watchdog.btadapters, "adapter_by_address", return_value=adapter
            ),
            mock.patch.object(
                bt_watchdog.btadapters, "power_on", return_value=(True, "powered")
            ),
            mock.patch.object(bt_watchdog.btadapters, "pin_to_adapter", return_value=failed),
            mock.patch.object(bt_watchdog.btadapters, "connect_profile") as connect,
        ):
            self.assertFalse(
                bt_watchdog.reconnect_speaker(
                    "C9:5C:FD:6E:28:46", adapter.address, "hci7"
                )
            )
        connect.assert_not_called()

    def test_power_failure_prevents_trust_and_connect(self) -> None:
        adapter = btadapters.Adapter("hci7", "A0:AD:9F:73:6C:24", "USB", 4)
        with (
            mock.patch.object(
                bt_watchdog.btadapters, "adapter_by_address", return_value=adapter
            ) as resolve,
            mock.patch.object(
                bt_watchdog.btadapters,
                "power_on",
                return_value=(False, "rfkill refused"),
            ),
            mock.patch.object(bt_watchdog.btadapters, "pin_to_adapter") as pin,
            mock.patch.object(bt_watchdog.btadapters, "connect_profile") as connect,
        ):
            self.assertFalse(
                bt_watchdog.reconnect_speaker(
                    "C9:5C:FD:6E:28:46", adapter.address, "hci1"
                )
            )
        resolve.assert_called_once_with(adapter.address)
        pin.assert_not_called()
        connect.assert_not_called()

    def test_missing_permanent_address_never_falls_back_to_diagnostic_hci(self) -> None:
        with (
            mock.patch.object(bt_watchdog.btadapters, "adapters") as adapters,
            mock.patch.object(bt_watchdog.btadapters, "pin_to_adapter") as pin,
            mock.patch.object(bt_watchdog.btadapters, "connect_profile") as connect,
        ):
            self.assertFalse(
                bt_watchdog.reconnect_speaker("C9:5C:FD:6E:28:46", None, "hci1")
            )
        adapters.assert_not_called()
        pin.assert_not_called()
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
