from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import output_remote


class ScriptedSocket:
    def __init__(self, payload: bytes) -> None:
        self.input = io.BytesIO(payload)
        self.output = io.BytesIO()

    def makefile(self, *_args, **_kwargs):
        owner = self

        class Stream:
            def readline(self, limit: int) -> bytes:
                return owner.input.readline(limit)

            def write(self, value: bytes) -> int:
                return owner.output.write(value)

        return Stream()


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

    def test_list_and_status_keep_legacy_fields_and_types(self) -> None:
        listed = self.request({"id": 4, "op": "list"})
        status = self.request({"id": 9, "op": "status"})

        self.assertEqual(
            set(listed),
            {"id", "ok", "outputs", "desired_id", "chosen_id", "reason", "call_active"},
        )
        self.assertEqual(
            set(listed["outputs"][0]),
            {"id", "label", "kind", "available", "connected", "setup_state"},
        )
        self.assertIsInstance(listed["outputs"], list)
        self.assertIsInstance(listed["outputs"][0]["available"], bool)
        self.assertIsInstance(listed["outputs"][0]["connected"], bool)
        self.assertIsInstance(listed["reason"], str)
        self.assertEqual({**listed, "id": 9}, status)

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

    def test_nonintegral_or_structured_request_ids_are_not_reflected(self) -> None:
        for request_id in (1.5, True, [1], {"spoof": 1}):
            with self.subTest(request_id=request_id):
                response = self.request({"id": request_id, "op": "list"})
                self.assertEqual(response, {"id": None, "ok": False, "error": "invalid request id"})

    def test_oversized_line_cannot_smuggle_a_command_in_its_suffix(self) -> None:
        payload = (
            b"x" * (output_remote.MAX_LINE_BYTES + 1)
            + b'{"id":8,"op":"set","output_id":"wired:jack"}\n'
        )
        sock = ScriptedSocket(payload)
        runner_calls = []

        with mock.patch.object(
            output_remote,
            "handle_request",
            side_effect=lambda *_args, **_kwargs: runner_calls.append(True),
        ):
            output_remote.serve_connection(sock)

        frames = sock.output.getvalue().splitlines()
        self.assertEqual(len(frames), 1)
        self.assertEqual(json.loads(frames[0])["error"], "request is too large")
        self.assertEqual(runner_calls, [])

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
