# E01 — Do SCO audio packets cross the HCI transport on the Pi 3B's BCM43438?

- **Status:** Not started — **run this first, before any other work on hardware**
- **Resolves risk:** R2 (probability 3, impact 5, score 15)
- **Gates milestone:** M4, and therefore the entire Bluetooth track
- **Script:** `tests/stage-b-hfp/s1-sco-over-hci.sh`

## Question

During a live call with the Pi connected to the Pixel as an HFP device, do SCO data packets
appear in **both directions** on the HCI transport — and does the Broadcom vendor command
`Write_SCO_PCM_Int_Param` (routing = Transport) change the answer?

## Why it cannot be answered by reading

The documentation establishes the *mechanism* but not the *outcome*:

- The BCM43438's PCM/I2S pins are not routed anywhere on the Pi 3B PCB, so SCO has exactly
  one possible path: multiplexed over the HCI UART transport.
- `Documentation/devicetree/bindings/net/broadcom-bluetooth.yaml` documents
  `brcm,bt-pcm-int-params = <sco-routing rate frame-type sync-mode clock-mode>` with
  `sco-routing: 1 = Transport`.
- `bcm2837-rpi-3-b.dts` sets only `shutdown-gpios` on the `bt` node — it does **not** set
  that property.
- raspberrypi/linux#2229 ("A2DP works, HSP/HFP silent on Pi 3") has been open since 2017.
- A btstack-dev thread concludes this is **not a hardware limitation** but a vendor
  documentation gap, with no confirmed working configuration published.

Nobody has published whether the vendor command actually works on this specific part and
firmware revision. The only way to know is to send it and watch the transport.

## Method

Run the script twice and compare. The **difference between the two runs is the finding** —
a single run in isolation proves much less.

```bash
sudo ./tests/stage-b-hfp/s1-sco-over-hci.sh                     # run 1: baseline
sudo ./tests/stage-b-hfp/s1-sco-over-hci.sh --apply-vendor-cmd  # run 2: SCO routed to HCI
```

Prerequisite: the Pixel must already be paired and showing "Phone calls" enabled. If the
phone will not connect at all, that is spike **S2**'s problem, not this one — run E02 first.

Held constant: same room, same phone, same distance (~1 m), same call type, continuous
speech in both directions for the full capture.

Varied: only the vendor command.

The script also records **which attach mechanism is in play** (`attach-mechanism.txt`).
This determines whether the device-tree overlay route is even viable: the DT property is
only read when the kernel drives the chip via serdev. If `hciuart.service` is attaching the
chip from userspace with `hciattach`, the overlay is a dead end and only the runtime vendor
command can work. **Record this before choosing a fix.**

## Partial result — attach mechanism resolved 2026-08-16 (no BT hardware needed)

The branching question ("is the device-tree route even viable, or is the chip attached from
userspace?") is **answered, favourably**, from the running system:

```
$ cat /sys/class/bluetooth/hci0/device/uevent
DRIVER=hci_uart_bcm
OF_NAME=bluetooth
OF_FULLNAME=/soc/serial@7e201000/bluetooth
OF_COMPATIBLE_0=brcm,bcm43438-bt

$ systemctl is-enabled hciuart.service
not-found
```

- **The kernel binds the controller via serdev** (`hci_uart_bcm` on `serial0-0`).
  `hciuart.service` does not exist on this image at all — the userspace `hciattach` path is
  gone. **Therefore `brcm,bt-pcm-int-params` in a device-tree overlay WILL be read**, and
  `pi/boot/overlays/bridge-bt-sco-overlay.dts` is the correct production fix rather than a
  possible dead end.
- **`brcm,bt-pcm-int-params` is absent from the live device tree.** The node carries only
  `compatible`, `fallback-bd-address`, `local-bd-address`, `max-speed`, `name`, `phandle`,
  `shutdown-gpios`, `status`. So SCO routing sits at the **firmware default**, which on a Pi 3B
  means the PCM pins — and those go nowhere on this PCB. This is exactly the failure mode
  `PLAN.md` §2.1 predicted, now confirmed as the starting condition rather than inferred.

Measured baseline: Debian 13 trixie, kernel **6.18.34+rpt-rpi-v8** (newer than the 6.12 the plan
assumed), aarch64, Raspberry Pi 3 Model B **Rev 1.2**, bluez **5.82**, pipewire **1.4.2**,
wireplumber **0.5.8**.

**What this does not answer:** whether setting the property actually makes SCO data flow. The
btstack-dev thread reports the vendor command being accepted but ineffective on some BCM43438
firmware. That still requires the runs below.

## Runs

| # | Date | Variant | Attach mechanism | SCO TX | SCO RX | Air mode | Verdict | Artifacts |
|---|---|---|---|---|---|---|---|---|
| 0 | 2026-08-16 | pre-flight, no BT peer | **serdev / `hci_uart_bcm`**; `bt-pcm-int-params` **absent** | — | — | — | DT route viable | `artifacts/U0*` |
| 1 | | baseline | | | | | | |
| 2 | | `--apply-vendor-cmd` | | | | | | |
| 3 | | DT overlay `bridge-bt-sco` | | | | | | |

Expected order of magnitude if it works: mSBC uses 7.5 ms frames, so roughly **133
packets/second per direction**. A 60 s capture during a live call should show *thousands*
of SCO packets, not tens. The script's threshold is deliberately loose (20/s) so that a
marginal result is reported as half-duplex rather than as a pass.

## Raw data

Copy the artifact directories into `docs/experiments/results/E01/`. **Commit the
`.btsnoop` files** — they are the evidence, and if the verdict is negative they are what
justifies the limitation report the brief asks for rather than a design workaround.

## Result

_(fill in)_

## Verdict

_(CONFIRMED / REFUTED / INCONCLUSIVE)_

## Consequences for the plan

| Verdict | What happens |
|---|---|
| `SCO_OK` on baseline | Risk R2 closes at probability 0. No overlay or vendor service needed. Delete the `bridge-btfw` SCO step from the installer. |
| `SCO_OK` only with the vendor command | R2 closes. `bridge-btfw.service` becomes mandatory and ships. Try the DT overlay as the cleaner equivalent; keep the runtime command as fallback. |
| `SCO_LINK_NO_DATA` after both fixes | **Mode 1 and Mode 1W are both dead on the onboard radio.** Escalate to `PLAN.md` §15 Q1 before writing any more Bluetooth code. Mode 2 (Pico) becomes the only path, and the plan's shape changes. |
| `NO_SCO_LINK` | Not a routing problem. Run E02 first; this experiment is invalid until an SCO link is being attempted at all. |

## Follow-up questions this raised

_(fill in — e.g. does the routing survive a controller reset? a reboot? a firmware reload?)_
