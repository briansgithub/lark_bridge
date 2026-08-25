# LarkBridge USB-BT600 support package

This directory builds the narrowly scoped `larkbridge-bt600-support` Debian package required by
ADR-0009. The package is tied to Raspberry Pi kernel release
`6.18.34+rpt-rpi-v8` and Debian kernel package version `1:6.18.34-1+rpt1`; it is not a DKMS package
and must not be used with another kernel ABI.

The build downloads only commit-addressed or version-addressed primary inputs, verifies every
download before use, applies the two patches in `patches/`, and builds `btrtl.ko` and `btusb.ko`
against the exact Raspberry Pi v8 `.config` and `Module.symvers`. The resulting package contains:

- the two modules under `/usr/lib/modules/6.18.34+rpt-rpi-v8/updates/larkbridge/` (the merged-usr
  target of `/lib/modules/6.18.34+rpt-rpi-v8/updates/larkbridge/`);
- only `rtl8761cu_fw.bin` and `rtl8761cu_config.bin` under `/usr/lib/firmware/rtl_bt/`;
- an exact-release depmod override, upstream `WHENCE`, the matching Realtek licence, and generated
  provenance JSON.

The package's `postinst` and `postrm` run only `depmod -a 6.18.34+rpt-rpi-v8`. They do not unload
modules, restart services, update an initramfs, pair devices, or reboot. Installing the package is
therefore a separate, later deployment action and does not activate the new modules in a running
kernel.

Upstream keeps per-device flags in `quirks_table`, while only the generic `btusb_table` is emitted
through `MODULE_DEVICE_TABLE`. Consequently `modinfo` correctly shows the generic Bluetooth USB
class alias rather than a literal BT600 VID:PID alias. The package gate proves both halves: the
compiled source contains exactly one `0b05:1d70` Realtek/wideband quirk, and an exact BT600 USB
modalias resolves to the packaged `btusb` through depmod/modprobe.

Run the complete reproducibility gate from a host with Docker:

```text
python support/bt600/build_reproducible.py --output artifacts/bt600
```

The orchestrator builds the pinned arm64 container image, runs two clean package builds, runs the
package/rollback verifier in each container, and publishes the artifact only if the two `.deb`
files are byte-identical. Build output is intentionally ignored by Git.

Run the source/provenance unit checks without Docker:

```text
python -m unittest discover -s support/bt600/tests -p "test_*.py"
```

No command in this directory installs onto, connects to, or changes the live Raspberry Pi.
