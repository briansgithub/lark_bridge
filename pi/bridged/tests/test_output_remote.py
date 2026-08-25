from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import btadapters
import controller_roles
import output_remote

CALL_ADDRESS = "B0:82:E2:25:18:36"
OUTPUT_ADDRESS = "A0:AD:9F:73:6C:24"
PHONE_ADDRESS = "5C:33:7B:CB:BF:C5"


def roles() -> controller_roles.ControllerRoles:
    return controller_roles.parse_controller_roles(
        {
            "devices": {
                "phone": {
                    "address": PHONE_ADDRESS,
                    "adapter": CALL_ADDRESS,
                    "adapter_product": "ASUS USB-BT600",
                    "adapter_bus": "USB",
                    "adapter_usb_vendor_id": "0b05",
                    "adapter_usb_product_id": "1d70",
                },
                "output": {
                    "adapter": OUTPUT_ADDRESS,
                    "adapter_product": "ASUS USB-BT500",
                    "adapter_bus": "USB",
                    "adapter_usb_vendor_id": "0b05",
                    "adapter_usb_product_id": "1bf6",
                },
            },
            "bluetooth": {"allow_transitional_uart_call": False},
        }
    )


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

    def test_sdp_query_is_bound_to_the_permanent_call_address(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            "Service Name: LarkBridge Output Control\n"
            f"UUID 128: {output_remote.SERVICE_UUID}\nChannel: 17\n",
            "",
        )
        with mock.patch.object(output_remote.subprocess, "run", return_value=result) as run:
            self.assertEqual(
                output_remote.discover_channel(PHONE_ADDRESS, CALL_ADDRESS), 17
            )
        self.assertEqual(
            run.call_args.args[0],
            ["sdptool", "--device", CALL_ADDRESS, "browse", PHONE_ADDRESS],
        )


class BoundRfcommTests(unittest.TestCase):
    def test_outbound_socket_is_bound_to_the_resolved_call_controller(self) -> None:
        call = btadapters.Adapter(
            "hci4",
            CALL_ADDRESS,
            "USB",
            4,
            usb_vendor_id="0b05",
            usb_product_id="1d70",
        )
        settings = mock.Mock(controller_roles=roles())
        state = output_remote.RemoteState()

        class Client:
            def __init__(self) -> None:
                self.bound = None
                self.connected = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _value):
                return None

            def bind(self, value):
                self.bound = value

            def connect(self, value):
                self.connected = value

        client = Client()

        def served(*_args, **_kwargs) -> None:
            state.stopping.set()

        with (
            mock.patch.object(
                output_remote, "_resolve_call_controller", return_value=(call, {})
            ),
            mock.patch.object(output_remote, "discover_channel", return_value=17) as discover,
            mock.patch.object(output_remote.socket, "socket", return_value=client),
            mock.patch.object(output_remote, "serve_connection", side_effect=served),
        ):
            output_remote.run(settings, state=state)

        discover.assert_called_once_with(PHONE_ADDRESS, CALL_ADDRESS)
        self.assertEqual(client.bound, (CALL_ADDRESS, 0))
        self.assertEqual(client.connected, (PHONE_ADDRESS, 17))

    def test_onboard_pixel_connection_is_not_inferred_as_the_call_role(self) -> None:
        onboard = btadapters.Adapter("hci0", "B8:27:EB:43:8D:51", "UART", 0)
        tree = {
            btadapters.path_for(onboard, PHONE_ADDRESS): {
                "org.bluez.Device1": {
                    "Paired": {"data": True},
                    "Connected": {"data": True},
                }
            }
        }
        settings = mock.Mock(controller_roles=roles())
        with (
            mock.patch.object(output_remote.btadapters, "managed_objects", return_value=tree),
            mock.patch.object(output_remote.btadapters, "adapters", return_value=[onboard]),
            self.assertRaisesRegex(btadapters.BluetoothOperationError, "controller_missing"),
        ):
            output_remote._resolve_call_controller(settings)


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
