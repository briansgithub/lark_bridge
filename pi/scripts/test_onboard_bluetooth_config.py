from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from onboard_bluetooth_config import BEGIN, ConfigError, configure, main


def test_disable_adds_one_global_managed_block_and_is_idempotent() -> None:
    original = "arm_64bit=0\n[pi3]\ndtparam=audio=on\n"

    updated = configure(original, enabled=True)

    assert updated.count(BEGIN) == 1
    assert updated.endswith(
        "[all]\ndtoverlay=disable-bt\n"
        "# END rpi-lark-bridge qualified USB-BT500 call controller\n"
    )
    assert configure(updated, enabled=True) == updated


def test_restore_removes_only_the_managed_block() -> None:
    original = "arm_64bit=0\n"
    managed = configure(original, enabled=True)

    assert configure(managed, enabled=False) == original


@pytest.mark.parametrize(
    "text",
    (
        "dtoverlay=disable-bt\n",
        "dtoverlay = disable-bt # hand managed\n",
        f"{BEGIN}\n[all]\ndtoverlay=disable-bt\n",
        f"{BEGIN}\n[all]\ndtoverlay=other\n# END rpi-lark-bridge qualified USB-BT500 call controller\n",
    ),
)
def test_ambiguous_or_edited_configuration_fails_closed(text: str) -> None:
    with pytest.raises(ConfigError):
        configure(text, enabled=True)


def test_cli_atomically_updates_and_checks_existing_file(tmp_path: Path) -> None:
    config = tmp_path / "config.txt"
    config.write_text("arm_64bit=0\n", encoding="utf-8")
    config.chmod(0o640)

    assert main(["--path", str(config), "--disable-qualified"]) == 0
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert main(["--path", str(config), "--check"]) == 0
    assert main(["--path", str(config), "--restore-managed"]) == 0
    assert config.read_text(encoding="utf-8") == "arm_64bit=0\n"
