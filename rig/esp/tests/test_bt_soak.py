import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bt_soak  # noqa: E402

DUMP_UP = """\
Bluetooth Status
  State:         ON
  ConnectionState: STATE_CONNECTED
  Bonded devices: 1
    XX:XX:XX:XX:61:5F(Public) => XX:XX:XX:XX:61:5F(Public) [BR/EDR] [0x240408] [ACL BR/EDR:Y LE:N] LarkBridge
  11-23 23:36:26.684 [XX:XX:XX:XX:61:5F] aclStateChangeCallback: State:Disconnected, transport:BR/EDR, Address type:Public, reason:8, Adapter state:ON
  11-23 23:36:41.485 [XX:XX:XX:XX:6C:24] aclStateChangeCallback: State:Connected, tranport:BR/EDR, Address type:Public, reason:0, Adapter state:ON
BluetoothActiveDeviceManager
  A2DP:
    Connected count: 1
      XX:XX:XX:XX:61:5F: LarkBridge <- ACTIVE
  HFP:
    Connected count: 1
      XX:XX:XX:XX:61:5F: LarkBridge <- ACTIVE
  HA:
    Connected count: 0
"""

DUMP_DOWN = """\
  State:         ON
  ConnectionState: STATE_DISCONNECTED
    XX:XX:XX:XX:61:5F(Public) => XX:XX:XX:XX:61:5F(Public) [BR/EDR] [ACL BR/EDR:N LE:N] LarkBridge
  A2DP:
    Connected count: 0
  HFP:
    Connected count: 0
"""


class ParseTests(unittest.TestCase):
    def test_link_up(self):
        state = bt_soak.parse_bluetooth_manager(DUMP_UP, "61:5f")
        self.assertTrue(state["link_up"])
        self.assertTrue(state["a2dp_active"] and state["hfp_active"])
        self.assertEqual(state["acl_history"], [
            {"pixel_time": "11-23 23:36:26.684", "state": "Disconnected", "reason": 8}
        ])

    def test_link_down_and_change(self):
        up = bt_soak.parse_bluetooth_manager(DUMP_UP, "61:5F")
        down = bt_soak.parse_bluetooth_manager(DUMP_DOWN, "61:5F")
        self.assertFalse(down["link_up"])
        self.assertTrue(down["bonded"])
        self.assertEqual(
            bt_soak.state_changes(up, down),
            ["acl_bredr", "a2dp_connected", "hfp_connected", "link_up"],
        )
        self.assertEqual(bt_soak.state_changes(None, down), [])


class ConsoleOptionTests(unittest.TestCase):
    def test_console_commands_parse_and_reject_bad_input(self):
        args = bt_soak.build_parser().parse_args(
            ["--seconds", "5", "--out", "x", "--console", "10:tap lark_in", "--console", "60:stats"])
        self.assertEqual(args.console, [(10.0, "tap lark_in"), (60.0, "stats")])
        for bad in ("0.5:stats", "x:stats", "10:", "10:tap; reboot", "10:Tap"):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    bt_soak.build_parser().parse_args(["--seconds", "5", "--out", "x", "--console", bad])


if __name__ == "__main__":
    unittest.main()
