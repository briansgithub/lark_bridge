#!/usr/bin/env python3
"""Build and verify the ABI-locked LarkBridge USB-BT600 support package."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


SUPPORT_ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = SUPPORT_ROOT / "sources.json"
DEFAULT_WORK_ROOT = Path("/tmp/larkbridge-bt600-build")
KBUILD_USER = "larkbridge"
KBUILD_HOST = "reproducible"


class BuildError(RuntimeError):
    """Raised when a pinned input or output violates the package contract."""


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> str:
    print("+", " ".join(command), flush=True)
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if completed.returncode:
        detail = ""
        if capture:
            detail = f"\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        raise BuildError(f"command failed ({completed.returncode}): {' '.join(command)}{detail}")
    return completed.stdout if capture else ""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_bytes(entry: dict[str, object]) -> bytes:
    request = urllib.request.Request(
        str(entry["url"]), headers={"User-Agent": "larkbridge-bt600-support/1"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if entry.get("encoding") == "base64":
        data = base64.b64decode(data, validate=True)
    expected_size = int(entry["size"])
    if len(data) != expected_size:
        raise BuildError(
            f"size mismatch for {entry['name'] if 'name' in entry else entry['path']}: "
            f"expected {expected_size}, got {len(data)}"
        )
    actual_hash = sha256_bytes(data)
    if actual_hash != entry["sha256"]:
        raise BuildError(
            f"SHA-256 mismatch for {entry['name'] if 'name' in entry else entry['path']}: "
            f"expected {entry['sha256']}, got {actual_hash}"
        )
    return data


def download(entry: dict[str, object], destination: Path) -> Path:
    data = checked_bytes(entry)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    print(f"verified {destination.name}: {sha256_bytes(data)}", flush=True)
    return destination


def require_text(haystack: str, needle: str, label: str) -> None:
    if needle not in haystack:
        raise BuildError(f"{label} does not contain required text: {needle}")


def first_debian_changelog_stanza(changelog: str) -> str:
    next_stanza = changelog.find("\nlinux (", 1)
    return changelog if next_stanza < 0 else changelog[:next_stanza]


def parse_control_field_lines(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, value = line.partition(": ")
        if not separator or not name:
            raise BuildError(f"unexpected dpkg-deb control output: {line}")
        fields[name] = value
    return fields


def normalize_tree(root: Path, epoch: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        os.utime(path, (epoch, epoch), follow_symlinks=False)
    os.utime(root, (epoch, epoch), follow_symlinks=False)


def write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(mode)


def maintainer_script(kernel_release: str) -> str:
    return f"#!/bin/sh\nset -e\ndepmod -a {kernel_release}\n"


def depmod_config(kernel_release: str) -> str:
    return (
        "# Select the LarkBridge backports only for their exact kernel ABI.\n"
        f"override btrtl {kernel_release} updates/larkbridge\n"
        f"override btusb {kernel_release} updates/larkbridge\n"
    )


def package_control(manifest: dict[str, object]) -> str:
    package = manifest["package"]
    kernel = manifest["kernel"]
    return (
        f"Package: {package['name']}\n"
        f"Version: {package['version']}\n"
        f"Architecture: {package['architecture']}\n"
        "Maintainer: LarkBridge Project <root@localhost>\n"
        "Section: kernel\n"
        "Priority: optional\n"
        f"Depends: {package['kernel_dependency']}\n"
        f"Built-Using: linux (= {kernel['binary_package_version']})\n"
        "Description: exact RTL8761CU support for the LarkBridge USB-BT600\n"
        " ABI-locked btrtl/btusb backports and matching Realtek firmware for\n"
        " Raspberry Pi kernel 6.18.34+rpt-rpi-v8.\n"
    )


def prepare_inputs(manifest: dict[str, object], work_root: Path) -> dict[str, Path]:
    downloads = work_root / "downloads"
    source_root = work_root / "source"
    source_root.mkdir(parents=True)
    kernel = manifest["kernel"]

    metadata_paths: dict[str, Path] = {}
    for entry in kernel["source_metadata"]:
        metadata_paths[entry["name"]] = download(entry, downloads / entry["name"])

    dsc_path = metadata_paths["linux_6.18.34-1+rpt1.dsc"]
    dsc = dsc_path.read_text(encoding="utf-8")
    require_text(dsc, f"Version: {kernel['binary_package_version']}", "kernel dsc")
    orig = kernel["orig_tarball"]
    require_text(dsc, f"{orig['sha256']} {orig['size']} {orig['name']}", "kernel dsc")
    debian_entry = next(
        item
        for item in kernel["source_metadata"]
        if item["name"] == "linux_6.18.34-1+rpt1.debian.tar.xz"
    )
    require_text(
        dsc,
        f"{debian_entry['sha256']} {debian_entry['size']} {debian_entry['name']}",
        "kernel dsc",
    )

    changelog = run(
        ["tar", "-xOf", str(metadata_paths[debian_entry["name"]]), "debian/changelog"],
        capture=True,
    )
    first_stanza = first_debian_changelog_stanza(changelog)
    require_text(first_stanza, "linux (1:6.18.34-1+rpt1) trixie", "kernel changelog")
    require_text(first_stanza, f"Linux commit: {kernel['source_commit']}", "kernel changelog")

    for entry in kernel["source_files"]:
        download(entry, source_root / entry["path"])

    for backport in manifest["backports"]:
        patch_path = SUPPORT_ROOT / backport["patch"]
        if not patch_path.is_file():
            raise BuildError(f"missing backport patch: {patch_path}")
        run(
            ["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(patch_path)],
            cwd=source_root,
        )

    btrtl_source = (source_root / "drivers/bluetooth/btrtl.c").read_text(encoding="utf-8")
    btusb_source = (source_root / "drivers/bluetooth/btusb.c").read_text(encoding="utf-8")
    if btusb_source.count("USB_DEVICE(0x0b05, 0x1d70)") != 1:
        raise BuildError("patched btusb source must contain exactly one USB-BT600 entry")
    require_text(btusb_source, "BTUSB_REALTEK |", "patched btusb source")
    require_text(btusb_source, "BTUSB_WIDEBAND_SPEECH", "patched btusb source")
    for token in (
        "CHIP_ID_8761C = 51",
        "rtl_bt/rtl8761cu_fw",
        "rtl_bt/rtl8761cu_config",
        "case CHIP_ID_8761C:",
    ):
        require_text(btrtl_source, token, "patched btrtl source")

    build_packages: dict[str, Path] = {}
    for entry in kernel["build_packages"]:
        build_packages[entry["name"]] = download(entry, downloads / entry["name"])
    stock = kernel["stock_image_package"]
    stock_path = download(stock, downloads / stock["name"])

    firmware_paths: dict[str, Path] = {}
    for entry in manifest["firmware"]["files"]:
        firmware_paths[entry["name"]] = download(entry, downloads / entry["name"])
    documentation_paths: dict[str, Path] = {}
    for entry in manifest["firmware"]["documentation"]:
        documentation_paths[entry["name"]] = download(entry, downloads / entry["name"])

    return {
        "source_root": source_root,
        "stock_image": stock_path,
        "firmware_dir": downloads,
        "documentation_dir": downloads,
        **{f"build:{name}": path for name, path in build_packages.items()},
    }


def install_exact_headers(manifest: dict[str, object], inputs: dict[str, Path]) -> Path:
    if os.geteuid() != 0:
        raise BuildError("the container build must run as root to extract exact headers")
    for entry in manifest["kernel"]["build_packages"]:
        run(["dpkg-deb", "-x", str(inputs[f"build:{entry['name']}"]), "/"])

    release = manifest["kernel"]["release"]
    headers = Path(f"/usr/src/linux-headers-{release}")
    config = headers / ".config"
    symvers = headers / "Module.symvers"
    if sha256_path(config) != manifest["kernel"]["config_sha256"]:
        raise BuildError("exact deployed kernel .config hash does not match")
    if sha256_path(symvers) != manifest["kernel"]["module_symvers_sha256"]:
        raise BuildError("exact deployed kernel Module.symvers hash does not match")
    config_text = config.read_text(encoding="utf-8")
    require_text(
        config_text,
        f'CONFIG_CC_VERSION_TEXT="{manifest["build"]["gcc_version_text"]}"',
        "kernel config",
    )
    return headers


def check_toolchain(manifest: dict[str, object]) -> dict[str, str]:
    gcc_package = run(["dpkg-query", "-W", "-f=${Version}", "gcc-14"], capture=True).strip()
    binutils_package = run(
        ["dpkg-query", "-W", "-f=${Version}", "binutils"], capture=True
    ).strip()
    if gcc_package != manifest["build"]["gcc_package_version"]:
        raise BuildError(f"unexpected gcc-14 package version: {gcc_package}")
    if binutils_package != manifest["build"]["binutils_package_version"]:
        raise BuildError(f"unexpected binutils package version: {binutils_package}")
    gcc_text = run(["aarch64-linux-gnu-gcc-14", "--version"], capture=True).splitlines()[0]
    ld_text = run(["aarch64-linux-gnu-ld", "--version"], capture=True).splitlines()[0]
    require_text(gcc_text, "14.2.0", "gcc version")
    if not ld_text.endswith(manifest["build"]["binutils_version"]):
        raise BuildError(f"unexpected binutils version text: {ld_text}")
    return {
        "gcc_package": gcc_package,
        "gcc_version_text": gcc_text,
        "binutils_package": binutils_package,
        "binutils_version_text": ld_text,
    }


def build_modules(
    manifest: dict[str, object], inputs: dict[str, Path], headers: Path
) -> dict[str, Path]:
    module_root = inputs["source_root"] / "drivers/bluetooth"
    write_text(module_root / "Makefile", "obj-m += btrtl.o\nobj-m += btusb.o\n")
    epoch = int(manifest["build"]["source_date_epoch"])
    build_env = os.environ.copy()
    build_env.update(
        {
            "ARCH": "arm64",
            "KBUILD_BUILD_HOST": KBUILD_HOST,
            "KBUILD_BUILD_TIMESTAMP": f"@{epoch}",
            "KBUILD_BUILD_USER": KBUILD_USER,
            "KBUILD_BUILD_VERSION": "1",
            "KBUILD_ABS_SRCTREE": "0",
            "SOURCE_DATE_EPOCH": str(epoch),
            "KCFLAGS": (
                f"-fdebug-prefix-map={inputs['source_root']}=/usr/src/larkbridge-bt600 "
                f"-fmacro-prefix-map={inputs['source_root']}=/usr/src/larkbridge-bt600"
            ),
        }
    )
    run(
        ["make", "-j1", "-C", str(headers), f"M={module_root}", "modules"],
        env=build_env,
    )
    modules = {name: module_root / f"{name}.ko" for name in ("btrtl", "btusb")}
    for name, path in modules.items():
        if not path.is_file():
            raise BuildError(f"kernel build did not produce {name}.ko")
    return modules


def make_provenance(
    manifest: dict[str, object], modules: dict[str, Path], toolchain: dict[str, str]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "package": manifest["package"],
        "kernel": manifest["kernel"],
        "backports": [
            {
                **backport,
                "patch_sha256": sha256_path(SUPPORT_ROOT / backport["patch"]),
            }
            for backport in manifest["backports"]
        ],
        "firmware": manifest["firmware"],
        "build": {**manifest["build"], **toolchain},
        "artifacts": {
            f"{name}.ko": {"sha256": sha256_path(path), "size": path.stat().st_size}
            for name, path in sorted(modules.items())
        },
    }


def assemble_package(
    manifest: dict[str, object],
    inputs: dict[str, Path],
    modules: dict[str, Path],
    provenance: dict[str, object],
    work_root: Path,
    output_dir: Path,
) -> Path:
    package_root = work_root / "package-root"
    package_root.mkdir()
    release = manifest["kernel"]["release"]
    module_dir = package_root / f"usr/lib/modules/{release}/updates/larkbridge"
    module_dir.mkdir(parents=True)
    for name, module in modules.items():
        shutil.copyfile(module, module_dir / f"{name}.ko")

    for entry in manifest["firmware"]["files"]:
        destination = package_root / entry["install_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(inputs["firmware_dir"] / entry["name"], destination)

    doc_dir = package_root / f"usr/share/doc/{manifest['package']['name']}"
    doc_dir.mkdir(parents=True)
    for entry in manifest["firmware"]["documentation"]:
        shutil.copyfile(inputs["documentation_dir"] / entry["name"], doc_dir / entry["name"])
    write_text(
        doc_dir / "provenance.json",
        json.dumps(provenance, sort_keys=True, indent=2) + "\n",
    )
    write_text(
        doc_dir / "copyright",
        "Kernel modules: Linux Bluetooth driver sources, GPL-2.0-or-later.\n"
        "Firmware: Copyright (c) 2010 Realtek Semiconductor Corporation.\n"
        "See WHENCE and LICENCE.rtlwifi_firmware.txt in this directory.\n",
    )
    write_text(
        package_root / "usr/lib/depmod.d/larkbridge-bt600-support.conf",
        depmod_config(release),
    )

    control_dir = package_root / "DEBIAN"
    write_text(control_dir / "control", package_control(manifest))
    script = maintainer_script(release)
    write_text(control_dir / "postinst", script, 0o755)
    write_text(control_dir / "postrm", script, 0o755)

    normalize_tree(package_root, int(manifest["build"]["source_date_epoch"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{manifest['package']['name']}_{manifest['package']['version']}_"
        f"{manifest['package']['architecture']}.deb"
    )
    package_path = output_dir / filename
    build_env = os.environ.copy()
    build_env["SOURCE_DATE_EPOCH"] = str(manifest["build"]["source_date_epoch"])
    run(
        [
            "dpkg-deb",
            "--root-owner-group",
            "--uniform-compression",
            "-Zxz",
            "-z9",
            "--build",
            str(package_root),
            str(package_path),
        ],
        env=build_env,
    )
    return package_path


def modinfo(field: str, module: Path) -> list[str]:
    text = run(["modinfo", "-F", field, str(module)], capture=True)
    return [line for line in text.splitlines() if line]


def verify_package(
    manifest: dict[str, object], package_path: Path, stock_image: Path, verify_root: Path
) -> None:
    package = manifest["package"]
    release = manifest["kernel"]["release"]
    fields = parse_control_field_lines(
        run(
        ["dpkg-deb", "-f", str(package_path), "Package", "Version", "Architecture", "Depends"],
        capture=True,
        )
    )
    expected_fields = {
        "Package": package["name"],
        "Version": package["version"],
        "Architecture": package["architecture"],
        "Depends": package["kernel_dependency"],
    }
    if fields != expected_fields:
        raise BuildError(f"package control mismatch: expected {expected_fields}, got {fields}")

    extracted = verify_root / "package"
    control = verify_root / "control"
    extracted.mkdir(parents=True)
    control.mkdir(parents=True)
    run(["dpkg-deb", "-x", str(package_path), str(extracted)])
    run(["dpkg-deb", "-e", str(package_path), str(control)])

    expected_script = maintainer_script(release)
    for name in ("postinst", "postrm"):
        if (control / name).read_text(encoding="utf-8") != expected_script:
            raise BuildError(f"{name} is not depmod-only")
    for forbidden in ("preinst", "prerm", "triggers"):
        if (control / forbidden).exists():
            raise BuildError(f"unexpected package lifecycle file: {forbidden}")

    expected_files = {
        f"usr/lib/modules/{release}/updates/larkbridge/btrtl.ko",
        f"usr/lib/modules/{release}/updates/larkbridge/btusb.ko",
        "usr/lib/firmware/rtl_bt/rtl8761cu_fw.bin",
        "usr/lib/firmware/rtl_bt/rtl8761cu_config.bin",
        "usr/lib/depmod.d/larkbridge-bt600-support.conf",
        f"usr/share/doc/{package['name']}/WHENCE",
        f"usr/share/doc/{package['name']}/LICENCE.rtlwifi_firmware.txt",
        f"usr/share/doc/{package['name']}/provenance.json",
        f"usr/share/doc/{package['name']}/copyright",
    }
    actual_files = {
        str(path.relative_to(extracted)).replace(os.sep, "/")
        for path in extracted.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise BuildError(
            f"package file set mismatch; missing={sorted(expected_files - actual_files)}, "
            f"extra={sorted(actual_files - expected_files)}"
        )

    firmware_entries = {entry["name"]: entry for entry in manifest["firmware"]["files"]}
    for name, entry in firmware_entries.items():
        installed = extracted / entry["install_path"]
        if sha256_path(installed) != entry["sha256"]:
            raise BuildError(f"installed firmware hash mismatch: {name}")

    module_dir = extracted / f"usr/lib/modules/{release}/updates/larkbridge"
    btrtl = module_dir / "btrtl.ko"
    btusb = module_dir / "btusb.ko"
    for module in (btrtl, btusb):
        vermagic = modinfo("vermagic", module)
        if len(vermagic) != 1 or not vermagic[0].startswith(f"{release} "):
            raise BuildError(f"wrong vermagic for {module.name}: {vermagic}")
        machine = run(["readelf", "-h", str(module)], capture=True)
        require_text(machine, "Machine:                           AArch64", module.name)

    aliases = modinfo("alias", btusb)
    if not any("dcE0dsc01dp01" in alias for alias in aliases):
        raise BuildError("btusb module lacks its generic Bluetooth USB class alias")
    firmware_names = set(modinfo("firmware", btrtl))
    for name in ("rtl_bt/rtl8761cu_fw.bin", "rtl_bt/rtl8761cu_config.bin"):
        if name not in firmware_names:
            raise BuildError(f"btrtl module lacks firmware declaration: {name}")

    depmod_text = (extracted / "usr/lib/depmod.d/larkbridge-bt600-support.conf").read_text(
        encoding="utf-8"
    )
    if depmod_text != depmod_config(release):
        raise BuildError("depmod override is not exact-release scoped")

    fake_root = verify_root / "module-root"
    fake_root.mkdir()
    (fake_root / "usr").mkdir()
    (fake_root / "lib").symlink_to("usr/lib", target_is_directory=True)
    run(["dpkg-deb", "-x", str(stock_image), str(fake_root)])
    stock_btrtl = fake_root / f"usr/lib/modules/{release}/kernel/drivers/bluetooth/btrtl.ko.xz"
    stock_btusb = fake_root / f"usr/lib/modules/{release}/kernel/drivers/bluetooth/btusb.ko.xz"
    if not stock_btrtl.is_file() or not stock_btusb.is_file():
        raise BuildError("pinned stock image does not retain both rollback modules")
    stock_hashes = {path.name: sha256_path(path) for path in (stock_btrtl, stock_btusb)}

    run(["dpkg-deb", "-x", str(package_path), str(fake_root)])
    run(["depmod", "-b", str(fake_root), release])
    resolved_alias = run(
        [
            "modprobe",
            "-d",
            str(fake_root),
            "-S",
            release,
            "-R",
            package["bt600_modalias_probe"],
        ],
        capture=True,
    ).splitlines()
    if "btusb" not in resolved_alias:
        raise BuildError(
            f"exact {package['bt600_usb_id']} modalias did not resolve to btusb: {resolved_alias}"
        )
    selected = run(
        ["modinfo", "-b", str(fake_root), "-k", release, "-n", "btusb"], capture=True
    ).strip()
    if "/updates/larkbridge/btusb.ko" not in selected:
        raise BuildError(f"depmod did not select the BT600 btusb override: {selected}")
    modules_dep = (fake_root / f"usr/lib/modules/{release}/modules.dep").read_text(
        encoding="utf-8"
    )
    btusb_line = next(
        (
            line
            for line in modules_dep.splitlines()
            if line.startswith("updates/larkbridge/btusb.ko:")
        ),
        "",
    )
    require_text(btusb_line, "updates/larkbridge/btrtl.ko", "custom btusb dependency")

    for name in ("btrtl.ko", "btusb.ko"):
        (fake_root / f"usr/lib/modules/{release}/updates/larkbridge/{name}").unlink()
    (fake_root / "usr/lib/depmod.d/larkbridge-bt600-support.conf").unlink()
    run(["depmod", "-b", str(fake_root), release])
    selected_after_remove = run(
        ["modinfo", "-b", str(fake_root), "-k", release, "-n", "btusb"], capture=True
    ).strip()
    if "/kernel/drivers/bluetooth/btusb.ko.xz" not in selected_after_remove:
        raise BuildError(f"stock btusb was not restored after removal: {selected_after_remove}")
    for path in (stock_btrtl, stock_btusb):
        if sha256_path(path) != stock_hashes[path.name]:
            raise BuildError(f"stock rollback module changed during package verification: {path.name}")

    print("package verification: PASS", flush=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    work_root = args.work_root.resolve()
    if work_root in (Path("/"), Path("/tmp")):
        raise BuildError(f"unsafe work root: {work_root}")
    if work_root.exists():
        raise BuildError(f"work root already exists; use a fresh container or path: {work_root}")
    work_root.mkdir(parents=True)

    inputs = prepare_inputs(manifest, work_root)
    headers = install_exact_headers(manifest, inputs)
    toolchain = check_toolchain(manifest)
    modules = build_modules(manifest, inputs, headers)
    provenance = make_provenance(manifest, modules, toolchain)
    package_path = assemble_package(
        manifest, inputs, modules, provenance, work_root, args.output.resolve()
    )
    verify_root = work_root / "verify"
    verify_root.mkdir()
    verify_package(manifest, package_path, inputs["stock_image"], verify_root)
    print(f"artifact sha256: {sha256_path(package_path)}  {package_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
