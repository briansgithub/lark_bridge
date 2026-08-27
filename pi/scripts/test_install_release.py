from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any

import install_release as installer
import pytest
from install_release import InstallError, read_release

A2DP_TARGET_FRAGMENT = "66-bridge-a2dp-source-target.conf"
AUX_HEADROOM_FRAGMENT = "67-bridge-aux-headroom.conf"


def make_archive(path: Path, *, corrupt: bool = False, unsafe: bool = False) -> str:
    payload = b"runtime\n"
    relative = "../escape" if unsafe else "runtime.py"
    manifest_hash = hashlib.sha256(payload).hexdigest()
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(f"rpi-lark-bridge/{relative}", payload)
        bundle.writestr(
            "rpi-lark-bridge/MANIFEST.sha256",
            f"{manifest_hash if not corrupt else '0' * 64}  {relative}\n",
        )
        bundle.writestr(
            "rpi-lark-bridge/RELEASE.json",
            json.dumps(
                {
                    "archive_schema": 1,
                    "commit": "a" * 40,
                    "profile": "pi3-usb-bt500-aux",
                }
            ),
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_install_archive(path: Path) -> str:
    payloads = {
        **{
            f"pi/systemd/system/{unit}": f"new {unit}\n".encode()
            for unit in installer.SYSTEM_UNITS
        },
        **{
            f"pi/systemd/user/{unit}": f"new {unit}\n".encode()
            for unit in installer.USER_UNITS
        },
        **{
            f"pi/scripts/{script}": f"new {script}\n".encode()
            for script in installer.LIB_SCRIPTS
        },
        **{
            f"pi/powerloss/{script}": f"new {script}\n".encode()
            for script in installer.POWERLOSS_SCRIPTS
        },
        "pi/pipewire/pipewire.conf.d/10-test.conf": b"new pipewire\n",
        "pi/wireplumber/wireplumber.conf.d/10-test.conf": b"new wireplumber\n",
        f"pi/wireplumber/wireplumber.conf.d/{A2DP_TARGET_FRAGMENT}": (b"new phone media target\n"),
        f"pi/wireplumber/wireplumber.conf.d/{AUX_HEADROOM_FRAGMENT}": (b"new AUX headroom\n"),
        "pi/bluez/main.conf.d/10-bridge.conf": b"new bluez\n",
        "pi/scripts/netplan-startup-fastpath": b"new netplan script\n",
        "pi/systemd/system/NetworkManager-10-larkbridge-netplan-startup.conf": (
            b"new netplan unit\n"
        ),
        "config/bridge.toml": b"new default config\n",
        "runtime.py": b"new runtime\n",
    }
    manifest = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {relative}\n"
        for relative, payload in sorted(payloads.items())
    )
    with zipfile.ZipFile(path, "w") as bundle:
        for relative, payload in payloads.items():
            bundle.writestr(f"rpi-lark-bridge/{relative}", payload)
        bundle.writestr("rpi-lark-bridge/MANIFEST.sha256", manifest)
        bundle.writestr(
            "rpi-lark-bridge/RELEASE.json",
            json.dumps(
                {
                    "archive_schema": 1,
                    "commit": "a" * 40,
                    "profile": "pi3-usb-bt500-aux",
                }
            ),
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def tree_state(root: Path) -> dict[str, tuple[str, Any]]:
    state: dict[str, tuple[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: os.fspath(item)):
        relative = path.relative_to(root).as_posix()
        if relative == "var" or relative.startswith("var/"):
            continue
        if path.is_symlink():
            state[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            state[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            state[relative] = ("directory", None)
    return state


def test_valid_release_is_verified(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_archive(archive)

    release, manifest = read_release(archive, checksum)

    assert release["commit"] == "a" * 40
    assert manifest == {"runtime.py": hashlib.sha256(b"runtime\n").hexdigest()}


def test_manifest_corruption_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_archive(archive, corrupt=True)

    with pytest.raises(InstallError, match="manifest checksum"):
        read_release(archive, checksum)


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_archive(archive, unsafe=True)

    with pytest.raises(InstallError, match="unsafe archive path"):
        read_release(archive, checksum)


def test_preimage_payload_is_verified_before_restore(tmp_path: Path) -> None:
    system_root = tmp_path / "root"
    managed = system_root / "etc/example.conf"
    write_file(managed, b"old\n")
    preimage_root = tmp_path / "preimage"
    installer.snapshot_preimages([managed], system_root, preimage_root)
    managed.write_bytes(b"new\n")
    payload = next((preimage_root / "files").iterdir())
    payload.write_bytes(b"corrupt\n")

    with pytest.raises(InstallError, match="preimage checksum mismatch"):
        installer.restore_preimages(system_root, preimage_root)

    assert managed.read_bytes() == b"new\n"


def test_symlink_preimage_is_restored(tmp_path: Path) -> None:
    system_root = tmp_path / "root"
    link = system_root / "etc/systemd/system/example.target.wants/example.service"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to("../original.service")
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    preimage_root = tmp_path / "preimage"
    installer.snapshot_preimages([link], system_root, preimage_root)
    link.unlink()
    link.write_bytes(b"replacement\n")

    installer.restore_preimages(system_root, preimage_root)

    assert link.is_symlink()
    assert os.readlink(link) == "../original.service"


def test_install_deploys_powerloss_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_install_archive(archive)
    system_root = tmp_path / "root"
    target = system_root / "home/admin/rpi-lark-bridge"
    fragment = (
        system_root / "home/admin/.config/wireplumber/wireplumber.conf.d" / A2DP_TARGET_FRAGMENT
    )
    aux_fragment = (
        system_root
        / "home/admin/.config/wireplumber/wireplumber.conf.d"
        / AUX_HEADROOM_FRAGMENT
    )
    write_file(fragment, b"old phone media target\n")
    write_file(aux_fragment, b"old AUX headroom\n")

    def portable_symlink(target_name: str, link: Path) -> None:
        write_file(link, f"symlink:{target_name}\n".encode())

    monkeypatch.setattr(installer, "symlink", portable_symlink)
    installer.install(
        archive,
        checksum,
        target=target,
        system_root=system_root,
        netplan_fastpath=False,
    )

    verifier = system_root / "usr/local/lib/rpi-lark-bridge/powerloss/powerloss_verify.py"
    assert verifier.read_bytes() == b"new powerloss_verify.py\n"
    assert fragment.read_bytes() == b"new phone media target\n"
    assert aux_fragment.read_bytes() == b"new AUX headroom\n"
    transactions = list(
        (system_root / "var/lib/rpi-lark-bridge/releases" / ("a" * 12)).glob("install-*")
    )
    manifest = json.loads((transactions[0] / "system-preimage/MANIFEST.json").read_text("utf-8"))
    paths = {entry["path"] for entry in manifest["entries"]}
    assert f"home/admin/.config/wireplumber/wireplumber.conf.d/{A2DP_TARGET_FRAGMENT}" in paths
    assert f"home/admin/.config/wireplumber/wireplumber.conf.d/{AUX_HEADROOM_FRAGMENT}" in paths


def test_failed_install_restores_all_system_and_user_preimages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "release.zip"
    checksum = make_install_archive(archive)
    system_root = tmp_path / "root"
    target = system_root / "home/admin/rpi-lark-bridge"
    write_file(target / "old-runtime.py", b"old runtime\n")
    write_file(target / "config/bridge.toml", b"operator config\n")

    existing_files = {
        system_root / "etc/systemd/system/bridge-tuning.service": b"old tuning\n",
        system_root
        / "home/admin/.config/systemd/user/bridge-output-remote.service": (
            b"old user unit\n"
        ),
        system_root
        / "home/admin/.config/pipewire/pipewire.conf.d/10-test.conf": (
            b"old pipewire\n"
        ),
        system_root
        / "home/admin/.config/wireplumber/wireplumber.conf.d"
        / A2DP_TARGET_FRAGMENT: b"old phone media target\n",
        system_root / "etc/bluetooth/main.conf.d/10-bridge.conf": b"old bluez\n",
        system_root
        / "etc/systemd/system/NetworkManager.service.d/10-larkbridge-netplan-startup.conf": (
            b"old netplan config\n"
        ),
        system_root
        / "usr/local/lib/rpi-lark-bridge/boot-path/netplan": (b"old netplan script\n"),
        system_root
        / "usr/local/lib/rpi-lark-bridge/powerloss/powerloss_verify.py": (
            b"old powerloss verifier\n"
        ),
        system_root / "etc/larkbridge/DEPLOYED.json": b'{"old": true}\n',
    }
    for path, payload in existing_files.items():
        write_file(path, payload)

    existing_link_paths = {
        system_root
        / "etc/systemd/system/graphical.target.wants/bridge-btfw.service": (
            b"legacy btfw enablement\n"
        ),
        system_root
        / "etc/systemd/system/multi-user.target.wants/bridge-btwatchdog@call.service": (
            b"legacy watchdog enablement\n"
        ),
        system_root
        / "home/admin/.config/systemd/user/default.target.wants/bridge-output-remote.service": (
            b"legacy output enablement\n"
        ),
    }
    for path, payload in existing_link_paths.items():
        write_file(path, payload)

    before = tree_state(system_root)

    def fail_deployed(_path: Path, _document: dict[str, Any]) -> None:
        raise OSError("injected deployment metadata failure")

    def portable_symlink(target_name: str, link: Path) -> None:
        write_file(link, f"symlink:{target_name}\n".encode())

    monkeypatch.setattr(installer, "symlink", portable_symlink)
    monkeypatch.setattr(installer, "write_deployed", fail_deployed)
    with pytest.raises(OSError, match="injected deployment metadata failure"):
        installer.install(
            archive,
            checksum,
            target=target,
            system_root=system_root,
            netplan_fastpath=False,
        )

    assert tree_state(system_root) == before
    transactions = list(
        (system_root / "var/lib/rpi-lark-bridge/releases" / ("a" * 12)).glob(
            "install-*"
        )
    )
    assert len(transactions) == 1
    manifest_path = transactions[0] / "system-preimage/MANIFEST.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    paths = {entry["path"] for entry in manifest["entries"]}
    assert "etc/larkbridge/DEPLOYED.json" in paths
    assert "etc/systemd/system/graphical.target.wants/bridge-btfw.service" in paths
    assert "home/admin/.config/wireplumber/wireplumber.conf.d/10-test.conf" in paths
    assert f"home/admin/.config/wireplumber/wireplumber.conf.d/{A2DP_TARGET_FRAGMENT}" in paths
    assert f"home/admin/.config/wireplumber/wireplumber.conf.d/{AUX_HEADROOM_FRAGMENT}" in paths
    assert "usr/local/lib/rpi-lark-bridge/powerloss/powerloss_verify.py" in paths
