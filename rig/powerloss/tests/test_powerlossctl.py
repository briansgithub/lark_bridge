import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "powerlossctl.py"
SPEC = importlib.util.spec_from_file_location("powerlossctl", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load powerlossctl")
powerlossctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(powerlossctl)


class CampaignState(unittest.TestCase):
    def test_minimum_matrix_is_exactly_fifty_cuts(self) -> None:
        self.assertEqual(sum(powerlossctl.REQUIREMENTS.values()), 50)

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
