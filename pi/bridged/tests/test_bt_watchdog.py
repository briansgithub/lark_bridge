from __future__ import annotations

import unittest
from unittest import mock

import bt_watchdog
import btadapters


class SpeakerReconnectTests(unittest.TestCase):
    def test_trust_failure_prevents_a_misleading_connect_attempt(self) -> None:
        adapter = btadapters.Adapter("hci1", "A0:AD:9F:73:6C:24", "USB", 1)
        failed = btadapters.TrustPinResult(False, failures=("D-Bus refused",))
        with (
            mock.patch.object(bt_watchdog.btadapters, "adapters", return_value=[adapter]),
            mock.patch.object(bt_watchdog.btadapters, "pin_to_adapter", return_value=failed),
            mock.patch.object(bt_watchdog.btadapters, "connect_profile") as connect,
        ):
            self.assertFalse(
                bt_watchdog.reconnect_speaker("C9:5C:FD:6E:28:46", "hci1")
            )
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
