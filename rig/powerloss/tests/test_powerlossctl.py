import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "powerlossctl.py"
SPEC = importlib.util.spec_from_file_location("powerlossctl", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load powerlossctl")
powerlossctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(powerlossctl)


class CampaignState(unittest.TestCase):
    def test_minimum_matrix_is_exactly_fifty_cuts(self) -> None:
        self.assertEqual(sum(powerlossctl.REQUIREMENTS.values()), 50)

    def test_pixel_chaos_schedule_is_seeded_and_targets_risky_windows(self) -> None:
        schedule = powerlossctl.pixel_chaos_schedule(20260901)

        self.assertEqual(schedule, powerlossctl.pixel_chaos_schedule(20260901))
        self.assertEqual(len(schedule), 5)
        delays = [int(case["state"].rsplit("-", 1)[1]) for case in schedule[:3]]
        self.assertEqual(delays[0], 1)
        self.assertIn(delays[1], range(3, 8))
        self.assertIn(delays[2], range(12, 19))
        self.assertEqual(
            [case["random_context"] for case in schedule[3:]],
            ["bluetooth-recovery", "persistent-write"],
        )
        self.assertEqual(
            powerlossctl.schedule_requirements(schedule),
            {
                schedule[0]["state"]: 1,
                schedule[1]["state"]: 1,
                schedule[2]["state"]: 1,
                "random": 2,
            },
        )

    def test_phone_recovery_acceptance_requires_fast_connected_pixel(self) -> None:
        recovered = {
            "details": {
                "bridge": {"phone": {"connected": True}},
                "call_watchdog": {
                    "bond_state": "connected",
                    "connected_monotonic": 19.4,
                    "repair_state": "idle",
                },
            }
        }

        accepted = powerlossctl.phone_recovery_acceptance(recovered, 25.0)

        self.assertTrue(accepted["ready"])
        self.assertEqual(accepted["failures"], [])

    def test_phone_recovery_acceptance_rejects_late_or_incomplete_recovery(self) -> None:
        recovered = {
            "details": {
                "bridge": {"phone": {"connected": False}},
                "call_watchdog": {
                    "bond_state": "trusted",
                    "connected_monotonic": 31.0,
                    "repair_state": "pairing_window",
                },
            }
        }

        accepted = powerlossctl.phone_recovery_acceptance(recovered, 25.0)

        self.assertFalse(accepted["ready"])
        self.assertEqual(len(accepted["failures"]), 4)
        self.assertIn("exceeding 25s", accepted["failures"][-1])

    def test_pixel_chaos_campaign_records_schedule_and_phone_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "safety.json"
            evidence.write_text("{}\n", encoding="utf-8")
            output = root / "campaign"
            arguments = argparse.Namespace(
                output=output,
                profile="pixel-chaos",
                safety_evidence=evidence,
                seed=20260901,
            )
            verified = mock.Mock(
                returncode=0,
                stdout=json.dumps({"backup_sha256": "backup-digest"}),
            )

            with (
                mock.patch.object(
                    powerlossctl.subprocess, "run", return_value=verified
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                powerlossctl.campaign_init(arguments)

            campaign = json.loads(
                (output / "campaign.json").read_text(encoding="utf-8")
            )
            self.assertEqual(campaign["profile"], "pixel-chaos")
            self.assertEqual(campaign["minimum_off_seconds"], 12.0)
            self.assertTrue(campaign["require_phone"])
            self.assertEqual(campaign["phone_connect_limit_seconds"], 25.0)
            self.assertEqual(len(campaign["schedule"]), 5)
            self.assertEqual(sum(campaign["requirements"].values()), 5)

    def test_abort_preserves_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            evidence = campaign / "evidence.json"
            evidence.write_text("{}\n", encoding="utf-8")
            evidence_hash = hashlib.sha256(evidence.read_bytes()).hexdigest()
            (campaign / "runs/run-1").mkdir(parents=True)
            (campaign / "campaign.json").write_text(
                json.dumps(
                    {
                        "safety_evidence": str(evidence),
                        "safety_evidence_sha256": evidence_hash,
                    }
                ),
                encoding="utf-8",
            )
            run = campaign / "runs/run-1/run.json"
            run.write_text(
                json.dumps({"phase": "ARMED", "run_id": "run-1"}), encoding="utf-8"
            )
            arguments = argparse.Namespace(
                campaign=campaign, reason="operator mistimed cut"
            )

            with contextlib.redirect_stdout(io.StringIO()):
                powerlossctl.abort(arguments)

            result = json.loads(run.read_text(encoding="utf-8"))
            self.assertEqual(result["phase"], "FAILED")
            self.assertIn("operator mistimed cut", result["failure"])


if __name__ == "__main__":
    unittest.main()
