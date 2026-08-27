from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRAGMENTS = (
    "50-bridge-bluez.conf",
    "66-bridge-a2dp-source-target.conf",
)


def test_boot_installer_validates_installs_and_tracks_phone_media_policy() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert (
        'wireplumber_dir="/home/$BRIDGE_USER/.config/wireplumber/wireplumber.conf.d"' in installer
    )
    for fragment in FRAGMENTS:
        source = f"pi/wireplumber/wireplumber.conf.d/{fragment}"
        assert source in installer
        assert f'"$wireplumber_dir/{fragment}"' in installer
        assert f'install_managed "{source}"' in installer


def test_boot_transaction_allows_phone_media_policy_rollback() -> None:
    rollback = (ROOT / "pi" / "scripts" / "boot-transaction.sh").read_text(encoding="utf-8")

    for fragment in FRAGMENTS:
        target = f"/home/admin/.config/wireplumber/wireplumber.conf.d/{fragment}"
        assert target in rollback


def test_boot_installer_rejects_a_user_outside_the_fixed_rollback_allowlist() -> None:
    installer = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert '[ "$BRIDGE_USER" = admin ]' in installer
    assert "rollback paths are intentionally fixed" in installer
