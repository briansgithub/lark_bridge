from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from rig.bt500_aux import remote
from rig.bt500_aux.tests.test_harness import good_snapshot


class FakeRunner:
    def __init__(
        self,
        *,
        malformed_controllers: bool = False,
        status_path: Path | None = None,
    ) -> None:
        self.malformed_controllers = malformed_controllers
        self.status_path = status_path
        self.commands: list[tuple[str, ...]] = []

    def run(self, command, *, timeout: float = 15.0) -> remote.Result:
        words = tuple(str(item) for item in command)
        self.commands.append(words)
        joined = " ".join(words)
        if "controller_roles.py" in joined:
            if self.malformed_controllers:
                return remote.Result(0, "{bad", "")
            return remote.Result(
                0,
                json.dumps(
                    {
                        "ready": True,
                        "call": {
                            "ready": True,
                            "configured_address": "A0:AD:9F:73:6C:24",
                            "observed_address": "A0:AD:9F:73:6C:24",
                            "observed_bus": "USB",
                            "observed_usb_id": "0b05:1bf6",
                            "hci": "hci7",
                        },
                        "output": {
                            "required": False,
                            "configured": False,
                            "ready": True,
                            "reason": "wired-output",
                        },
                    }
                ),
                "",
            )
        if words[:2] == ("python", "-c") or (len(words) >= 2 and words[1] == "-c"):
            return remote.Result(0, "{}", "")
        if words[:2] == ("pw-link", "-l"):
            return remote.Result(0, "bridge.aec.source\n  |-> output.bridge.mic\n", "")
        if "pw-top" in words:
            return remote.Result(0, "R 1 1024 0 0 0 0 0 0 echo-cancel-playback\n", "")
        if words and words[0] == "systemctl":
            return remote.Result(
                0,
                (
                    "ActiveState=active\n"
                    "NRestarts=0\n"
                    "ExecMainStatus=0\n"
                    "ExecMainStartTimestampMonotonic=10\n"
                ),
                "",
            )
        if words[:2] == ("journalctl", "-k"):
            return remote.Result(0, "", "")
        if words and words[0] == "journalctl":
            return remote.Result(0, "clean journal\n", "")
        if words == ("lsusb",):
            return remote.Result(
                0,
                "Bus 001 Device 002: ID 0b05:1bf6 ASUS USB-BT500\n",
                "",
            )
        if words == ("lsusb", "-t"):
            return remote.Result(0, "/: Bus 001.Port 1: Dev 1\n", "")
        if words[:2] == ("hciconfig", "hci7") and words[-1] == "version":
            return remote.Result(0, "HCI Version: 5.1\n", "")
        if words[:3] == ("hcitool", "-i", "hci7"):
            return remote.Result(
                0, "< ACL 5C:33:7B:CB:BF:C5\n< eSCO 5C:33:7B:CB:BF:C5\n", ""
            )
        if words == ("hciconfig", "hci7"):
            return remote.Result(
                0, "RX bytes:100 acl:1 sco:200\nTX bytes:100 acl:1 sco:201\n", ""
            )
        if words == ("pw-dump",):
            return remote.Result(0, "[]\n", "")
        if words and words[0] == "busctl":
            if self.status_path is not None:
                state = "CALL_DOWN" if words[-1] == "Disconnect" else "ACTIVE"
                self.status_path.write_text(
                    json.dumps({"state": state}), encoding="utf-8"
                )
            return remote.Result(0, "", "")
        return remote.Result(127, "", f"unexpected command: {joined}")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_collects_every_required_evidence_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status.json"
            watchdog = root / "watchdog.json"
            status.write_text(json.dumps(good_snapshot()["status"]), encoding="utf-8")
            watchdog.write_text(json.dumps({"recoveries": 0}), encoding="utf-8")
            document = remote.snapshot(
                FakeRunner(),
                full=True,
                status_path=status,
                watchdog_path=watchdog,
            )

        self.assertEqual(document["collection_errors"], [])
        self.assertEqual(document["controllers"]["call"]["hci"], "hci7")
        self.assertTrue(document["transport"]["controller_answers"])
        self.assertTrue(document["transport"]["sco"])
        self.assertEqual(document["graph_quantum"], 1024)
        self.assertIn("bluez", document)
        self.assertIn("pipewire_dump", document)
        self.assertIn("services", document)
        self.assertIn("journals", document)
        self.assertIn("usb", document)

    def test_malformed_controller_json_is_explicit_collection_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status.json"
            watchdog = root / "watchdog.json"
            status.write_text(json.dumps(good_snapshot()["status"]), encoding="utf-8")
            watchdog.write_text(json.dumps({"recoveries": 0}), encoding="utf-8")
            document = remote.snapshot(
                FakeRunner(malformed_controllers=True),
                status_path=status,
                watchdog_path=watchdog,
            )
        self.assertTrue(
            any("controller status" in error for error in document["collection_errors"])
        )


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class SoakTests(unittest.TestCase):
    def test_soak_passes_after_full_duration_and_exact_sampling(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            code = remote.run_soak(
                output,
                duration=10,
                interval=5.0,
                snapshot_fn=lambda **_kwargs: copy.deepcopy(good_snapshot()),
                clock=clock,
                sleep=clock.sleep,
            )
            state = json.loads((output / "state.json").read_text(encoding="utf-8"))
            lines = (output / "samples.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(code, 0)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["elapsed_s"], 10.0)
        samples = [json.loads(line) for line in lines if "soak_elapsed_s" in line]
        self.assertEqual(len(samples), 2)

    def test_soak_stops_immediately_on_hard_failure(self) -> None:
        clock = FakeClock()
        calls = 0

        def snapshots(**_kwargs):
            nonlocal calls
            calls += 1
            document = good_snapshot()
            if calls >= 2:
                document["status"]["state"] = "CALL_DOWN"
            return document

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            code = remote.run_soak(
                output,
                duration=30,
                interval=5.0,
                snapshot_fn=snapshots,
                clock=clock,
                sleep=clock.sleep,
            )
            state = json.loads((output / "state.json").read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (output / "samples.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(code, 1)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(any(event.get("event") == "hard_failure" for event in events))
        self.assertLess(clock.value, 30)

    def test_interruption_checkpoints_and_resume_finishes_remaining_time(self) -> None:
        first_clock = FakeClock()
        calls = 0

        def interrupted(**_kwargs):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise KeyboardInterrupt
            return copy.deepcopy(good_snapshot())

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = remote.run_soak(
                output,
                duration=15,
                interval=5.0,
                snapshot_fn=interrupted,
                clock=first_clock,
                sleep=first_clock.sleep,
            )
            interrupted_state = json.loads(
                (output / "state.json").read_text(encoding="utf-8")
            )
            second_clock = FakeClock()
            second = remote.run_soak(
                output,
                duration=15,
                interval=5.0,
                resume=True,
                snapshot_fn=lambda **_kwargs: copy.deepcopy(good_snapshot()),
                clock=second_clock,
                sleep=second_clock.sleep,
            )
            final = json.loads((output / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(first, 130)
        self.assertEqual(interrupted_state["status"], "interrupted")
        self.assertEqual(second, 0)
        self.assertEqual(final["status"], "passed")
        self.assertEqual(final["runs"], 2)

    def test_resume_rejects_malformed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "state.json").write_text("{bad", encoding="utf-8")
            with self.assertRaises(remote.EvidenceError):
                remote.run_soak(
                    output,
                    duration=10,
                    interval=5.0,
                    resume=True,
                    snapshot_fn=lambda **_kwargs: copy.deepcopy(good_snapshot()),
                    clock=FakeClock(),
                    sleep=lambda _seconds: None,
                )


class RecycleTests(unittest.TestCase):
    def test_recycle_targets_exact_bt500_device_and_observes_both_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status.json"
            status.write_text(json.dumps({"state": "ACTIVE"}), encoding="utf-8")
            config = root / "bridge.toml"
            config.write_text(
                '[devices.phone]\naddress = "5C:33:7B:CB:BF:C5"\n',
                encoding="utf-8",
            )
            runner = FakeRunner(status_path=status)
            result = remote.recycle_call(
                runner,
                status_path=status,
                config_path=config,
                clock=lambda: 0.0,
                sleep=lambda _seconds: None,
            )

        self.assertEqual(result["verdict"], "PASS")
        calls = [command for command in runner.commands if command[0] == "busctl"]
        self.assertEqual(len(calls), 2)
        self.assertIn("/org/bluez/hci7/dev_5C_33_7B_CB_BF_C5", calls[0])
        self.assertEqual(calls[0][-1], "Disconnect")
        self.assertEqual(calls[1][-1], "Connect")


if __name__ == "__main__":
    unittest.main()
