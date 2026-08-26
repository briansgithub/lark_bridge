from __future__ import annotations

import json
import sys
from pathlib import Path

from rig.analysis import aec_status


def active_status(candidate_id: str, node: str) -> dict:
    return {
        "state": "ACTIVE",
        "generation": 3,
        "endpoints": {"microphone": node, "lark": node if candidate_id == "lark-a1" else None},
        "microphone": {"selected": {"id": candidate_id, "node": node}},
        "aec": {"enabled": True, "verified": True, "owner_pid": 123},
        "graph": {"unexpected_links": []},
    }


def test_active_fifine_status_requires_matching_identity(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(active_status("fifine-k054", "alsa_input.usb-FIFINE")),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aec_status.py",
            str(path),
            "--expect",
            "active",
            "--expected-microphone",
            "fifine-k054",
        ],
    )

    assert aec_status.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["microphone_id"] == "fifine-k054"


def test_active_status_rejects_wrong_microphone(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(active_status("fifine-k054", "alsa_input.usb-FIFINE")),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["aec_status.py", str(path), "--expect", "active"],
    )

    assert aec_status.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert any("expected 'lark-a1'" in failure for failure in result["failures"])


def test_legacy_lark_status_remains_accepted(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "status.json"
    status = active_status("lark-a1", "alsa_input.usb-LARK")
    status.pop("microphone")
    status["endpoints"].pop("microphone")
    path.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["aec_status.py", str(path), "--expect", "active"],
    )

    assert aec_status.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["microphone_id"] == "lark-a1"


def test_baseline_rejects_missing_status(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "missing.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["aec_status.py", str(path), "--expect", "baseline"],
    )

    assert aec_status.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert "bridge status file missing" in result["failures"]


def test_baseline_rejects_invalid_status(tmp_path: Path, monkeypatch, capsys) -> None:
    path = tmp_path / "status.json"
    path.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["aec_status.py", str(path), "--expect", "baseline"],
    )

    assert aec_status.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert any("status JSON invalid" in failure for failure in result["failures"])


def test_baseline_rejects_absent_or_mismatched_selection(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "status.json"
    status = active_status("fifine-k054", "alsa_input.usb-FIFINE")
    status["microphone"]["selected"] = None
    status["microphone"]["selection_reason"] = "all configured microphones are absent"
    # A compatibility endpoint must not revive an authoritative selected=null decision.
    status["endpoints"]["lark"] = "alsa_input.usb-STALE-LARK"
    path.write_text(json.dumps(status), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["aec_status.py", str(path), "--expect", "baseline"],
    )

    assert aec_status.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert any("all configured microphones are absent" in item for item in result["failures"])

    path.write_text(
        json.dumps(active_status("fifine-k054", "alsa_input.usb-FIFINE")),
        encoding="utf-8",
    )
    assert aec_status.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert any("expected 'lark-a1'" in item for item in result["failures"])


def test_safe_allows_no_selection_but_rejects_selected_mismatch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "status.json"
    safe = {
        "state": "SAFE",
        "generation": 4,
        "microphone": {"selected": None, "selection_reason": "identity conflict"},
        "endpoints": {"microphone": None, "lark": "alsa_input.usb-STALE-LARK"},
        "graph": {"unexpected_links": []},
    }
    path.write_text(json.dumps(safe), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["aec_status.py", str(path), "--expect", "safe"],
    )

    assert aec_status.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["microphone_id"] is None

    safe = active_status("fifine-k054", "alsa_input.usb-FIFINE")
    safe["state"] = "SAFE"
    path.write_text(json.dumps(safe), encoding="utf-8")
    assert aec_status.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert any("expected 'lark-a1'" in item for item in result["failures"])


def test_safe_rejects_malformed_microphone_status(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "state": "SAFE",
                "microphone": "malformed",
                "endpoints": {"microphone": None, "lark": None},
                "graph": {"unexpected_links": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["aec_status.py", str(path), "--expect", "safe"],
    )

    assert aec_status.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert any("status invalid" in item for item in result["failures"])
