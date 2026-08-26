# Resume here when the ESP hardware arrives

## Fixed project identity

- Worktree: `B:\Desktop\W\Hardware_write\larkbridge-esp32`
- Branch: `codex/esp32-dual-radio-port`
- Source checkpoint: run `git rev-parse HEAD`; it must match the source commit recorded in the
  retained pre-hardware manifests and bundle.
- Original Raspberry Pi checkout/rollback reference:
  `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge`
- Archived original objective: `H:\Downloads\bt600_plan.md`

The repository is build-ready, not hardware-qualified. Every H0–H17 row remains pending until its
named evidence is captured.

## First 20 minutes

```powershell
Set-Location B:\Desktop\W\Hardware_write\larkbridge-esp32
git status --short --branch
py -3 firmware/tools/build_firmware.py host
py -3 firmware/tools/arrival.py ports
py -3 firmware/tools/arrival.py new-run --gate H0 --label inventory --operator USER
```

Stop if the worktree is unexpectedly dirty, host tests fail, the board labels differ from the
ordered ESP32-S31-Function-CoreBoard-1 and ESP32-DevKitC-32UE, or the 32UE antenna path is not
complete. Photograph both sides and connector/header orientation before wiring.

Then follow, in order:

1. [`flashing.md`](flashing.md) for pinned native IDF, separate flash, boot evidence, and one-peer
   bond-reset rules.
2. [`wiring.md`](wiring.md) for the provisional six-wire inter-board harness.
3. [`arrival-runbook.md`](arrival-runbook.md) and
   [`qualification-matrix.md`](qualification-matrix.md) for H0–H17.
4. [`protocol-v1.md`](protocol-v1.md) if UART state or route authorization is unclear.
5. [`aec-qualification.md`](aec-qualification.md) for H10; never enable processed uplink before its
   explicit qualification image and evidence run.
6. [`rf-power-matrix.md`](rf-power-matrix.md) before compacting antennas or adding one-cable power.

## Hardware facts that must be filled in

Record exact board revisions, chip revisions, Bluetooth MACs, COM ports, LARK VID:PID and UAC
alternate setting, antenna and pigtail part markings, harness revision/lengths/colors, supply and
cable identities, Pixel/car identities, and final paired addresses. Header orientation is not
assumed from the online pinout; confirm delivered silkscreen/schematic and continuity first.

## Campaign rule

During iteration, run ten resumable cycles and a 5–10 minute smoke soak. That does not satisfy
endurance. The release gate is a separate 3,600-second production-topology soak after all preceding
hardware gates pass. Stop on a hard failure and retain the incomplete run rather than overwriting
it.

## Rollback

If the ESP path is blocked by S31 preview silicon/IDF, UAC compatibility, HFP HCI audio, memory, or
RF coexistence, preserve the failing E18 run and return to the untouched Pi worktree above. Do not
rewrite Pi evidence to make it look like ESP evidence, and do not erase either checkout.
