from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import output_remote

STATUS = {
    "output": {
        "desired_id": "a2dp:AA:BB:CC:DD:EE:FF",
        "chosen": {"id": "wired:jack"},
        "reason": "speaker away; using wire",
        "candidates": [
            {
                "id": "wired:jack",
                "label": "Built-in jack",
                "kind": "wired",
                "node": "alsa_output.secret",
                "present": True,
                "connected": True,
            },
            {
                "id": "a2dp:AA:BB:CC:DD:EE:FF",
                "label": "Car stereo",
                "kind": "a2dp",
                "node": None,
                "present": False,
                "connected": False,
            },
        ],
    }
}


class ChannelParsingTests(unittest.TestCase):
    def test_parses_android_service_channel(self) -> None:
        text = (
            "Service Name: Headset Gateway\n  Channel: 8\n"
            "Service Name: LarkBridge Output Control\n"
            f"  UUID 128: {output_remote.SERVICE_UUID}\n  Channel: 17\n"
        )
        self.assertEqual(output_remote.parse_rfcomm_channel(text), 17)

    def test_rejects_missing_or_invalid_channel(self) -> None:
        self.assertIsNone(output_remote.parse_rfcomm_channel("no service"))
        self.assertIsNone(output_remote.parse_rfcomm_channel("Channel: 31"))


class ProtocolTests(unittest.TestCase):
    def request(self, payload, runner=subprocess.run):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge-status.json"
            path.write_text(json.dumps(STATUS), encoding="utf-8")
            return output_remote.handle_request(
                payload,
                status_path=path,
                runner=runner,
                bridgectl_path=Path("/bridge/bridgectl.py"),
            )

    def test_list_exposes_human_state_but_not_pipewire_nodes(self) -> None:
        response = self.request({"id": 4, "op": "list"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["chosen_id"], "wired:jack")
        self.assertEqual(response["outputs"][1]["label"], "Car stereo")
        self.assertNotIn("node", response["outputs"][0])
        self.assertEqual(response["outputs"][0]["setup_state"], "ready")
        self.assertFalse(response["call_active"])

    def test_set_uses_durable_non_chiming_cli_path(self) -> None:
        seen = {}

        def runner(command, **_kwargs):
            seen["command"] = command
            return subprocess.CompletedProcess(command, 0, "chosen\n", "startup: slot b\n")

        response = self.request(
            {"id": 5, "op": "set", "output_id": "a2dp:AA:BB:CC:DD:EE:FF"},
            runner,
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["accepted_label"], "Car stereo")
        self.assertEqual(
            seen["command"][-3:],
            ["a2dp:AA:BB:CC:DD:EE:FF", "--remember", "--no-chime"],
        )

    def test_unknown_output_is_refused_without_running_cli(self) -> None:
        called = False

        def runner(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError

        response = self.request({"id": 6, "op": "set", "output_id": "bogus"}, runner)
        self.assertFalse(response["ok"])
        self.assertFalse(called)

    def test_needs_setup_output_is_refused_without_running_cli(self) -> None:
        changed = json.loads(json.dumps(STATUS))
        changed["output"]["candidates"][1]["setup_state"] = "needs_setup"
        called = False

        def runner(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge-status.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            response = output_remote.handle_request(
                {"id": 7, "op": "set", "output_id": "a2dp:AA:BB:CC:DD:EE:FF"},
                status_path=path,
                runner=runner,
            )
        self.assertFalse(response["ok"])
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
