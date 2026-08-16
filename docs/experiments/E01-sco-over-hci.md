# E01 — Do SCO audio packets cross the HCI transport on the Pi 3B's BCM43438?

- **Status:** **CONCLUDED 2026-08-16 — RESOLVED, risk R2 closed**
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

### Controller capability (measured, `hciconfig hci0 features`)

`BD B8:27:EB:43:8D:51`, features page 0 = `bf fe cf fe db ff 7b 87`. Decoded by the stack, not
by hand:

| Capability | Present | Consequence |
|---|---|---|
| `<SCO link>`, `<HV2>`, `<HV3>` | yes | basic SCO available |
| `<CVSD>` | yes | 8 kHz narrowband is the guaranteed floor |
| **`<transparent SCO>`** | **yes** | **mSBC wideband (16 kHz) is possible** — the air mode it requires exists |
| `<extended SCO>`, `<EV4>`, `<EV5>` | yes | full eSCO with retransmission |
| `<EDR eSCO 2 Mbps>`, `<EDR eSCO 3 Mbps>`, `<3-slot EDR eSCO>` | yes | headroom for eSCO scheduling |
| `<err. data report>` | yes | erroneous-data reporting, needed for mSBC packet-loss concealment |

**So the controller is capable on paper for everything the design needs.** That narrows this
experiment to a single question: does SCO routing actually deliver those packets over the HCI
transport, or does the firmware default send them to the unconnected PCM pins?

**Hypothesis for Stage E / risk R1, recorded now so it can be tested rather than guessed:**
`SCO MTU: 64:1` — a 64-byte SCO buffer, and only **one** of it. mSBC frames are 60 bytes so
they fit, but a single-deep queue means the host must service SCO on time, every time. Once
A2DP's ACL traffic is competing for the same controller, that shallow queue is a plausible
mechanism for the dropouts R1 predicts. Watch for SCO buffer overruns in `btmon` during E1–E6.

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

## Result — measured 2026-08-16, during a live Discord call on the Pixel 7a

The controller powers up with **`sco_routing = 0x00` (PCM)**, read directly:

```
$ sudo hcitool -i hci0 cmd 0x3f 0x1d          # Read_SCO_PCM_Int_Param
  01 1D FC 00  00 02 00 01 01
               ^^ sco_routing = 0x00 = PCM
```

The BCM43438's PCM pins are not connected to anything on the Pi 3B PCB, so received SCO audio
goes to a dead interface. **The resulting failure is asymmetric**, which is what makes it so
easy to misdiagnose:

| `sco_routing` | SCO Data **TX** | SCO Data **RX** | `hciconfig` sco counters |
|---|---|---|---|
| `0x00` PCM (default) | 4866 packets | **0 packets** | `rx: 0` / `tx: 5060` |
| `0x01` Transport (fixed) | 7328 packets | **14656 packets** | `rx: 15831` / `tx: 19122` |

Host→controller SCO still transmits over the air, so **the microphone direction appears to work
while call audio from the phone silently never arrives.** SCO is a synchronous circuit — packets
flow continuously regardless of speech — so RX = 0 is absence, not silence.

The fix is a single vendor command, and the controller accepts it:

```
$ sudo hcitool -i hci0 cmd 0x3f 0x1c 0x01 0x02 0x00 0x01 0x01
  01 1C FC 00                                  # status 0x00 success
$ sudo hcitool -i hci0 cmd 0x3f 0x1d
  01 1D FC 00  01 02 00 01 01                  # routing now 0x01 Transport
```

### Link quality achieved

From the `Synchronous Connect Complete` event:

```
Link type: eSCO (0x02)          Transmission interval: 0x0c
RX/TX packet length: 60          Retransmission window: 0x04
Air mode: Transparent (0x03)
```

and the codec negotiation over RFCOMM:

```
AT+BAC=1,2,3   ->  +BCS: 2  ->  AT+BCS=2
```

**Codec 2 = mSBC.** Transparent air mode with 60-byte frames is exactly wideband speech, so the
uplink quality ceiling is **16 kHz, not 8 kHz CVSD**. That is the best case the plan hoped for.

### Android's view, concurrently

```
SCO state          : SCO_STATE_ACTIVE_INTERNAL
active comm device : bt_sco_hs  larkbridge
audio mode         : MODE_IN_COMMUNICATION  (owner: com.discord)
```

Android routed a live Discord call's audio to the Pi over Bluetooth SCO.

With routing fixed, PipeWire finally exposes both HFP nodes:
`bluez_input.5C_33_7B_CB_BF_C5.0` and `bluez_output.5C_33_7B_CB_BF_C5.1`. **This also answers
the question left open in E02**: the HFP card materialises only once an SCO transport exists.

## Verdict

**CONFIRMED — SCO data crosses the HCI transport in both directions, with mSBC wideband, once
`sco_routing` is set to Transport.**

Verdict code: `SCO_OK` (with `--apply-vendor-cmd`). Baseline without the fix: `SCO_HALF_DUPLEX`.

## Production fix

`bridge-btfw.service` + `/usr/local/lib/rpi-lark-bridge/set-sco-routing.sh`, installed and
enabled `WantedBy=bluetooth.service`. It is idempotent, and it **verifies the read-back** rather
than assuming the write took — the btstack-dev thread describes firmware that accepts this
command and ignores it. Tested both ways: no-ops when already correct, and repairs when forced
back to PCM.

The device-tree route (`brcm,bt-pcm-int-params` in `bridge-bt-sco-overlay.dts`) remains viable —
the kernel binds via serdev — but is not needed now that the runtime unit is proven, and the
runtime unit works regardless of attach mechanism.

## Consequences for the plan

| Verdict | What happens |
|---|---|
| `SCO_OK` on baseline | Risk R2 closes at probability 0. No overlay or vendor service needed. Delete the `bridge-btfw` SCO step from the installer. |
| `SCO_OK` only with the vendor command | R2 closes. `bridge-btfw.service` becomes mandatory and ships. Try the DT overlay as the cleaner equivalent; keep the runtime command as fallback. |
| `SCO_LINK_NO_DATA` after both fixes | **Mode 1 and Mode 1W are both dead on the onboard radio.** Escalate to `PLAN.md` §15 Q1 before writing any more Bluetooth code. Mode 2 (Pico) becomes the only path, and the plan's shape changes. |
| `NO_SCO_LINK` | Not a routing problem. Run E02 first; this experiment is invalid until an SCO link is being attempted at all. |

## Follow-up questions this raised

_(fill in — e.g. does the routing survive a controller reset? a reboot? a firmware reload?)_
