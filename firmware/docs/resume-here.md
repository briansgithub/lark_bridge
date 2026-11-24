# ESP32 development handoff

**2026-11-22 · Build-ready · Hardware gates H0–H17 pending**

Clone the repository and check out the ESP32 branch:

```text
git clone --branch codex/esp32-dual-radio-port https://github.com/briansgithub/lark_bridge.git
cd lark_bridge
git status --short --branch
git rev-parse HEAD
```

The [ESP32 port plan](../../ESP32_PORT_PLAN.md) and [ADR-0010](../../docs/architecture/decisions/ADR-0010-esp32-dual-radio-bridge.md)
define the design. The S31 in [`firmware/s31`](../s31/) owns LARK USB capture, Pixel 7a Bluetooth,
and AEC. The 32UE in [`firmware/output`](../output/) owns car-facing A2DP and AVRCP. They share
48 kHz stereo PCM over I2S and control over UART. The Raspberry Pi 3 implementation in
[`pi/`](../../pi/) is the reference for expected behavior and the fallback while qualifying the port.

## Build and artifact provenance

[E21](../../docs/experiments/E21-esp32-prehardware-port.md) records the original native tests,
pinned ESP-IDF builds, and flash bundle checksums. The retained local ZIPs identify historical
commit `b608dc3a3cec883501c4ab55bdee87586e751128`, which is outside the current branch ancestry.
The firmware sources, target configs and locks, host tests, and build tools from that checkpoint
match the tracked inputs at branch baseline `e7f8180`. A fresh clone should build from its clean checked-out
commit and record the source commit and hashes from the resulting manifests. If reusing the
original ZIPs on the bench host, verify the [recorded hashes](../../docs/experiments/E21-esp32-prehardware-port.md#retained-build-identities)
and retain their original source identity in the evidence.

From the repository root on Windows:

```powershell
py -3 firmware/tools/build_firmware.py host
py -3 -m unittest discover -s firmware/tools/tests -p 'test_*.py'
py -3 firmware/tools/build_firmware.py output
py -3 firmware/tools/build_firmware.py s31
py -3 firmware/tools/arrival.py bundle --target output
py -3 firmware/tools/arrival.py bundle --target s31
```

Use `python3` in place of `py -3` on Linux or macOS. The builds use pinned ESP-IDF containers
(32UE v6.0.2 and S31 v6.1-rc1). Generated binaries, ZIPs, and manifests are local outputs. Use
[`flashing.md`](flashing.md) for the native Windows flashing environment and monitor.

## First bench session

1. **H0** — Photograph both sides of each board. Verify the delivered revisions, connectors,
   programming cables, and header orientation. Confirm that the output module is a 32UE and that
   its U.FL pigtail connects to one external antenna lead before enabling its radio. Record the
   physical inventory. Use `arrival.py new-run --gate H0 --label inventory --operator OPERATOR
   --output-root PATH`, replacing the placeholders with the operator name and a durable local
   evidence directory outside the checkout.
2. **H1** — Keep the harness and LARK receiver disconnected. Connect the output board alone via
   Micro-B, identify it with `arrival.py ports`, flash the matching verified image, and save its
   boot log. Repeat with the S31 using its USB Serial/JTAG Type-C port. Record board and build
   identities, reset reason, Bluetooth address, and heap. With no peers, expect silence or
   `NOT_READY`. Expect `erase_bonds=0` on both boards and `aec_publish=0` on S31.

The [arrival runbook](arrival-runbook.md) gives the bench sequence. Use `arrival.py` to attach
logs and record a verdict for each gate. Preserve failed or interrupted attempts. Record the
firmware manifests used on each board alongside the run's checkout identity.

## Continue through functional qualification

- **H2–H5** — Confirm the six-wire [harness](wiring.md) against the delivered boards and check it
  unpowered. Use separate programming USB supplies, common ground, and disconnected 5 V headers.
  Prove UART and independent reset recovery before synthetic I2S audio and drift tests.
- **H6–H10** — Prove LARK USB capture, then qualify the output radio with a bench speaker, Pixel
  media and call downlink, and the final car pairing. Follow the runbook's one-peer bond reset
  procedure before switching the output from speaker to car. Keep the normal microphone uplink
  locked to digital silence until [H10 AEC qualification](aec-qualification.md) passes.
- **H11–H15** — Exercise call/media transitions, steering controls, RF coexistence and fault
  recovery, then complete ten end-to-end cycles on the final candidate. H0–H15 define functional
  completion. H16 is a separate 3,600-second active-call production-topology soak. H17 follows
  selection of an automotive-rated supply for the protected one-cable installation.

Use the [qualification matrix](qualification-matrix.md) for exact thresholds. The agent can own
builds, flashing, serial logs, diagnosis, fixes, and evidence. Coordinate physical assembly,
meter readings, pairing, and acoustic and car setup with the owner. The previously discussed
end-to-end HIL runner and qualification-only serial test interface are still open work.
`arrival.py` already supports individual gate records and serial capture, so H0 and H1 can proceed
with the existing tooling. Prioritize the first failed gate and repeat its affected checks after
a fix.

Use the [wire protocol](protocol-v1.md) for link and route debugging and the
[RF and power matrix](rf-power-matrix.md) before consolidating power or changing antenna placement.
Watch the S31 preview Bluetooth and USB behavior and record minimum runtime heap on both targets.
