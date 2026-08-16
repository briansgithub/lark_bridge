# rpi-lark-bridge — Implementation Plan

**Target:** a single repository that turns a Raspberry Pi 3 Model B v1.2 + Raspberry Pi Pico into an
audio-routing bridge that lets a Hollyland Lark A1 (capture-only USB-C receiver) act as the
microphone for calls on a Google Pixel 7a (Android 14), while call audio is played out somewhere
else entirely.

**Status:** planning only. No code has been written. This document is the specification another
coding agent executes milestone by milestone.

**Date of research:** 2026-08-15. Software baseline: Raspberry Pi OS "Trixie" (Debian 13, released
2025-10-01), PipeWire 1.4.2, WirePlumber 0.5.8, BlueZ 5.79+ (verify with `apt policy bluez` on the
actual image), pico-sdk 2.x with vendored TinyUSB.

---

## 0. How to read this document

Every non-obvious claim is tagged:

| Tag | Meaning |
|---|---|
| **[DOC]** | Documented in official docs, upstream source, or a device-tree binding. Cited in §16. |
| **[INF]** | Architectural inference from the documented facts. Reasonable, not proven. |
| **[TEST]** | Cannot be settled by reading. A specific experiment in this plan resolves it. |

The plan is deliberately **risk-first**: three throwaway spikes run before any product code, because
if spike S1 fails, Mode 1 changes shape completely and there is no point building around it.

---

## 1. Architecture decision

### 1.1 Recommended final architecture

Three modes, not two. The third one — Mode 1W — is the change I most want you to accept, and the
reasoning is in §1.4.

```
                          ┌──────────────────────────────────────────────┐
                          │            SHARED CORE (always on)           │
                          │                                              │
  Hollyland Lark A1 ─USB─►│  ALSA USB capture ─► PipeWire graph @48 kHz  │
  (capture-only UAC)      │        │                                     │
                          │        ├─ bridge.mic      (loopback)         │
                          │        ├─ bridge.callout  (loopback)         │
                          │        ├─ bridge.monitor  (optional tap)     │
                          │                                              │
                          │  bridged (Python) ── bridgectl (CLI)         │
                          │  WirePlumber policy · BlueZ · systemd        │
                          └───────┬───────────────────────┬──────────────┘
                                  │                       │
                ┌─────────────────┘                       └──────────────────┐
                ▼                                                            ▼
   ┌────────────────────────────┐                        ┌────────────────────────────────┐
   │ MODE 1  Bluetooth bridge   │                        │ MODE 2  USB headset bridge     │
   │                            │                        │                                │
   │  HFP HF ──────► Pixel 7a   │                        │  I2S duplex ──► Pico ──USB──►  │
   │  A2DP src ────► headphones │                        │                     Pixel 7a   │
   │  (one radio, hci0)         │                        │  (Pi is I2S clock master)      │
   └────────────────────────────┘                        └────────────────────────────────┘
                │
                ▼
   ┌────────────────────────────┐
   │ MODE 1W  BT call + wired   │   ← de-risked MVP variant of Mode 1
   │  HFP HF ──────► Pixel 7a   │
   │  call audio ──► USB DAC /  │
   │                3.5 mm jack │
   │  (radio does HFP only)     │
   └────────────────────────────┘
```

### 1.2 Mode 1 — Bluetooth call bridge (primary wireless target)

```
  Hollyland Lark A1
        │  USB Audio Class capture, 48 kHz (assumed — measure in M1)
        ▼
  ┌──────────────────────── Raspberry Pi 3B v1.2 ────────────────────────┐
  │                                                                       │
  │  alsa_input.usb-Hollyland_...       48 kHz S16/S24 mono or stereo     │
  │            │                                                          │
  │            ▼                                                          │
  │  [bridge.mic loopback]  gain / mute / (future) noise suppression      │
  │            │                                                          │
  │            ▼   48 kHz → 16 kHz  (resample #1, unavoidable)            │
  │  bluez_output.<PIXEL_MAC>.handsfree-head-unit   (HFP HF sink)         │
  │                                                                       │
  │  bluez_input.<PIXEL_MAC>.handsfree-head-unit    (HFP HF source)       │
  │            │   16 kHz → 48 kHz  (resample #2, unavoidable)            │
  │            ▼                                                          │
  │  [bridge.callout loopback]  volume / (future) AEC reference           │
  │            │                                                          │
  │            ▼   48 kHz, no resample if SBC negotiated at 48 kHz        │
  │  bluez_output.<SINK_MAC>.a2dp-sink               (A2DP Source)        │
  └───────────────────────────────────────────────────────────────────────┘
        │ hci0 — ONE Broadcom BCM43438 radio carrying both links
        ├──────── HFP / eSCO ──────────► Pixel 7a          (uplink + downlink)
        └──────── A2DP / ACL ──────────► headphones or car stereo
```

Android sees: one paired device advertising **HFP Hands-Free (UUID 0000111e)** and nothing else it
can push media to. It treats it as a mono communication headset, which is exactly the goal.

### 1.3 Mode 2 — USB headset bridge (wired fallback / independent path)

```
  Lark A1 ──USB──► Pi 3B ──I2S full duplex──► Pico ──USB device──► Pixel 7a
                     ▲                          │
                     └──────── I2S RX ──────────┘

  ┌─── Pi 3B ──────────────────────────────┐   ┌─── Pico (RP2040) ────────────┐
  │ Lark capture 48 kHz                    │   │ PIO I2S slave (BCLK/LRCLK in)│
  │        ▼                               │   │        │ DMA double-buffered │
  │ [bridge.mic] ──► snd_rpi_* I2S PLAYBACK├──►│ RX ring ──► USB ISO IN  ────►│──► Pixel mic
  │                                        │   │                              │
  │ [bridge.callout] ◄── I2S CAPTURE   ◄───┤◄──│ TX ring ◄── USB ISO OUT ◄────│◄── Pixel playback
  │        ▼                               │   │            + feedback EP     │
  │  A2DP sink / USB DAC / 3.5 mm / file   │   │ Pi is I2S clock master       │
  └────────────────────────────────────────┘   └──────────────────────────────┘
```

Note the direction naming trap: **the Pi's I2S _playback_ substream carries the microphone** (Lark →
Pico → Pixel), and the Pi's I2S _capture_ substream carries the phone's playback. Name the PipeWire
nodes `bridge.i2s.to-pico` / `bridge.i2s.from-pico`, never "playback"/"capture", or you will wire it
backwards at 2 a.m.

### 1.4 Mode 1W — Bluetooth call + wired output (recommended MVP)

Identical to Mode 1 except the last hop: call downlink goes to a **USB audio dongle** (a $6 CM108/
CM109-class UAC1 device) or the Pi's 3.5 mm jack instead of A2DP.

Why this exists as a first-class mode rather than a footnote:

1. It removes the single highest-risk element (SCO + A2DP on one radio) from the MVP while keeping
   everything else identical. If Stage E fails, you still have a working product.
2. It is **lower latency**. A2DP headphones add 150–250 ms one-way on the path where you hear the
   far end. That is on top of HFP and network delay and is conversationally noticeable. [INF]
3. It costs nothing: the graph difference is one `target.object` string in one loopback definition.
4. It gives Stage E a clean control group — any degradation measured in Mode 1 that is absent in
   Mode 1W is attributable to radio contention, not to the audio graph.

### 1.5 What is shared between all modes

| Shared | Not shared |
|---|---|
| Lark capture node + its udev-stable name | Which sink `bridge.callout` targets |
| `bridge.mic` / `bridge.callout` loopbacks | Which sink `bridge.mic` targets |
| PipeWire/WirePlumber daemon config, clock rate, quantum | BlueZ profile set actually connected |
| `bridged`, `bridgectl`, IPC, health checks, logging | I2S card presence (Mode 2 only) |
| systemd unit graph, udev rules, install scripts | Pico firmware (Mode 2 only) |
| Diagnostics, log collection, test harness | — |

The mode switch is therefore **"which two `target.object` properties are set, and which BlueZ
connections are held open."** Nothing about the graph topology changes shape. That is the single most
important simplification in this design: there is exactly one audio graph, parameterised.

---

## 2. Feasibility assessment

Confidence is my probability that the link works well enough for daily use without a redesign.

| # | Link | Feasibility | Conf. | Key risks | Experiment that resolves it |
|---|---|---|---|---|---|
| L1 | Lark A1 → Pi ALSA capture | High | 0.95 | Non-48k native rate; mono-in-stereo; AGC in receiver | M1 / Stage A |
| L2 | Pi → A2DP Source → headphones | High | 0.95 | Codec/rate negotiation forcing 44.1 kHz; hw volume quirks | M3 / Stage D |
| L3 | BlueZ+PipeWire as **HFP HF** toward an Android AG | Med-High | 0.65 | PipeWire native-backend HF path is the less-travelled direction; a Gentoo report has WirePlumber segfaulting when `hfp_hf` handling is exercised in a system service [DOC] | Spike **S2**, M4 / Stage B |
| L4 | **SCO over HCI on BCM43438** | Medium | 0.50 | Pi 3's BT chip has no PCM/I2S pins wired, so SCO *must* go over HCI; Broadcom historically shipped it defaulting to PCM and undocumented [DOC] | Spike **S1** — do this first |
| L5 | **Simultaneous HFP/eSCO + A2DP on one radio** | Med-Low | 0.35 clean / 0.70 usable-with-artifacts | eSCO reserves slots; A2DP ACL must fit around them; BCM43438 + Wi-Fi coexistence on the same die | Spike **S3**, M6 / Stage E |
| L6 | Android treats Pi as a comm headset for cellular + Discord | Med-High | 0.80 | Class-of-Device, profile advertisement, per-app audio policy | M5, M12 |
| L7 | Pico enumerates on Pixel 7a as UAC2 headset | Med-High | 0.75 | AOSP docs only commit to a **UAC1 subset** [DOC]; UAC2 works on Pixel 6+ per field reports but is not a documented guarantee | M9 / Stage F |
| L8 | Android routes **calls** (not just media) to a USB headset | Medium | 0.55 | Telephony routing to USB is OEM/policy dependent and undocumented | M12 |
| L9 | Pi full-duplex I2S (`bcm2835-i2s`) | Med-High | 0.75 | Full duplex needs a machine driver with a duplex-capable codec node; several forum reports of "only the first-started direction works" | M10 / Stage G |
| L10 | RP2040 PIO I2S slave + DMA | High | 0.85 | Proven implementations exist (`malacalypse/rp2040_i2s_example` has `i2s_bidi_slave`) [DOC] | M9b |
| L11 | Pico USB↔I2S rate reconciliation | Medium | 0.70 | UAC2 explicit feedback endpoint behaviour on Android is untested; TinyUSB `uac2_headset` has open RP2040 stability issues (#1728 open, #2838 fixed via PR #1802) [DOC] | M9, M11 |
| L12 | Whole system survives reboot + reconnect unattended | Medium | 0.60 | BlueZ stale state, PipeWire suspend, USB renumeration | M8, Stage I |

### 2.1 The scrutinised item: one controller doing HFP/SCO + A2DP

**What is documented.** The BCM43438 on the Pi 3B is a combo Wi-Fi/BT die attached over UART
(`/dev/serial1`, `hci_uart`/`btbcm`). Its PCM/I2S audio pins are **not routed to anything on the Pi
3B PCB** [DOC]. Therefore HFP audio has only one possible path: SCO packets multiplexed over the HCI
UART transport. Broadcom parts configure SCO routing with the vendor command
`Write_SCO_PCM_Int_Param` (OGF 0x3F, OCF 0x1C). The Linux binding
`Documentation/devicetree/bindings/net/broadcom-bluetooth.yaml` exposes this as a 5-byte device-tree
property:

```
brcm,bt-pcm-int-params = <sco-routing pcm-interface-rate frame-type sync-mode clock-mode>;
      sco-routing:  0=PCM  1=Transport(HCI)  2=Codec  3=I2S
```
[DOC]. The Pi 3B device tree (`bcm2837-rpi-3-b.dts`) sets only `shutdown-gpios` on the `bt` node — it
does **not** set `brcm,bt-pcm-int-params` [DOC]. So out of the box the controller may be routing SCO
to dead PCM pins. That is the most likely explanation for the long-standing "A2DP works, HSP/HFP is
silent on Pi 3" reports (raspberrypi/linux#2229, open since 2017) [DOC].

**Two candidate fixes, both cheap to test (spike S1):**
- **S1a:** ship a device-tree overlay adding `brcm,bt-pcm-int-params = <0x01 0x02 0x00 0x01 0x01>;`
  to the `bt` node. Requires the kernel to be driving the chip via serdev/`hci_bcm` rather than
  userspace `hciattach` from `hciuart.service` — check which one Raspberry Pi OS Trixie actually uses
  before assuming the property is read.
- **S1b:** send the raw vendor command post-attach:
  `hcitool cmd 0x3F 0x1C 0x01 0x02 0x00 0x01 0x01`, wrapped in a `bridge-btfw.service` that runs
  after `bluetooth.service`. Ugly but transport-agnostic, and it works whether or not the DT property
  is honoured.

S1a is the correct engineering answer; S1b is the fallback and also the fast way to *learn the
answer* in an afternoon. Do S1b first for the knowledge, then S1a for the product.

**What is not documented and must be measured.** Whether the controller's scheduler can sustain a
2-slot eSCO reservation (EV3/2-EV3, ~64 kbit/s each way) alongside an A2DP ACL stream (~200–350
kbit/s for SBC at 48 kHz) *while also* running Wi-Fi on the same die, over a 3 Mbit/s UART with
SCO-over-HCI framing. Nobody publishes this. There is no substitute for Stage E. [TEST]

**My honest expectation** [INF]: HFP-only and A2DP-only will both work. Simultaneous operation will
"work" but with periodic A2DP dropouts during SCO, worse when 2.4 GHz Wi-Fi is up. Mitigations in
priority order: (1) disable onboard Wi-Fi (`dtoverlay=disable-wifi`) and use Ethernet, (2) force SBC
at the lowest acceptable bitpool to shrink the ACL duty cycle, (3) increase A2DP buffering to ride
through SCO reservation windows, (4) fall back to Mode 1W. If after all four it is still bad, the
plan is to **document it as a measured controller limitation with btmon evidence**, per your
instruction — not to quietly add a dongle.

**Note it is not free even if it works:** the call downlink is capped at HFP quality (16 kHz mSBC at
best, 8 kHz CVSD at worst). A2DP's fidelity is irrelevant here; only its *latency* and *reliability*
matter. Configure A2DP for latency, not quality.

---

## 3. Repository tree

```
rpi-lark-bridge/
├── README.md                      One-screen what/why, hardware list, quickstart, mode table
├── LICENSE
├── CHANGELOG.md                   Keep-a-changelog; milestones land here
├── VERSION                        Single source of truth; read by bridgectl --version
├── Makefile                       Thin façade: make lint | test-host | pico | install | docs
├── .editorconfig  .gitignore  .gitattributes
├── .github/workflows/
│   ├── lint.yml                   shellcheck, ruff, black --check, clang-format, dtc syntax
│   ├── pi.yml                     pytest for bridged/bridgectl on host (no hardware)
│   └── pico.yml                   Cross-compile Pico firmware (all descriptor variants) + host unit tests
│
├── docs/
│   ├── PLAN.md                    This document. The contract.
│   ├── architecture/
│   │   ├── overview.md            §1 expanded, with the canonical ASCII diagrams
│   │   ├── audio-dataflow.md      §5: every PCM hop, format, and resampler
│   │   ├── bluetooth.md           §6: roles, UUIDs, profile naming convention, reconnect FSM
│   │   ├── pico-usb.md            §7: descriptors, endpoints, buffering
│   │   ├── i2s-transport.md       §7: clocking, PIO, DMA, drift
│   │   ├── observability.md       Log schema, metric names, status JSON schema
│   │   └── decisions/             ADRs, one file each, immutable once accepted
│   │       ├── ADR-0001-three-modes.md
│   │       ├── ADR-0002-loopback-over-link-daemon.md
│   │       ├── ADR-0003-python-for-bridged.md
│   │       ├── ADR-0004-i2s-pi-is-clock-master.md
│   │       ├── ADR-0005-uac2-primary-uac1-fallback.md
│   │       ├── ADR-0006-user-session-not-system-pipewire.md
│   │       ├── ADR-0007-48k-internal-graph.md
│   │       └── ADR-0008-platformio-arduino-pico-build.md
│   ├── hardware/
│   │   ├── bom.md                 Exact parts, incl. the USB DAC for Mode 1W and the cable list
│   │   ├── wiring-pi-pico.md      §12 pin table, photos, cable-length limits
│   │   ├── wiring-diagram.svg     Generated from tools/report/wiring.py; .txt ASCII twin in repo
│   │   ├── power.md               Schottky-into-VSYS rationale, what must NOT be connected
│   │   └── lark-a1.md             Measured descriptors, rates, gain behaviour (filled in at M1)
│   ├── operations/
│   │   ├── install.md             §13 fresh-system procedure
│   │   ├── pairing.md             pair-phone / pair-output, trust, re-pair, forget
│   │   ├── modes.md               What each mode does, how to switch, what breaks
│   │   ├── recovery.md            §"it stopped working" decision tree
│   │   ├── troubleshooting.md     Symptom → command → interpretation table
│   │   └── flashing-pico.md       BOOTSEL, picotool, the Pi-drives-RUN reset trick
│   ├── experiments/
│   │   ├── TEMPLATE.md            Question / method / raw data / verdict / confidence delta
│   │   ├── E01-sco-over-hci.md            (spike S1)
│   │   ├── E02-hfp-hf-role.md             (spike S2)
│   │   ├── E03-hfp-a2dp-coexistence.md    (spike S3)
│   │   ├── E04-lark-capabilities.md
│   │   ├── E05-android-usb-routing.md
│   │   ├── E06-i2s-duplex-overlay.md
│   │   └── results/               btmon .btsnoop, wav, pw-dump json, csv — the evidence
│   ├── development/
│   │   ├── setup.md               Dev host toolchain, cross-build, how to work without hardware
│   │   ├── testing.md             How to run each stage; what "pass" means
│   │   ├── style.md               Language rules (§4.9), naming, log conventions
│   │   └── release.md             Version bump, tag, image build
│   └── reference/
│       ├── glossary.md            SCO/eSCO, mSBC, CVSD, HF/AG, SEP, quantum, XRUN…
│       └── external-sources.md    §16 link list with retrieval dates
│
├── pi/
│   ├── bridged/                          The daemon. Python package, one wheel, two entry points.
│   │   ├── pyproject.toml                deps: dbus-fast, click, rich, tomli-w, jsonschema
│   │   ├── src/bridged/
│   │   │   ├── __main__.py               systemd entry; signal handling; sd_notify READY/WATCHDOG
│   │   │   ├── config.py                 Load/validate config/bridge.toml against JSON schema
│   │   │   ├── state.py                  Mode FSM: OFF→STARTING→READY→DEGRADED→RECOVERING
│   │   │   ├── modes/
│   │   │   │   ├── base.py               enter()/exit()/health() contract
│   │   │   │   ├── bluetooth.py          Mode 1 / 1W: hold HFP + (A2DP | wired) targets
│   │   │   │   ├── usb.py                Mode 2: require I2S card + Pico, set targets
│   │   │   │   └── diagnostics.py        Mode 3: tear down policy, expose raw devices
│   │   │   ├── bluez/
│   │   │   │   ├── objects.py            ObjectManager cache; Adapter1/Device1/MediaTransport1
│   │   │   │   ├── agent.py              org.bluez.Agent1 (NoInputNoOutput) for headless pairing
│   │   │   │   ├── connect.py            Connect/ConnectProfile with backoff, per-UUID
│   │   │   │   ├── transport.py          MediaTransport1 state + codec + volume introspection
│   │   │   │   └── recovery.py           hciconfig reset → rfkill cycle → btattach restart ladder
│   │   │   ├── pw/
│   │   │   │   ├── dump.py               pw-dump subprocess + parse (structured, cached)
│   │   │   │   ├── graph.py              Node/port/link model; find-by-property, never by id
│   │   │   │   ├── targets.py            Set metadata target.object on the loopback nodes
│   │   │   │   └── metrics.py            XRUN counters, quantum, rate, driver, buffer fill
│   │   │   ├── alsa/cards.py             /proc/asound parse: cards, xruns, hw params
│   │   │   ├── health/
│   │   │   │   ├── checks.py             ~20 named checks, each returns OK/WARN/FAIL + evidence
│   │   │   │   └── recovery.py           Escalation ladder with rate limits and hysteresis
│   │   │   ├── ipc/
│   │   │   │   ├── server.py             Unix socket @ $XDG_RUNTIME_DIR/bridge.sock, 0600
│   │   │   │   └── protocol.py           Versioned line-delimited JSON request/response
│   │   │   ├── logging.py                journald structured fields; one event = one JSON line
│   │   │   └── pico.py                   Pico presence/health via udev + optional CDC stats port
│   │   ├── src/bridgectl/
│   │   │   ├── cli.py                    click group: mode/status/diagnostics/pair-*/logs/tap
│   │   │   ├── render.py                 rich tables for humans; --json for machines
│   │   │   └── selftest.py               `bridgectl doctor` — runs health checks standalone
│   │   └── tests/                        pytest; fixtures = recorded pw-dump/bluez JSON
│   ├── pipewire/pipewire.conf.d/
│   │   ├── 10-bridge-clock.conf          rate 48000, quantum, allowed-rates, resample.quality
│   │   ├── 20-bridge-endpoints.conf      module-loopback: bridge.mic, bridge.callout, bridge.tap
│   │   ├── 30-bridge-alsa.conf           ALSA properties: period/headroom for USB + I2S cards
│   │   └── 40-bridge-filters.conf.disabled   AEC/sidetone/gain filter-chain, off by default
│   ├── wireplumber/wireplumber.conf.d/
│   │   ├── 50-bridge-bluez.conf          bluez5.roles, codecs, msbc, backend, seat-monitoring
│   │   ├── 51-bridge-bluez-rules.conf    Per-device rules keyed on MAC: autoconnect, priorities
│   │   ├── 52-bridge-alsa.conf           Disable auto-suspend; pin node.name for Lark + I2S card
│   │   └── 60-bridge-policy.conf         Disable autoswitch-to-headset-profile; no desktop policy
│   ├── bluez/
│   │   ├── main.conf.d/10-bridge.conf    Class, AutoEnable, Reconnect*, ControllerMode=bredr
│   │   └── apply-main-conf.sh            Merges fragment if the distro BlueZ lacks main.conf.d
│   ├── systemd/
│   │   ├── system/
│   │   │   ├── bridge-btfw.service       SCO-routing vendor cmd + CoD, After=bluetooth.service
│   │   │   ├── bridge-boot.service       Assert overlays/config; fail loudly and early
│   │   │   └── bridge-picoreset.service  Optional: pulse Pico RUN on boot
│   │   └── user/
│   │       ├── bridged.service           Type=notify, WatchdogSec, Restart=always, After=wireplumber
│   │       ├── pipewire.service.d/10-bridge.conf        Restart hardening, rt limits
│   │       └── wireplumber.service.d/10-bridge.conf
│   ├── udev/
│   │   ├── 90-bridge-lark.rules          SYMLINK + ENV{BRIDGE_ROLE}="lark" by VID:PID+serial
│   │   ├── 91-bridge-pico.rules          BRIDGE_ROLE=pico; tag CDC diag port
│   │   └── 92-bridge-usbdac.rules        BRIDGE_ROLE=wired-out for Mode 1W dongle
│   ├── boot/
│   │   ├── config.txt.fragment           dtparam/dtoverlay lines the installer merges idempotently
│   │   ├── cmdline.txt.fragment
│   │   └── overlays/
│   │       ├── bridge-i2s-duplex-overlay.dts   simple-audio-card + dummy duplex codec
│   │       ├── bridge-bt-sco-overlay.dts       brcm,bt-pcm-int-params
│   │       └── Makefile                        dtc build + install to /boot/firmware/overlays
│   └── limits/
│       ├── 95-bridge-rtprio.conf         /etc/security/limits.d — rtprio/memlock for the bridge user
│       └── 95-bridge-sysctl.conf
│
├── pico/
│   ├── platformio.ini                    PRIMARY build (VS Code). arduino-pico core, -DUSE_TINYUSB.
│   ├── CMakeLists.txt                    Reference build for CI + escape hatch. Same sources.
│   ├── pico_sdk_import.cmake
│   ├── pio_main.cpp                      ~15-line PlatformIO shim: setup()→bridge_main_core0(),
│   │                                     setup1()/loop1()→core1. No logic. Excluded from CMake build.
│   ├── src/
│   │   ├── main.c                        bridge_main_core0/1(). Core0: USB. Core1: I2S. No Arduino API.
│   │   ├── board_config.h                THE pin definitions. Single source of truth.
│   │   ├── audio_pipeline.c/.h           Ring plumbing, format conversion, mute/gain
│   │   ├── ring_buffer.c/.h              SPSC lock-free, power-of-two, host-testable
│   │   ├── rate_control.c/.h             Feedback value + fallback drop/dup; PI controller
│   │   ├── led_status.c/.h               Onboard LED codes: enum/streaming/underrun/I2S-loss
│   │   ├── diag_cdc.c/.h                 CDC console: `stats`, `ver`, `reset`; off in release
│   │   └── watchdog.c                    hw watchdog + panic → reboot to known state
│   ├── usb/
│   │   ├── tusb_config.h
│   │   ├── usb_descriptors.c/.h          Shared: device desc, strings, IAD, serial from flash ID
│   │   ├── desc_uac2.c                   UAC2 topology (primary)
│   │   ├── desc_uac1.c                   UAC1 topology (fallback build)
│   │   └── audio_callbacks.c             tud_audio_* hooks, clock get/set, volume/mute FU
│   ├── i2s/
│   │   ├── i2s_slave.pio                 RX+TX slave programs, external BCLK/LRCLK — SOURCE OF TRUTH
│   │   ├── i2s_slave.pio.h               Pre-generated by pioasm and COMMITTED (PlatformIO can't run
│   │   │                                 pioasm). `make pio` regenerates; CI fails if it differs.
│   │   ├── i2s_slave.c/.h                Init, SM alloc, pin binding, start/stop
│   │   └── i2s_dma.c/.h                  Chained double-buffer DMA + IRQ handlers
│   ├── test/
│   │   ├── host/                         Unity: ring_buffer, rate_control, format conv
│   │   └── target/
│   │       ├── tone_only/                Firmware variant: 1 kHz tone → USB IN (Stage F)
│   │       ├── usb_loopback/             USB OUT → USB IN (Stage F)
│   │       └── i2s_loopback/             I2S RX → I2S TX (Stage G)
│   └── tools/flash.sh                    picotool load -f, or RUN-pin reset + UF2 copy
│
├── config/
│   ├── bridge.toml.example               Devices (MAC), mode, gains, latency targets
│   ├── profiles/{headphones,car,wired}.toml
│   └── schema/bridge.schema.json
│
├── scripts/
│   ├── install.sh                        Orchestrator; --mode, --dry-run, --skip, idempotent
│   ├── bootstrap/
│   │   ├── 00-preflight.sh               Model/OS/kernel checks. Refuse unknown platforms.
│   │   ├── 10-packages.sh                apt install, pinned list from config
│   │   ├── 20-user.sh                    Create `bridge` user, groups, enable-linger
│   │   ├── 30-boot-config.sh             config.txt/overlays, disable-wifi, dwc_otg tuning
│   │   ├── 40-audio.sh                   PipeWire/WirePlumber config, limits, udev
│   │   ├── 50-bluetooth.sh               BlueZ conf, CoD, SCO routing service
│   │   ├── 60-services.sh                Install + enable units, reload
│   │   └── 70-verify.sh                  Runs `bridgectl doctor`; non-zero exit on FAIL
│   ├── uninstall.sh                      Reverses every step; leaves no orphans
│   ├── pair-phone.sh / pair-output.sh    Thin wrappers over `bridgectl pair-*`
│   ├── bt-reset.sh                       Manual escalation ladder for wedged controllers
│   ├── collect-logs.sh                   tar.gz: journals, pw-dump, btmon, /proc/asound, config
│   ├── build-pico.sh  flash-pico.sh
│   └── lib/common.sh                     log/die/require_root/idempotent_append/backup_file
│
├── tests/
│   ├── run-stage.sh                      ./tests/run-stage.sh E --duration 3600 --report
│   ├── stage-a-lark/ … stage-i-recovery/ One dir per stage; each has run.sh + expect.yaml
│   ├── lib/
│   │   ├── measure.py                    Common harness: start/stop, collect, assert, emit JSON
│   │   ├── analyze_wav.py                Silence, clipping, dropout-gap, DC, level, SNR
│   │   ├── glitch_detect.py              Tone continuity → dropout count/duration histogram
│   │   ├── btmon_parse.py                SCO/eSCO setup, codec, disconnect reasons, retransmits
│   │   ├── xrun.py                       /proc/asound + pw metrics deltas
│   │   └── assertions.sh
│   └── fixtures/                         Golden pw-dump/bluez JSON, reference tones
│
├── tools/
│   ├── audio/{tone_gen.py,latency_probe.py,level_meter.py}
│   ├── bt/{btmon_capture.sh,codec_report.py,sco_probe.py}
│   ├── pw/{graph_dump.py,graph_render.py,xrun_watch.py}
│   └── report/{make_report.py,wiring.py,templates/}
│
└── third_party/NOTICE.md                 TinyUSB, pico-sdk, any PIO code adapted (license + origin)
```

**Directory rationale, briefly.** `pi/` and `pico/` split by *target*, not by language, because they
cross-compile differently and are flashed differently. Config lives beside the component that owns it
(`pi/pipewire/`, `pi/bluez/`) rather than in a global `config/` — `config/` holds only *user* config.
`docs/experiments/` is a first-class directory because half this project is measurement, and those
measurements must outlive the session that made them. `tests/` is staged to mirror §9 exactly so
"run Stage E" is a literal command, not an interpretation.

---

## 4. Component architecture

### 4.1 `bridged` — the supervisor daemon

| | |
|---|---|
| **Responsibility** | Own the mode FSM; hold BlueZ connections; point the two loopbacks at the right targets; run health checks; escalate recovery; serve status over IPC. **Never touches PCM samples.** |
| **Language** | Python 3.11+ |
| **Why** | It is an I/O-and-policy component: D-Bus to BlueZ, subprocess to `pw-dump`, JSON to a socket. Python's async D-Bus story (`dbus-fast`) is the best available, and the whole point of this component is that a human can read it at 2 a.m. and understand why it did what it did. The hard rule that makes this safe: **if a component touches samples in steady state, it is not Python — and preferably it does not exist, because PipeWire already does that job.** |
| **Inputs** | `config/bridge.toml`; BlueZ `ObjectManager` signals; `pw-dump` JSON; `/proc/asound/*`; udev events; Pico CDC stats (optional) |
| **Outputs** | PipeWire metadata writes (`target.object`); BlueZ `Connect`/`ConnectProfile`/`Disconnect`; journald structured logs; IPC responses |
| **Process** | One process, user unit, runs as `bridge` |
| **systemd** | `Type=notify`, `WatchdogSec=30`, `Restart=always`, `RestartSec=2`, `After=wireplumber.service`, `PartOf=bridge.target` |
| **Deps** | pipewire-utils, bluez, dbus-fast, click, jsonschema |
| **Interfaces** | Unix socket `$XDG_RUNTIME_DIR/bridge.sock`, versioned JSON-lines protocol |

### 4.2 `bridgectl` — the CLI

**Yes, build it, and build it early (M7).** Justification: you will run diagnostics hundreds of times
across nine test stages. A CLI that prints one authoritative status page is worth more than the code
it costs, and it doubles as the acceptance-test interface. Same Python package, separate console
script. Talks only over IPC — it never pokes PipeWire or BlueZ directly, so `bridgectl status` and
`bridged`'s internal view can never disagree.

```
bridgectl mode bluetooth|bluetooth-wired|usb|diagnostics
bridgectl status [--json] [--watch]
bridgectl doctor                     # standalone checks, works even if bridged is down
bridgectl pair-phone [--timeout 120]
bridgectl pair-output [--timeout 120]
bridgectl devices [--forget MAC]
bridgectl tap start|stop [--point mic|callout|hfp-rx|i2s-rx] [--seconds N]
bridgectl graph [--dot|--json]
bridgectl logs [--since 10m] [--collect]
bridgectl mute|unmute|gain <dB>
```

### 4.3 PipeWire + WirePlumber (upstream, configured — not modified)

The routing decision. Options considered: WirePlumber Lua scripts, `pw-link` from shell, a custom
PipeWire client, ALSA loopback devices, `module-loopback`.

**Chosen: `libpipewire-module-loopback` instances declared in `pipewire.conf.d`, with targets set by
`bridged` via PipeWire metadata.** (ADR-0002)

Why: a loopback creates a *persistent named node pair* that exists whether or not its target exists.
When the Bluetooth device disappears and comes back, PipeWire reattaches to `target.object` on its
own — no daemon in the reconnect path, no link-recreation race, nothing to forget. It also gives each
leg its own buffering and a natural insertion point for gain/AEC/sidetone later. `pw-link` shell
loops are explicitly **banned from production code** (they are fine inside `tests/`), because they
are imperative snapshots of a declarative problem, and a custom PipeWire client would be re-writing
`module-loopback` worse.

WirePlumber's job is narrowed to: device/profile policy, per-MAC rules, and *not* doing desktop
things. Disable `bluetooth.autoswitch-to-headset-profile` — desktop profile auto-switching is exactly
the "magic" that makes appliances nondeterministic.

### 4.4 BlueZ (upstream, configured) + `bridged.bluez.agent`

BlueZ provides the stack. `bridged` registers a `NoInputNoOutput` `org.bluez.Agent1` so headless
pairing works without `bluetoothctl` interactivity, and drives connect/trust/reconnect itself rather
than relying on BlueZ's `[Policy] AutoEnable` alone (which we still set as a belt-and-braces).

### 4.5 `bridge-btfw.service` (system unit, shell)

Applies the SCO-routing configuration and Class-of-Device after `bluetooth.service` is up. Shell,
~30 lines, because it is three `hciconfig`/`btmgmt`/`hcitool` calls with error checking. Ordering:
`After=bluetooth.service`, `Before=bridge.target`. Idempotent; logs the controller's response bytes.

### 4.6 Pico firmware

| | |
|---|---|
| **Responsibility** | Present a full-duplex USB Audio headset to the Pixel; move samples between USB and I2S; reconcile the two clock domains; report health |
| **Language** | C11 + TinyUSB. Not C++ (TinyUSB and the SDK are C; mixing buys nothing). Not Rust (`embassy-rp`/`rp-hal` UAC support is far behind TinyUSB; "use what upstream ships" clearly wins here) |
| **Build** | **PlatformIO + arduino-pico core is the primary/daily build** (ADR-0008); CMake + pico-sdk is retained in CI as the reference build. **All firmware sources are Arduino-API-free plain C** so both front-ends compile identical code. Any `#include <Arduino.h>` outside `pio_main.cpp` is a lint failure. |
| **Inputs** | USB ISO OUT (host playback), I2S RX (from Pi) |
| **Outputs** | USB ISO IN (mic to host), USB feedback EP, I2S TX (to Pi), LED, optional CDC diag |
| **Deps** | pico-sdk ≥2.1, TinyUSB as vendored by the SDK, `picotool` for flashing |
| **Interfaces** | USB descriptors (contract with Android); I2S electrical contract with the Pi; CDC text protocol for diagnostics |

### 4.7 Install / bootstrap scripts

**Language: POSIX-ish bash** (`set -euo pipefail`, shellcheck-clean). Justification: they run once on
a fresh image, before any Python environment is guaranteed, and they mostly call `apt`, `install`,
`systemctl`, `raspi-config`. Numbered fragments so `install.sh --skip 30` is meaningful and each
fragment is separately re-runnable. Every fragment must be idempotent and must back up any file it
edits to `<file>.bridge.bak`.

### 4.8 Test harness

**Language: Python** for anything that analyses audio or parses `btmon`/`pw-dump` (numpy/scipy make
dropout detection trivial), **bash** for the stage drivers that start/stop things. Each stage emits a
machine-readable `result.json` so §9's matrix can be regenerated rather than hand-maintained.

### 4.9 Language rules (put this in `docs/development/style.md`)

1. Sample-path code in steady state: **C** (Pico) or nothing — PipeWire does it.
2. Policy, orchestration, diagnostics: **Python**.
3. Provisioning and one-shot system mutation: **bash**.
4. Declarative behaviour: **config files**, not code. If you are writing code to do something
   PipeWire/WirePlumber/BlueZ config can express, stop.
5. **Lua only if forced.** WirePlumber 0.5 config is largely declarative; reach for Lua only when a
   policy cannot be expressed in `.conf` rules, and put it in `pi/wireplumber/scripts/` with a
   comment explaining what declarative attempt failed first.
6. No Rust in v1. Revisit only if a measured need appears (candidate: a userspace I2S↔ring shim if
   `bcm2835-i2s` duplex proves unusable — see §7.6 fallback).

---

## 5. Audio dataflow

### 5.1 Global rate decision (ADR-0007)

Internal graph runs at **48 kHz, F32 planar internally, 2-channel or 1-channel per node as needed**.
`default.clock.rate = 48000`, `default.clock.allowed-rates = [ 48000 ]` — deliberately *not*
including 16000. A single fixed graph rate is worth more than the resample it would save, because
Mode 1 has an HFP endpoint (16 kHz) and an A2DP endpoint (48 kHz) live at the same time; any dynamic
rate switch would thrash one of them.

Quantum: start at `default.clock.quantum = 1024` (21.3 ms) for stability on a Pi 3, then tune
downward to 512 or 256 in M8 with XRUN counts as the gate. `min-quantum = 256`, `max-quantum = 2048`.

### 5.2 Mode 1 — Bluetooth call bridge

| Hop | Format in | Format out | Conversion | Where |
|---|---|---|---|---|
| Lark receiver → USB | — | 48 kHz S16/S24, 1–2 ch | — | device (measure in M1) |
| USB → ALSA node | 48 kHz S16 | 48 kHz F32 | format only | `alsa_input.*` |
| ALSA node → `bridge.mic` | 48/F32 2ch | 48/F32 1ch | **channel: stereo→mono** (or pick L) | loopback |
| `bridge.mic` → HFP sink | 48/F32 1ch | **16 kHz S16 1ch** | **resample #1 (48k→16k)** + mSBC encode | PipeWire bluez5 sink |
| Air | mSBC over eSCO, 60-byte frames, 7.5 ms | | codec | controller |
| HFP source → `bridge.callout` | **16 kHz S16 1ch** | 48/F32 1ch | mSBC decode + **resample #2 (16k→48k)** | PipeWire bluez5 source |
| `bridge.callout` → A2DP sink | 48/F32 1ch | 48 kHz S16 2ch | **channel: mono→stereo** | loopback |
| A2DP sink → air | 48/S16 2ch | SBC | **codec encode** (no resample if 48 kHz negotiated) | PipeWire bluez5 sink |

**Exactly two resamples, both at the HFP boundary, both unavoidable** — HFP is a 16 kHz (or 8 kHz)
transport and nothing can change that. If the Pixel negotiates CVSD instead of mSBC, the same two
resamples become 48↔8 kHz and quality drops accordingly; `bridgectl status` must report which codec
is live, every time.

A third resample appears **only if** the A2DP sink refuses 48 kHz and forces 44.1 kHz. Mitigation:
prefer 48 kHz SBC via `bluez5.default.rate = 48000`; if the headphones insist on 44.1, accept the
resample and log it as a WARN.

Mode 1W replaces the last two rows with: `bridge.callout` → USB DAC sink at 48 kHz S16 2ch, zero
resampling, ~5–10 ms instead of ~150–250 ms.

### 5.3 Mode 2 — USB headset bridge

| Hop | Format | Conversion | Where |
|---|---|---|---|
| Lark → ALSA → `bridge.mic` | 48 kHz | channel map only | Pi |
| `bridge.mic` → I2S TX node | 48 kHz S32_LE 2ch (mono duplicated or L-only) | **format widen S16→S32**, no resample | Pi `bcm2835-i2s` |
| I2S air | 48 kHz, 64 BCLK/frame, BCLK 3.072 MHz | — | wire |
| Pico I2S RX → ring | S32 → S16 truncate/shift | format | Pico |
| Ring → USB ISO IN | 48 kHz S16 1ch, 96 B/frame | **adaptive ±1 sample/frame** | Pico |
| USB ISO OUT → ring | 48 kHz S16 2ch, 192 B/frame | — | Pico |
| Ring → I2S TX | S16 → S32 | format + **feedback EP rate steering** | Pico |
| Pi I2S RX → `bridge.callout` | 48 kHz S32 → F32 | format | Pi |
| `bridge.callout` → chosen sink | 48 kHz | depends on sink | Pi |

**Zero resampling in the nominal Mode 2 path.** The only rate adaptation is the Pico's ±1-sample
adjustment against the host's SOF clock, and the USB feedback endpoint, which is sample-*rate*
steering rather than sample-rate *conversion*. If `bridge.callout` then goes on to A2DP, §5.2's A2DP
row applies.

### 5.4 Buffering and latency budget (design targets, to be measured)

| Stage | Target | Notes |
|---|---|---|
| Lark USB capture | 8–16 ms | ALSA period ×2 |
| PipeWire quantum | 10–21 ms | 512–1024 @48k |
| HFP/eSCO air | ~15–30 ms | mSBC framing + retransmission window |
| A2DP air + headphone buffer | **150–250 ms** | dominant term; headphone-dependent |
| USB DAC (Mode 1W) | 5–10 ms | |
| I2S link | 2–4 ms | 1 ms DMA halves |
| Pico ring + USB | 4–8 ms | |
| **Mode 1 uplink** (Lark→far end) | **~50–80 ms** + network | acceptable |
| **Mode 1 downlink** (far end→ears) | **~200–300 ms** + network | noticeable; Mode 1W fixes it |
| **Mode 1W downlink** | **~50–70 ms** + network | |
| **Mode 2 uplink** | **~40–60 ms** | |

There is no acoustic loop in Mode 1/1W (the Lark is not fed the call audio), so latency is a comfort
issue, not a stability issue — except when the far-end audio leaks acoustically into the Lark, which
is what §5.5 is for.

### 5.5 Echo, sidetone, gain — architecture now, implementation later

AEC is **not** an MVP prerequisite: with headphones there is no loop at all. It becomes necessary
only in the car-speaker case. Leave room, don't build it:

- `pi/pipewire/pipewire.conf.d/40-bridge-filters.conf.disabled` ships a ready
  `module-echo-cancel` (WebRTC) definition wired as: **rec = `bridge.mic` pre-uplink,
  play = `bridge.callout` post-downlink**. Enabling it is renaming one file plus a restart.
- Both loopbacks already exist, so inserting a filter chain is a target change, not a re-plumb.
- Gain and mute live on the `bridge.mic` loopback's volume (settable via `bridgectl gain/mute`), not
  in the Lark's ALSA mixer — device mixers vanish on replug, node volumes do not.
- Sidetone (hearing yourself) is a third loopback `bridge.sidetone` from `bridge.mic` to the output
  sink at −20 dB, shipped disabled.

---

## 6. Bluetooth architecture

### 6.1 The naming convention you must internalise first

PipeWire's `bluez5.roles` values are named **from the remote device's perspective**, which is the
opposite of what most people assume and the single easiest way to misconfigure this project.
Confirmed from upstream source: `spa_bt_profile_from_uuid()` maps the *remote's advertised* UUID
`0000111e` (HFP HF) → `SPA_BT_PROFILE_HFP_HF`, and `backend-native.c` then runs the **AG** state
machine for that profile (`case SPA_BT_PROFILE_HFP_HF: rfcomm_process_events(..., rfcomm_hfp_ag)`)
[DOC].

| `bluez5.roles` value | Remote device is | **Pi acts as** | Use here |
|---|---|---|---|
| `hfp_ag` | Audio Gateway (the Pixel) | **HFP Hands-Free** | ✅ required for the phone |
| `hfp_hf` | Hands-Free unit (a headset) | Audio Gateway | ❌ disable |
| `hsp_ag` | HSP Audio Gateway | HSP Headset | ✅ keep as fallback |
| `hsp_hs` | HSP Headset | HSP AG | ❌ disable |
| `a2dp_sink` | A2DP Sink (headphones) | **A2DP Source** | ✅ required for output |
| `a2dp_source` | A2DP Source (a phone streaming) | A2DP Sink | ❌ disable — stops Android pushing media at us |
| `bap_*` | LE Audio | — | ❌ disable (Pi 3 is BR/EDR-era) |

### 6.2 Target configuration

`pi/wireplumber/wireplumber.conf.d/50-bridge-bluez.conf`:
```
monitor.bluez.properties = {
  bluez5.roles              = [ hfp_ag hsp_ag a2dp_sink ]
  bluez5.hfphsp-backend     = "native"
  bluez5.enable-msbc        = true
  bluez5.enable-sbc-xq      = false      # latency > fidelity for call audio
  bluez5.enable-hw-volume   = true
  bluez5.codecs             = [ sbc ]    # deterministic; add aac/aptx only after Stage E passes
  bluez5.default.rate       = 48000
  bluez5.default.channels   = 2
  bluez5.hw-offload-sco     = false      # no PCM path exists on Pi 3B
  monitor.bluez.seat-monitoring = "disabled"   # headless: no logind seat
}
wireplumber.settings = {
  bluetooth.autoswitch-to-headset-profile = false
  bluetooth.use-persistent-storage        = true
}
```
[DOC] for every property name and its default; the *values* are this project's choices.

`pi/bluez/main.conf.d/10-bridge.conf`:
```
[General]
Class = 0x000408          # major=Audio/Video, minor=Hands-free. Service bits are set by BlueZ
                          # from registered profiles; main.conf Class only carries major+minor. [DOC]
ControllerMode = bredr    # no LE role needed; reduces radio scheduling pressure
FastConnectable = true
[Policy]
AutoEnable = true
ReconnectAttempts = 7
ReconnectIntervals = 1,2,4,8,16,32,64
```

### 6.3 SCO transport

Because the BCM43438's PCM pins go nowhere on a Pi 3B, SCO **must** be routed to the HCI transport.
Set via `brcm,bt-pcm-int-params = <0x01 0x02 0x00 0x01 0x01>` in a DT overlay on the `bt` node
(`sco-routing=1 Transport`, `rate=512 kbps`, `frame=short`, `sync=master`, `clock=master`) [DOC], or
at runtime by `bridge-btfw.service` issuing `hcitool cmd 0x3f 0x1c 01 02 00 01 01`.

Codec preference: **mSBC (wideband, 16 kHz)** over CVSD (8 kHz). mSBC over SCO needs "transparent
data" air mode plus controller support; PipeWire probes for it and PipeWire's own quirk database can
disable it per-device [DOC]. `bridgectl status` must always print the negotiated codec — a silent
fall back to CVSD is the most likely cause of "it sounds like a 1990s phone".

### 6.4 Pairing, trust, identity

- Pairing is initiated **from the Pi** for the output device (`pair-output`) and **from the phone**
  for the Pixel (`pair-phone` puts the adapter into pairable+discoverable and registers the agent).
- Everything is keyed on **MAC address** stored in `config/bridge.toml` under `[devices.phone]` /
  `[devices.output]`. Never `hci0`-relative indices, never `card N`.
- `bridged` sets `Trusted=true` on both, so BlueZ accepts incoming reconnects unattended.
- Alias is set to a stable friendly name (`LarkBridge`) so it is identifiable in Android's UI.

### 6.5 Connection lifecycle FSM (per device)

```
        ┌──────────┐  paired & trusted   ┌─────────────┐
        │ UNKNOWN  ├────────────────────►│ DISCONNECTED│◄──────────────┐
        └──────────┘                     └──────┬──────┘               │
                                                │ Connect()            │ Disconnected signal
                                         ┌──────▼──────┐               │ (backoff 1,2,4,…,64 s
                                         │ CONNECTING  │               │  + jitter, cap 5 min)
                                         └──────┬──────┘               │
                            profile UUID up     │                      │
                                         ┌──────▼──────┐               │
                                         │  CONNECTED  ├───────────────┘
                                         └──────┬──────┘
                       MediaTransport1 active   │
                                         ┌──────▼──────┐
                                         │  STREAMING  │  ← the only state that counts as healthy
                                         └─────────────┘
```

Reconnect strategy: **the Pi is the initiator for the output device; the Pixel is the initiator for
itself.** Android reconnects to trusted headsets aggressively; fighting it with our own connect loop
causes collisions. So: `bridged` retries the *output* device with backoff, and for the *phone* it
only ensures the adapter is pairable/connectable and waits. If the phone hasn't reconnected in 60 s
and is known-present, one gentle `Connect()` attempt is allowed.

### 6.6 Controller recovery ladder

Applied by `bridged.health.recovery`, each rung rate-limited and logged, never more than once per
5 minutes per rung:

1. Reconnect the failing profile only (`ConnectProfile(uuid)`).
2. `Disconnect()` then `Connect()` the device.
3. `bluetoothctl power off; power on` (adapter down/up via D-Bus `Powered`).
4. `rfkill block bluetooth; sleep 1; rfkill unblock bluetooth`.
5. `systemctl restart bluetooth.service` (then re-run `bridge-btfw.service`).
6. Restart `hciuart`/serdev attach — the nuclear option short of reboot.
7. Give up, mark `DEGRADED`, emit a loud structured log event. **Never auto-reboot.**

### 6.7 Coexistence controls (Stage E variables)

Make each of these a switch so Stage E can measure them independently rather than shipping folklore:
- `dtoverlay=disable-wifi` (Ethernet + SSH for dev — recommended default during bracket testing)
- SBC bitpool ceiling / `bluez5.enable-sbc-xq=false`
- A2DP sink `session.suspend-timeout-seconds` and node latency (bigger buffer rides through SCO)
- eSCO vs SCO, mSBC vs CVSD
- `ControllerMode = bredr` (no LE advertising/scanning competing for slots)
- Physical: distance, antenna orientation, USB 3 devices nearby (USB 3 is a notorious 2.4 GHz noise
  source — the Pi 3 has none, but the phone's charger and the environment matter)

---

## 7. Pico architecture

### 7.1 USB Audio Class strategy (ADR-0005)

**Primary: UAC2. Fallback build: UAC1. Decide empirically at M9 on the actual Pixel 7a.**

The honest state of the evidence: AOSP's own documentation commits only to "limited USB Audio Class
1 (UAC1) support" in host mode, with PCM 16/24/32-bit and rates including 48 kHz [DOC]. Field
reports say Pixel 6+ handle UAC2 fine, and TinyUSB's mature, maintained example is `uac2_headset`
(UAC2); TinyUSB does also support UAC1 descriptors [DOC]. So UAC2 is the better-supported *firmware*
path and the less-documented *Android* path, and UAC1 is the reverse.

Resolution: build both from day one behind `-DBRIDGE_UAC=2|1` sharing all non-descriptor code, and
make "does the Pixel enumerate and route it" a **milestone gate (M9.3)** rather than an assumption.
Full-Speed bandwidth is not the deciding factor — 48 kHz S16 stereo out (192 B/frame) plus mono in
(96 B/frame) is trivial against the 1023 B/frame FS isochronous limit [DOC].

### 7.2 Descriptor topology (UAC2 primary)

```
Device: bDeviceClass=0xEF/0x02/0x01 (IAD), bcdUSB=0x0200, Full-Speed
        idVendor/idProduct: use a test VID/PID during development; document the choice in
        docs/architecture/pico-usb.md. iSerial derived from RP2040 unique flash ID (stable udev key).

IAD → AudioControl interface 0
  CS1  Clock Source        (internal, fixed 48 000 Hz, clock validity = valid)
  IT1  Input Terminal      type USB Streaming (0x0101), 2 ch (FL,FR)   ← host playback
  FU2  Feature Unit        volume + mute, source IT1
  OT3  Output Terminal     type Headphones (0x0302), source FU2
  IT4  Input Terminal      type Microphone (0x0201), 1 ch (FC)
  FU5  Feature Unit        volume + mute, source IT4
  OT6  Output Terminal     type USB Streaming (0x0101), source FU5

AudioStreaming interface 1  (OUT, host → device)
  alt 0: zero bandwidth
  alt 1: PCM S16LE, 2 ch, 48 kHz
         EP 0x01 ISO ASYNC OUT, wMaxPacketSize 192, bInterval 1
         EP 0x81 ISO FEEDBACK IN, 4 bytes (16.16), bInterval per spec

AudioStreaming interface 2  (IN, device → host)
  alt 0: zero bandwidth
  alt 1: PCM S16LE, 1 ch, 48 kHz
         EP 0x82 ISO ASYNC IN, wMaxPacketSize 98 (48+1 samples ×2 B), bInterval 1

Optional (build flag, off by default): CDC interface for diagnostics; HID consumer-control for
volume keys. Both are OFF in the default build so the device presents as the least surprising
possible headset to Android.
```

Deliberate choices: **mono microphone** (a headset mic is mono; halves IN bandwidth and matches what
Android expects), **stereo playback** (Android will happily downmix, and stereo is the least
surprising for a "headset"), **single 48 kHz rate** (no rate switching means no
`tud_audio_set_req` state churn — directly relevant to TinyUSB issue #1728, which is a state-machine
failure during repeated alt-setting/rate changes [DOC]).

### 7.3 TinyUSB reuse plan

Reuse `examples/device/uac2_headset` as the *structural* reference for descriptor macros and the
`tud_audio_*` callback set, but do **not** vendor it as-is. Known upstream problems to inherit
around:
- **#2838** — `uac2_headset` hangs the RP2040 on host-side interface/rate changes; fixed by **PR
  #1802**; the issue is closed [DOC]. Pin a TinyUSB revision that contains #1802, and record that
  revision in `third_party/NOTICE.md`.
- **#1728** — hard assert after prolonged playback / repeated `Set Interface` cycles; **still open**
  [DOC]. Mitigate by advertising exactly one alt-1 format and one sample rate, and by fuzzing
  alt-setting transitions in test `target/usb_loopback`.
- RP2040 isochronous: hardware max packet is **1023 B**, not 1024; double buffering is available for
  ISO at 128/256/512 B [DOC]. Our packets are ≤192 B, so use single-buffered ISO and avoid the
  double-buffer corner entirely. Errata-15 handling lives in TinyUSB's `dcd_rp2040.c`; don't
  reimplement it.

**PlatformIO-specific constraints (from ADR-0008).** Under the arduino-pico core, TinyUSB arrives
vendored inside `Adafruit_TinyUSB_Arduino` rather than as a submodule we pin, and arduino-pico's own
USB-audio support is weak ([arduino-pico#2707](https://github.com/earlephilhower/arduino-pico/issues/2707),
closed with no documented fix) [DOC]. Therefore:
- **M9.1 gate:** verify the bundled TinyUSB contains PR #1802 before writing any descriptor code.
  Record the core version and the TinyUSB SHA in `docs/experiments/E05`. If it does not, either bump
  the core or build via CMake with a pinned TinyUSB and treat PlatformIO as editor-only.
- Register our UAC descriptors through a **custom class driver** (`usbd_app_driver_get_cb`) with
  **statically defined** descriptor arrays. Do not adopt a library that synthesises descriptors at
  runtime — the descriptor bytes are our contract with Android and must be diffable in git.
- Build with `-DUSE_TINYUSB`; never `PIO_FRAMEWORK_ARDUINO_NO_USB`.
- `.pio` files are assembled offline: `make pio` runs `pioasm` and writes `i2s_slave.pio.h`, which is
  committed. CI regenerates and fails on any diff, so the header can never drift from its source.

### 7.4 Pi ↔ Pico transport selection

| Candidate | Verdict |
|---|---|
| **I2S/PCM** | **Chosen.** Both ends have dedicated hardware (Pi PCM peripheral; RP2040 PIO+DMA), it is inherently full-duplex with shared clocks, it is a *synchronous* transport so there is one audio clock instead of two, and it appears to Linux as a normal ALSA card — meaning PipeWire, not our code, handles it. |
| SPI | Rejected. Not clocked at the audio rate, so both ends need their own audio clock and you reintroduce the drift problem you were trying to avoid, plus framing/ordering has to be invented. Full-duplex SPI on the Pi is also awkward at fixed low rates. |
| PCM/TDM | Same peripheral as I2S, more slots than we need. Keep in reserve if we ever carry >2 channels (e.g. separate mic + AEC reference). |
| USB CDC (Pi host → Pico) | Rejected except as a **diagnostics side-channel**. It would consume the Pico's only USB port, which is the entire reason the Pico exists. |
| UART | Rejected. 48 kHz × 16 bit × 2 directions ≈ 1.5 Mbit/s of raw payload with no framing or clocking; possible but strictly worse than I2S in every dimension. |

### 7.5 Clocking (ADR-0004): the Pi is I2S master

Decision: **Pi = BCLK + LRCLK master; Pico = I2S slave via PIO.**

Reasons:
1. `bcm2835-i2s` in **master** mode is the path every Raspberry Pi audio HAT uses; slave mode is far
   less exercised. Take the well-trodden side on the platform that is harder to debug.
2. The Pi is where the resampler lives. PipeWire already adaptively resamples the Lark's USB clock
   into the graph clock; making the I2S card the graph's driver means everything converges on one
   clock with one adaptive resampler, on the CPU that has FPU and headroom.
3. RP2040 as I2S *master* would need its PIO fractional divider to synthesise 3.072 MHz from a
   125 MHz system clock: 125e6/3.072e6 = 40.6901, and the PIO divider's 8 fractional bits give
   40 + 176/256 = 40.6875 → ≈48 003 Hz, about **+65 ppm** error, which is fine in isolation but is
   free to avoid. [INF]
4. As a slave, the Pico's PIO needs roughly ~8 system clocks per BCLK edge; at 3.072 MHz BCLK and
   125 MHz sysclk there are ~40, so timing is comfortable [INF], and proven bidirectional slave PIO
   implementations exist to model on (`i2s_bidi_slave`) [DOC].

Consequence: the Pico has two clock domains — I2S (from the Pi) and USB SOF (from the Pixel) — and
must reconcile them:
- **USB IN (mic)**: asynchronous source. Send 48 samples/frame nominally, 47 or 49 when the RX ring
  drifts past thresholds. Hosts expect this from async IN endpoints.
- **USB OUT (playback)**: asynchronous sink with an **explicit feedback endpoint** reporting the
  measured I2S rate in 16.16 format, steered by a slow PI controller on TX ring fill. If Android
  ignores or mishandles feedback [TEST], fall back to sample drop/duplicate at zero crossings, at
  most one sample per ~10 ms, which is inaudible on speech.

### 7.6 Pi-side I2S soundcard

`bcm2835-i2s` needs a machine driver and a codec node to appear as an ALSA card. Plan:

- **M10 step 1 (fast path):** use the shipped `dtoverlay=googlevoicehat-soundcard`, which is a
  no-external-codec, 48 kHz, full-duplex I2S card with the Pi as master [DOC for the overlay's
  existence; TEST for the duplex claim]. Zero DT authoring, immediate Stage G progress.
- **M10 step 2 (product):** replace it with a repo-owned overlay `bridge-i2s-duplex` built from
  `simple-audio-card` + a dummy duplex codec, so the card name, channel count and slot width are ours
  and stable across OS updates.
- **Fallback if duplex fails:** the documented failure mode is "only the direction started first
  works." If that reproduces, options in order are (a) open both substreams from a single process
  with `snd_pcm_link()`, which PipeWire can be made to do by exposing the card as one duplex device;
  (b) drop to half-duplex-alternating, which is unacceptable for calls; (c) two separate I2S
  peripherals — not available on a Pi 3. Record the result in `docs/experiments/E06`.

### 7.7 Pico buffering, DMA, and failure handling

```
I2S RX ──DMA──► [rx_dma_buf A|B]  1 ms each (48 frames × 2 ch × 32 bit)
                       │ IRQ on half-complete
                       ▼
              rx_ring (SPSC, 8 ms)  ───► USB ISO IN callback
USB ISO OUT ─► tx_ring (SPSC, 8 ms)  ───► [tx_dma_buf A|B] ──DMA──► I2S TX
```
- Two chained DMA channels per direction (ping-pong), IRQ on each half.
- Rings are power-of-two, single-producer/single-consumer, index-based, no locks. **Host-unit-tested**
  in `pico/test/host/` — this is the one piece of firmware logic that can and must be tested off-target.
- **Underrun** (ring empty at DMA time): emit silence, increment counter, LED code, do not stall.
- **Overflow**: drop oldest frame, increment counter.
- **I2S clock loss** (no LRCLK edge for >5 ms): mark the link down, feed silence to USB IN, keep USB
  alive (so Android doesn't tear down the device), assert an LED code, and recover automatically when
  clocks return. Never hang.
- **Hardware watchdog** armed at 1 s, kicked from the main loop; a wedged firmware reboots itself
  into a known state rather than presenting a zombie USB device.
- Core split: **core0 = TinyUSB**, **core1 = I2S DMA servicing + format conversion**. Rings are the
  only shared state.

### 7.8 Firmware diagnostics

- LED patterns: slow blink = idle/no USB; solid = streaming both ways; double-blink = USB only;
  triple-blink = I2S clock lost; fast blink = underrun storm.
- Optional CDC console (`-DBRIDGE_DIAG_CDC=ON`, off in release) printing a one-line stats record
  every second: ring fills, under/overruns, feedback value, measured I2S rate, USB alt settings.
  `bridged` can read this when the Pico's diag port is connected to the *Pi* — which is only possible
  in bench setups, so it is a development aid, not a product feature.

---

## 8. Implementation phases

Format: **objective → files → test → pass → fail → depends on**. Spikes come first and are allowed
to be throwaway; their deliverable is a document in `docs/experiments/`, not production code.

### Spike S1 — Does SCO reach the host on this controller? (do this before anything else)

- **Objective:** determine whether SCO packets flow over HCI on the BCM43438 with and without the
  vendor PCM routing command.
- **Files:** `docs/experiments/E01-sco-over-hci.md`, `tests/stage-b-hfp/sco_probe.sh`
- **Test:** `btmon -w e01.btsnoop` running; pair the Pixel; place a call; observe whether SCO
  Connection Complete is followed by SCO data packets on the HCI transport. Repeat after
  `hcitool cmd 0x3f 0x1c 01 02 00 01 01` and after a reboot with the DT overlay.
- **Pass:** non-empty bidirectional SCO traffic visible in btmon; `arecord` from the HFP source
  yields non-silent audio.
- **Fail:** SCO Connection Complete with zero data packets in either direction after both fixes →
  Mode 1 is not achievable on the onboard radio; escalate to §15 open question 1 before proceeding.
- **Depends on:** nothing. **Timebox: 1 day.**

### Spike S2 — Can the Pi be an HFP *Hands-Free* unit for the Pixel?

- **Objective:** confirm PipeWire's native backend registers UUID `0000111e` and completes service
  level connection with an Android AG; confirm no WirePlumber instability in the HF path.
- **Files:** `docs/experiments/E02-hfp-hf-role.md`, a scratch WirePlumber conf
- **Test:** set `bluez5.roles = [ hfp_ag hsp_ag a2dp_sink ]`; `sdptool browse local` shows Handsfree
  (not Handsfree AG); pair from the Pixel; verify Android's Bluetooth settings shows "Phone calls"
  available; check `wpctl status` for `bluez_input/bluez_output ... handsfree-head-unit` nodes;
  run 30 min with repeated call start/stop watching for WirePlumber restarts.
- **Pass:** nodes appear, SLC completes, zero WirePlumber crashes in 30 min.
- **Fail:** crash loop → try `hsp_ag` only (HSP is simpler, CVSD-only, and the Gentoo report suggests
  it is the more stable path [DOC]); record the quality cost.
- **Depends on:** S1. **Timebox: 1 day.**

### Spike S3 — Coexistence smoke test

- **Objective:** an early, crude read on whether HFP+A2DP on one radio is viable, before building
  anything on top of it.
- **Files:** `docs/experiments/E03-hfp-a2dp-coexistence.md`
- **Test:** A2DP streaming a 1 kHz tone to headphones; start a call on the Pixel; 10 minutes;
  `tools/audio/glitch_detect.py` on a recording made *at the headphones* (phone recording the
  headphone output acoustically is fine for a smoke test). Repeat with Wi-Fi disabled.
- **Pass (proceed with Mode 1 as primary):** <1 dropout/min, none longer than 50 ms.
- **Partial (proceed but make Mode 1W the default):** 1–10 dropouts/min.
- **Fail (Mode 1W becomes the product; Mode 1 becomes an experiment):** >10 dropouts/min or SCO
  disconnects.
- **Depends on:** S1, S2. **Timebox: 1 day.**

### M0 — Repository skeleton

- **Objective:** the tree in §3 exists with real README, CI, lint, and a `Makefile` that runs.
- **Files:** everything in §3 as stubs; `.github/workflows/*`; `scripts/lib/common.sh`;
  `docs/architecture/decisions/ADR-000{1..7}.md` written from §1–§7.
- **Test:** `make lint` and `make test-host` pass on a clean checkout with no hardware.
- **Pass:** green CI. **Fail:** n/a. **Depends on:** nothing (can run parallel to spikes).

### M1 — Lark validation (Stage A)

- **Objective:** know exactly what the Lark A1 is.
- **Files:** `tests/stage-a-lark/run.sh`, `tools/audio/level_meter.py`, `docs/hardware/lark-a1.md`,
  `pi/udev/90-bridge-lark.rules`
- **Test:** `lsusb -v`, `cat /proc/asound/cards`, `arecord -D hw:CARD=... --dump-hw-params`,
  60 s capture at each supported rate, analyse for DC offset, clipping, noise floor, channel content.
- **Pass:** a documented table of supported rate/format/channels; a clean 60 s WAV; a udev rule that
  gives the device a stable name across replug and reboot.
- **Fail:** device is not 48 kHz-capable → the whole graph rate decision (§5.1) is revisited.
- **Depends on:** M0.

### M2 — Headless appliance audio session

- **Objective:** PipeWire + WirePlumber run reliably with no desktop, no seat, on boot.
- **Files:** `scripts/bootstrap/20-user.sh`, `40-audio.sh`; `pi/systemd/user/*`;
  `pi/pipewire/pipewire.conf.d/10-bridge-clock.conf`; `pi/limits/95-bridge-rtprio.conf`
- **Test:** reboot 5×; `systemctl --user -M bridge@ status pipewire wireplumber`; `wpctl status`
  over SSH with no login session; 1 h idle then verify the Lark still captures.
- **Pass:** services active after every reboot; `wpctl status` works over plain SSH; no suspend of
  the Lark node.
- **Fail:** rtkit/permission failures → fall back to explicit `limits.d` rtprio rather than
  system-wide PipeWire (ADR-0006 says: never system-wide).
- **Depends on:** M0, M1.

### M3 — A2DP Source (Stage D)

- **Objective:** the Pi reliably plays to the chosen headphones/car at 48 kHz SBC.
- **Files:** `pi/wireplumber/wireplumber.conf.d/50-bridge-bluez.conf`, `51-*`;
  `pi/bluez/main.conf.d/10-bridge.conf`; `scripts/pair-output.sh`; `tests/stage-d-a2dp/`
- **Test:** pair; play a 10 min tone; `glitch_detect.py`; verify negotiated codec/rate via
  `pw-dump`; disconnect/reconnect 10×; power-cycle the headphones 5×.
- **Pass:** zero dropouts in 10 min; auto-reconnect within 15 s every time.
- **Fail:** forced 44.1 kHz → accept + log resample; codec instability → pin `bluez5.codecs=[sbc]`.
- **Depends on:** M2.

### M4 — HFP Hands-Free toward the Pixel (Stage B)

- **Objective:** productionise S1+S2.
- **Files:** `pi/systemd/system/bridge-btfw.service`; `pi/boot/overlays/bridge-bt-sco-overlay.dts`;
  `scripts/pair-phone.sh`; `scripts/bootstrap/50-bluetooth.sh`; `tests/stage-b-hfp/`
- **Test:** synthetic tone → HFP sink, recorded on the far end of a real call; Pixel → HFP source →
  WAV; verify codec is mSBC in `btmon` and in `bridgectl status`.
- **Pass:** both directions carry intelligible audio on a real cellular call; mSBC negotiated;
  survives 5 call start/stop cycles.
- **Fail:** CVSD only → document and continue (quality hit, not a blocker). No SCO → S1 fail path.
- **Depends on:** S1, S2, M2.

### M5 — Lark → HFP uplink (Stage C)

- **Objective:** the actual product feature, minimum version.
- **Files:** `pi/pipewire/pipewire.conf.d/20-bridge-endpoints.conf` (the `bridge.mic` loopback)
- **Test:** call the Pi's Pixel from a second phone; speak into the Lark; record the far end;
  compare intelligibility and level against the Pixel's built-in mic.
- **Pass:** far end hears the Lark clearly, at comparable level, with no phone-mic bleed.
- **Fail:** level too low → node volume, not device AGC. Phone mic still used → Android is not
  honouring the HFP mic; investigate profile/CoD before anything else.
- **Depends on:** M4.

### M6 — Simultaneous single-radio operation (Stage E) — **the gate**

- **Objective:** determine, with evidence, whether Mode 1 or Mode 1W is the product default.
- **Files:** `tests/stage-e-concurrent/{run.sh,expect.yaml}`; `tools/bt/btmon_capture.sh`;
  `docs/experiments/E03` (upgraded from spike to full report)
- **Test:** the §9 matrix rows E1–E6, including a 60-minute continuous call with A2DP active, with
  and without Wi-Fi, at 1 m and 5 m, with and without RF congestion.
- **Pass (Mode 1 default):** 60 min with <1 dropout/min and no SCO disconnect.
- **Partial (Mode 1W default, Mode 1 supported):** works but degrades.
- **Fail (Mode 1W only):** write the measured limitation up in `docs/experiments/E03` with btmon
  evidence and change the default in `config/bridge.toml.example`. **Do not add a BT dongle.**
- **Depends on:** M3, M5.

### M7 — `bridged` + `bridgectl` + modes

- **Objective:** the system becomes an appliance rather than a collection of configs.
- **Files:** all of `pi/bridged/`; `pi/systemd/user/bridged.service`; `config/bridge.toml.example`
  and its schema; `pi/udev/*`
- **Test:** `pytest` on recorded fixtures; on hardware, `bridgectl mode bluetooth|bluetooth-wired`
  round-trips; `bridgectl status --json` validates against the published schema; kill `bridged` and
  confirm audio keeps flowing (it must — the loopbacks are declarative).
- **Pass:** every §"Diagnostics" field in the brief is populated by `bridgectl status`; mode switch
  <3 s; daemon death does not interrupt an in-progress call.
- **Fail:** if killing `bridged` breaks audio, the design has leaked imperative link management —
  fix that, don't add supervision.
- **Depends on:** M6.

### M8 — Reliability and recovery, Mode 1 (Stage I subset)

- **Objective:** survive everything a user will do.
- **Files:** `pi/bridged/src/bridged/health/*`; `scripts/bt-reset.sh`; `tests/stage-i-recovery/`
- **Test:** scripted unplug/replug of Lark ×20; phone BT off/on ×20; headphone power-cycle ×20;
  `systemctl restart bluetooth` ×10; `systemctl --user restart pipewire` ×10; reboot ×10; airplane
  mode ×10; out-of-range walk-away/return ×10.
- **Pass:** ≥95 % of events auto-recover within 30 s with no human action; 100 % within 60 s or a
  clear `DEGRADED` status with an actionable message.
- **Fail:** any wedge requiring reboot is a defect, not a limitation.
- **Depends on:** M7.

**— Mode 1 is shippable here. Mode 2 follows. —**

### M9 — Pico as a standalone USB headset (Stage F)

- **9.1 Skeleton + toolchain gate:** PlatformIO build (primary) *and* CMake build (CI) of the same
  sources; blink; CDC diag. **Gate: confirm the arduino-pico core's bundled TinyUSB contains PR
  #1802** (`grep` the vendored `audio_device.c`, record core version + TinyUSB SHA in E05). If absent,
  bump the core or demote PlatformIO to editor-only and build via CMake. *Files:*
  `pico/platformio.ini`, `pico/pio_main.cpp`, `pico/CMakeLists.txt`,
  `pico/src/{main.c,board_config.h,led_status.c}`, `pico/tools/flash.sh`.
- **9.2 Tone → USB IN:** `pico/test/target/tone_only`; verify on a Linux host with `arecord`; check
  frequency accuracy (proves the sample clock) and continuity.
- **9.3 Android enumeration gate:** plug into the Pixel 7a; confirm it appears as an audio device;
  record with a voice recorder app; play through it. **Try UAC2 first, then UAC1.** Record the result
  in `docs/experiments/E05`. *This gate decides ADR-0005.*
- **9.4 Full duplex:** `usb_loopback` firmware (USB OUT → USB IN) exercising both endpoints and the
  feedback EP; run 2 h with repeated app switching to smoke out TinyUSB #1728.
- **Pass:** stable enumeration on Linux *and* the Pixel; 2 h loopback with no hang, no assert.
- **Fail:** UAC2 refused by Android → build UAC1 and repeat; both refused → §15 open question.
- **Depends on:** M0. **Independent of the whole Bluetooth track — can be done in parallel.**

### M10 — Pi ↔ Pico I2S (Stage G)

- **Objective:** a working full-duplex PCM link with no Android involved.
- **Files:** `pico/i2s/*`; `pi/boot/config.txt.fragment`; `pi/boot/overlays/bridge-i2s-duplex*`;
  `pi/pipewire/pipewire.conf.d/30-bridge-alsa.conf`; `docs/hardware/wiring-pi-pico.md`;
  `tests/stage-g-pi-pico/`
- **Test:** wire per §12. Pico runs `i2s_loopback`. On the Pi, `speaker-test` into the I2S playback
  device and `arecord` from the capture device simultaneously; verify the loopback returns the exact
  signal; run 1 h checking XRUNs on both sides and the Pico's underrun counters; scope/logic-analyse
  BCLK/LRCLK if anything is odd.
- **Pass:** bit-exact (or ±1 LSB after S32↔S16 conversion) loopback; zero XRUNs in 1 h; simultaneous
  playback+capture confirmed working (this is the L9 risk).
- **Fail:** only one direction works → §7.6 fallback ladder; document in E06.
- **Depends on:** M9.1, M9.2.

### M11 — Full USB bridge (Stage H)

- **Objective:** Mode 2 end to end.
- **Files:** `pico/src/audio_pipeline.c`, `rate_control.c`; `pi/bridged/src/bridged/modes/usb.py`
- **Test:** Lark → Pi → Pico → Pixel recorded in a voice recorder app; Pixel playback → Pico → Pi →
  chosen sink; 60 min continuous with drift monitoring (ring fill trend must be flat, not ramping).
- **Pass:** 60 min with no dropouts and bounded ring fill; measured end-to-end uplink latency
  <60 ms.
- **Fail:** ring fill ramps → feedback endpoint not working; enable the drop/dup fallback and
  document.
- **Depends on:** M9, M10, M7.

### M12 — Android application validation

- **Objective:** the part that cannot be inferred.
- **Files:** `tests/stage-h-full-usb/android-matrix.md`, `docs/experiments/E05`
- **Test:** for each of {Mode 1, Mode 1W, Mode 2} × {native cellular call, Discord, WhatsApp or
  Signal, Google Meet, a plain voice recorder}: does Android offer the device? Does it use it for
  the mic? For playback? What does the in-call audio picker show? Does it survive an app switch, a
  screen lock, a second incoming call?
- **Pass:** native call + Discord work in at least one mode; all results tabulated, including the
  failures.
- **Fail:** none — this milestone's deliverable is *knowledge*. A documented "Discord ignores USB
  headsets for input" is a successful outcome of this milestone.
- **Depends on:** M8, M11.

### M13 — Provisioning, docs, release

- **Objective:** a fresh SD card + this repo → a working appliance.
- **Files:** `scripts/install.sh` + all bootstrap fragments; `scripts/uninstall.sh`;
  `docs/operations/*`; `CHANGELOG.md`
- **Test:** flash a fresh Raspberry Pi OS image, follow `docs/operations/install.md` verbatim on a
  second Pi 3B if available, or after a full re-image. Time it. Count the manual steps.
- **Pass:** ≤2 manual interventions beyond `install.sh` (expected: pairing confirmation on the phone,
  and Pico BOOTSEL flashing), and `70-verify.sh` exits 0.
- **Depends on:** M8 (Mode 1 track) and M11 (Mode 2 track).

---

## 9. Test matrix

`tests/run-stage.sh <ID>` runs a row; each emits `result.json`. Duration column is the *acceptance*
run, not the smoke run.

| ID | Scenario | Mode | Duration | Key metrics | Pass criteria |
|---|---|---|---|---|---|
| A1 | Lark enumeration + formats | — | 5 min | rates, formats, channels | Documented; 48 kHz available |
| A2 | Lark clean capture | — | 60 s | noise floor, DC, clipping | No clipping; noise floor < −60 dBFS |
| A3 | Lark replug stability | — | 20 cycles | node reappears, name stable | 20/20, udev name unchanged |
| B1 | HFP HF SLC to Pixel | 1 | 5 min | UUID 111e registered, SLC, codec | SLC completes; mSBC or documented CVSD |
| B2 | Tone → HFP uplink | 1 | 5 min | far-end recording | Intelligible, correct level |
| B3 | HFP downlink → file | 1 | 5 min | WAV analysis | Non-silent, no gaps > 20 ms |
| B4 | Call start/stop cycles | 1 | 20 cycles | SCO setup success rate | ≥19/20 without manual action |
| C1 | Lark → Pixel call mic | 1 | 10 min | far-end recording | Lark audio, not phone mic |
| C2 | Lark → mic, 60 min call | 1 | 60 min | dropouts, level drift | 0 dropouts > 50 ms |
| D1 | A2DP source, tone | 1 | 10 min | dropouts, codec, rate | 0 dropouts; SBC @48 kHz |
| D2 | A2DP reconnect | 1 | 10 cycles | time to reconnect | ≤15 s, 10/10 |
| **E1** | **HFP + A2DP, Wi-Fi off, 1 m** | **1** | **60 min** | **dropouts/min, SCO drops** | **<1/min, 0 SCO drops** |
| E2 | HFP + A2DP, Wi-Fi on, 1 m | 1 | 30 min | same | Compare to E1; quantify delta |
| E3 | HFP + A2DP, 5 m + wall | 1 | 30 min | same | Degradation documented |
| E4 | HFP + A2DP + congested 2.4 GHz | 1 | 30 min | same | Degradation documented |
| E5 | Lark + HFP + A2DP (full Mode 1) | 1 | 60 min | end-to-end intelligibility | Usable call throughout |
| E6 | Control: Lark + HFP + wired out | 1W | 60 min | same | Baseline for E5 comparison |
| F1 | Pico UAC on Linux host | 2 | 30 min | enumeration, arecord/aplay | Both directions work |
| F2 | Pico UAC on Pixel 7a | 2 | 30 min | Android sees device | Enumerates; recorder app captures |
| F3 | Pico USB loopback soak | 2 | 2 h | asserts, hangs | Zero |
| G1 | Pi↔Pico I2S loopback | 2 | 60 min | bit accuracy, XRUNs | Exact; 0 XRUNs |
| G2 | I2S simultaneous duplex | 2 | 30 min | both substreams live | Both work concurrently |
| G3 | I2S drift | 2 | 60 min | Pico ring fill trend | Flat, bounded |
| H1 | Lark → Pico → Pixel | 2 | 30 min | Android recording | Lark audio present |
| H2 | Pixel → Pico → Pi → sink | 2 | 30 min | WAV analysis | Clean |
| H3 | Full Mode 2, call | 2 | 60 min | end-to-end | Usable |
| I1 | Lark unplug/replug | 1,2 | 20 cycles | recovery time | ≤30 s, no restart needed |
| I2 | Phone BT off/on | 1 | 20 cycles | reconnect | ≤30 s auto |
| I3 | Headphones power-cycle | 1 | 20 cycles | reconnect | ≤30 s auto |
| I4 | Pico unplug/replug | 2 | 20 cycles | re-enumeration | ≤15 s auto |
| I5 | `restart bluetooth` | 1 | 10 cycles | full recovery | 10/10 |
| I6 | `restart pipewire` | 1,2 | 10 cycles | links restored | 10/10, no `pw-link` needed |
| I7 | Reboot | 1,2 | 10 cycles | cold start to ready | ≤90 s, 10/10 |
| I8 | Out of range and back | 1 | 10 cycles | reconnect | ≤60 s auto |
| I9 | HFP codec renegotiation | 1 | 10 cycles | codec change handled | No graph teardown |
| J1 | Native cellular call | 1,1W,2 | per call | app behaviour | Tabulated |
| J2 | Discord | 1,1W,2 | per call | app behaviour | Tabulated |
| J3 | WhatsApp/Signal/Meet | 1,1W,2 | per call | app behaviour | Tabulated |
| J4 | 60-min endurance, real call | best mode | 60 min | everything | No manual intervention |

---

## 10. Risk register

Score = probability × impact, both 1–5.

| # | Risk | P | I | Score | Mitigation | Resolving test |
|---|---|---|---|---|---|---|
| R1 | **Single radio cannot sustain HFP/SCO + A2DP acceptably** | 4 | 4 | **16** | Mode 1W as a first-class mode; Wi-Fi off; SBC bitpool cap; larger A2DP buffer; document as measured limitation | S3, E1–E6 |
| R2 | **SCO never reaches the host on BCM43438** (PCM routing) | 3 | 5 | **15** | DT `brcm,bt-pcm-int-params`; runtime vendor HCI command; both shipped | **S1** |
| R3 | Android won't route calls to a USB headset | 3 | 4 | 12 | Mode 1/1W do not depend on this; Mode 2 is the secondary path by design | M12/J1–J3 |
| R4 | WirePlumber/PipeWire instability in the HFP HF path | 3 | 4 | 12 | Pin versions; HSP fallback; systemd restart + `bridged` health; loopbacks survive restarts | S2, I6 |
| R5 | `bcm2835-i2s` full duplex unreliable | 3 | 3 | 9 | googlevoicehat overlay first, custom overlay second, `snd_pcm_link` third | G1, G2, E06 |
| R6 | TinyUSB UAC instability on RP2040 (#1728) | 3 | 3 | 9 | Pin a revision containing PR #1802; single rate + single alt setting; watchdog; 2 h soak | F3 |
| R7 | USB↔I2S drift not absorbed (feedback EP ignored by Android) | 3 | 3 | 9 | PI-controlled feedback + drop/dup fallback; monitor ring fill trend | G3, H3 |
| R8 | HFP negotiates CVSD (8 kHz) not mSBC | 3 | 2 | 6 | Enable mSBC; report codec prominently; accept as a documented quality ceiling | B1 |
| R9 | Lark A1 is not 48 kHz / has internal AGC that fights us | 2 | 3 | 6 | Measure first (M1); adapt graph rate if needed | A1, A2 |
| R10 | A2DP latency makes conversation awkward even when stable | 4 | 2 | 8 | Mode 1W; document per-headphone latency; prefer low-latency sinks | E5 vs E6 |
| R11 | Pi 3 CPU/thermal limits under BT + resampling + I2S | 2 | 3 | 6 | Measure headroom each stage; `resample.quality` tuning; quantum tuning | all soaks |
| R12 | Stale BlueZ pairing state after firmware/OS updates | 3 | 2 | 6 | `bridgectl devices --forget`; documented re-pair procedure; recovery ladder | I5 |
| R13 | Powering the Pico wrongly damages something | 2 | 4 | 8 | Schottky-into-VSYS only; explicit "never do this" list in `docs/hardware/power.md`; verify before first plug | M10 pre-check |
| R14 | Provisioning drifts from reality as Raspberry Pi OS updates | 3 | 2 | 6 | Pin the OS image in docs; `00-preflight.sh` refuses unknown kernel/OS combos | M13 |
| R15 | **arduino-pico/Adafruit_TinyUSB fights custom UAC descriptors** | 3 | 3 | 9 | Arduino-API-free sources + dual build front-ends (ADR-0008); static descriptors via a custom class driver; CMake escape hatch is always green in CI | M9.1 gate, F1 |
| R16 | Committed `i2s_slave.pio.h` drifts from `i2s_slave.pio` | 2 | 3 | 6 | `make pio` + CI diff check; header carries a "generated, do not edit" banner | CI `pico.yml` |

R1 and R2 are the two that can change the shape of the project. Both are resolved in the first two
days by spikes S1 and S3, before any product code exists. That ordering is the main reason this plan
is safe to execute mechanically.

---

## 11. Dependencies

**Raspberry Pi OS packages** (pin the versions from the target image in
`scripts/bootstrap/10-packages.sh`):
`pipewire`, `pipewire-audio`, `pipewire-alsa`, `libspa-0.2-bluetooth`, `wireplumber`,
`pipewire-bin` (for `pw-cli`/`pw-dump`/`pw-link`/`wpctl`), `bluez`, `bluez-tools`, `alsa-utils`,
`python3`, `python3-venv`, `python3-pip`, `git`, `rfkill`, `usbutils`, `device-tree-compiler`,
`build-essential`, `cmake`, `gcc-arm-none-eabi`, `libnewlib-arm-none-eabi`, `libstdc++-arm-none-eabi-newlib`,
`picotool`, `sox`, `python3-numpy`, `python3-scipy`, `jq`, `socat`.
**Explicitly not installed / removed if present:** `ofono`, `hsphfpd`, `pulseaudio` — an unconfigured
oFono is a documented cause of HFP silently failing [DOC].

**Python** (`pi/bridged/pyproject.toml`, installed into a venv owned by the `bridge` user):
`dbus-fast`, `click`, `rich`, `jsonschema`, `tomli-w`; dev: `pytest`, `pytest-asyncio`, `ruff`,
`black`, `mypy`.

**Pico — primary toolchain (PlatformIO, ADR-0008):** VS Code + PlatformIO IDE extension.
`platform = https://github.com/maxgerhardt/platform-raspberrypi.git`, `board = pico`,
`framework = arduino`, `board_build.core = earlephilhower`, `build_flags = -DUSE_TINYUSB` [DOC].
PlatformIO fetches the toolchain, core and Adafruit_TinyUSB itself — no manual SDK install.
On Windows, **enable Win32 NTFS long paths** or the arduino-pico package install fails [DOC].

**Pico — reference toolchain (CMake, CI + escape hatch):** `pico-sdk` ≥ 2.1 (submodule at
`pico/extern/pico-sdk`, pinned by commit), TinyUSB pinned to a revision containing PR #1802,
`picotool`, CMake ≥ 3.13, `arm-none-eabi-gcc`. Also supplies `pioasm` for `make pio`.
Host unit tests: Unity (vendored, small) — built with the host compiler, no MCU needed.

**Rejected:** `wizio-pico` (`framework = baremetal`). It is the only PlatformIO route to a raw
pico-sdk build, but it bundles **pico-sdk 1.4.0** and its author has publicly stepped back from
maintenance [DOC]. Incompatible with the TinyUSB revision this project requires.

**Kernel modules / features:** `snd_usb_audio`, `snd_soc_bcm2835_i2s`, `snd_soc_simple_card`,
`bluetooth`, `btbcm`, `hci_uart`, `bnep` (not needed — can be blacklisted), `dwc_otg`.
Verify `CONFIG_BT_HCIUART_BCM` and `CONFIG_BT_HCIUART_SERDEV` are enabled in the running kernel.

**Raspberry Pi configuration** (`/boot/firmware/config.txt`, merged idempotently):
```
dtparam=audio=off                 # kill the on-board PWM audio unless Mode 1W uses the jack
dtoverlay=disable-wifi            # dev default; a config toggle, measured in Stage E
dtoverlay=bridge-bt-sco           # our overlay: brcm,bt-pcm-int-params
dtoverlay=googlevoicehat-soundcard   # M10 step 1; replaced by bridge-i2s-duplex in M10 step 2
# NOT set: dtoverlay=disable-bt   # we need the radio
```
Note `dtparam=audio=off` and Mode 1W-via-jack are mutually exclusive; the installer picks based on
`--mode`. A USB DAC is the recommended Mode 1W output anyway.

**systemd:** user-level `pipewire`, `pipewire-pulse` (optional), `wireplumber`, `bridged`, all under
a `bridge.target`; system-level `bluetooth`, `bridge-btfw`, `bridge-boot`. `loginctl enable-linger
bridge` is what makes the user session exist without a login (ADR-0006).

---

## 12. Hardware wiring

Enough information exists to fix the pinout now. The one thing that could move it is the PIO
program's pin-adjacency requirement (a PIO state machine's `in`/`out`/`side-set` pin groups must be
contiguous), which is settled the moment `i2s_slave.pio` is written in M10. The table below is chosen
to already satisfy the common constraint pattern (DIN followed by the two clock pins), so it should
survive. **Freeze it at M10 and record any change in `docs/hardware/wiring-pi-pico.md`.**

### 12.1 Signal wiring

| Signal | Direction | Pi 3B BCM | Pi header pin | Pico GPIO | Pico phys pin |
|---|---|---|---|---|---|
| BCLK (bit clock) | Pi → Pico | GPIO18 / PCM_CLK | 12 | GP7 | 10 |
| LRCLK (word select) | Pi → Pico | GPIO19 / PCM_FS | 35 | GP8 | 11 |
| PCM data, mic path | Pi → Pico | GPIO21 / PCM_DOUT | 40 | GP6 (DIN) | 9 |
| PCM data, playback path | Pico → Pi | GPIO20 / PCM_DIN | 38 | GP9 (DOUT) | 12 |
| Ground (mandatory) | — | GND | 39 | GND | 13 |
| Pico reset (optional) | Pi → Pico | GPIO26 | 37 | RUN | 30 |
| Pico "USB host present" (optional) | Pico → Pi | GPIO23 | 16 | GP15 | 20 |

Notes:
- Both devices are **3.3 V CMOS**; connect directly, no level shifting. RP2040 GPIO is **not** 5 V
  tolerant, and neither is the Pi's — never route anything 5 V to these pins.
- Ground must be common. Use at least one ground wire; two (pins 39 and 6 on the Pi to two Pico GND
  pins) is better for clock integrity.
- Keep wires **under ~15 cm**; ideally use a short ribbon with ground interleaved. BCLK is 3.072 MHz,
  which is undemanding, but jumper-wire crosstalk at that rate still shows up as jitter.
- Optional 100 Ω series resistors at the *source* end of BCLK, LRCLK and each data line if you see
  ringing on a scope. Not needed for short wires.
- The GPIO26→RUN link lets the Pi reset a wedged Pico without a human; the GP15→GPIO23 link lets the
  Pi know the Pico has USB VBUS from the phone. Both are cheap reliability wins; both are optional
  and the firmware/daemon must work without them.

### 12.2 Power (get this right before first power-on)

**Recommended:** power the Pico from the Pi, not from the phone.

```
  Pi 5V (header pin 2 or 4) ──►|── Pico VSYS (pin 39)
                          Schottky (1N5817 / SS14)
  Pi GND (pin 39/6)  ───────────  Pico GND (pin 38/13)
```

- The Pico already has an internal Schottky from VBUS to VSYS, so ORing a second source into VSYS
  through your own Schottky is the vendor-documented method. Whichever source is higher wins.
- **Never connect Pi 5 V to Pico VBUS (pin 40)** while a USB host is attached — that back-feeds the
  phone.
- Why Pi-powered: the Pico stays alive and its I2S link stays up when the phone is unplugged, which
  makes plug/unplug behaviour deterministic, and it doesn't drain the Pixel's battery.
- The Pi 3B needs a **≥2.5 A 5 V** supply; the Lark receiver, a USB DAC, and the Pico are all on its
  budget. Under-powering a Pi 3 produces exactly the kind of intermittent USB and audio failures that
  will waste days. Check `vcgencmd get_throttled` in `70-verify.sh`.

### 12.3 Cabling

- Pixel 7a (USB-C, host) ↔ Pico (micro-B, device): a **USB-C male to micro-B male** cable, or a
  USB-C-to-A OTG adapter plus a standard A-to-micro-B cable. The Pixel supplies VBUS as host; the
  Pico ignores it for power thanks to §12.2.
- Lark A1 USB-C receiver → Pi USB-A: USB-C to USB-A cable. Plug it into a port not shared with the
  Ethernet-heavy traffic if you see issues (all four Pi 3B ports share one LAN9514 hub and one
  480 Mbit/s SoC USB link — that is also why the Pi 3B can never be a USB gadget [DOC], which is the
  entire reason the Pico exists).

---

## 13. Fresh-system deployment procedure

Target experience:

```bash
# 1. Flash Raspberry Pi OS (64-bit, Trixie) with SSH + Ethernet preconfigured. Boot. SSH in.
git clone https://github.com/<you>/rpi-lark-bridge.git
cd rpi-lark-bridge
sudo ./scripts/install.sh --mode bluetooth-wired
sudo reboot
```
```bash
# 2. After reboot: pair, one interaction on the phone.
bridgectl pair-phone          # then accept the pairing prompt on the Pixel
bridgectl pair-output         # put headphones in pairing mode when prompted
bridgectl status
```
```bash
# 3. Mode 2 only: build and flash the Pico.
#    From a dev machine with VS Code + PlatformIO (the primary workflow):
#        open pico/ as the PlatformIO project, then Build and Upload.
#    Or headless, from the Pi or any machine with pio installed:
pio run -d pico -e pico -t upload      # one manual BOOTSEL press unless GPIO26→RUN is wired
bridgectl mode usb
```

`install.sh` responsibilities and properties:
- `--mode {bluetooth,bluetooth-wired,usb,all}`, `--dry-run`, `--skip N,M`, `--yes`, `--verbose`.
- **Idempotent**: re-running changes nothing if already applied. Every edited file is backed up to
  `<file>.bridge.bak` once, and edits are marked with `# >>> rpi-lark-bridge >>>` fences so
  `uninstall.sh` can remove exactly its own lines.
- **Fails early and loudly** in `00-preflight.sh`: wrong Pi model, unexpected OS release, missing
  Ethernet, insufficient disk, an already-running PulseAudio — refuse rather than half-install.
- **Ends by running `70-verify.sh`**, which runs `bridgectl doctor` and exits non-zero on any FAIL,
  so CI/scripted installs can gate on it.

**How close to one command can we realistically get?** Two irreducible manual steps remain:
1. **Bluetooth pairing confirmation on the Pixel.** Cannot be automated from the Pi side; it is a
   deliberate user-consent step in Android.
2. **Pico BOOTSEL on first flash** — unless the GPIO26→RUN wire in §12.1 is fitted, in which case
   `flash-pico.sh` can reset into the bootloader itself and even this disappears.

Everything else — packages, users, lingering, boot config, overlays, PipeWire/WirePlumber/BlueZ
config, udev, systemd units, the Python venv, the Pico toolchain — is scripted. So: **one command
plus one phone tap**, and optionally one button press.

---

## 14. Definition of done

### Mode 1 / 1W (Bluetooth track)
1. On a real cellular call placed from the Pixel 7a, the far end hears the **Lark A1**, not the
   Pixel's built-in microphone, verified by a recording from the far end.
2. The far end's voice reaches the selected output (A2DP headphones in Mode 1, USB DAC in Mode 1W)
   and not the Pixel's speaker.
3. `bridgectl status` correctly reports every field listed in the brief's diagnostics section,
   including the live HFP codec and SCO transport state.
4. **Mode 1 only:** a 60-minute continuous call with A2DP active on the single onboard controller
   completes with <1 dropout per minute and zero SCO disconnections (test E1) — **or** this
   requirement is formally replaced by a written, btmon-evidenced limitation report in
   `docs/experiments/E03-hfp-a2dp-coexistence.md`, and Mode 1W meets criteria 1–3 and 5–8 instead.
5. Every device in test group I recovers automatically within 30 s in ≥95 % of 20 trials, with no
   human action and no service restart by hand.
6. After `reboot`, the system reaches a call-ready state within 90 s with **zero** manual `pw-link`,
   `bluetoothctl`, or `systemctl` commands, 10 times out of 10.
7. Killing `bridged` mid-call does not interrupt audio.
8. A fresh Raspberry Pi OS image plus `git clone` plus `sudo ./scripts/install.sh` plus one pairing
   confirmation on the phone yields a working system, with `70-verify.sh` exiting 0.

### Mode 2 (USB track)
9. The Pico enumerates on the Pixel 7a as a full-duplex USB audio device with both a playback and a
   capture endpoint, surviving 20 unplug/replug cycles.
10. A recording made in an Android app while Mode 2 is active contains **Lark** audio.
11. Android playback routed to the Pico arrives at the Pi and can be sent to the selected sink.
12. A 60-minute Mode 2 session shows bounded, non-ramping Pico ring-buffer occupancy and zero
    underrun/overflow events.
13. Whether Android routes *cellular call* audio to the Pico is **documented with evidence** for
    native calls, Discord, and one other VoIP app — a negative result is an acceptable completion of
    this item, a missing result is not.

### Repository
14. Every §3 file exists and is non-empty; `make lint` and `make test-host` pass in CI with no
    hardware.
15. `docs/experiments/` contains a completed report for E01–E06 with raw data in `results/`.
16. `scripts/uninstall.sh` returns a system to its pre-install state, verified by diffing the
    backed-up files.

---

## 15. Open questions

Only the ones I genuinely cannot resolve by reading, measuring, or deciding myself.

1. **If the onboard radio cannot do HFP+A2DP acceptably (R1/R2 fail), what do you want?** The options
   are (a) Mode 1W with wired output becomes the product, (b) Mode 2 becomes the primary, (c) you
   relax the no-dongle constraint. I will default to **(a)** and keep building unless you say
   otherwise — the plan is structured so this costs nothing either way — but it is your call which
   compromise you actually want to live with.
2. **Which output devices must work?** Make, model, and codec support of the headphones and the car
   stereo. This determines A2DP codec/latency testing and whether 44.1 kHz resampling is in the
   steady-state path. Please name the specific units.
3. **Is ~200–300 ms of added one-way delay on the audio you hear acceptable** (Mode 1 with A2DP), or
   should Mode 1W be the default from the start? This is a subjective comfort judgement about your
   own conversations and I should not guess it.
4. **Is this appliance mains-powered and stationary, or portable/battery?** Battery operation changes
   the power design (§12.2), boot-time targets, and whether Ethernet-for-development is even
   available in normal use.
5. **Should the diagnostic PCM taps be capable of recording call audio persistently, or only on
   explicit short-lived request?** Recording calls has legal and consent implications that vary by
   jurisdiction. I will default to **explicit, time-boxed, off by default** unless you want
   otherwise.

---

## 16. Sources

Retrieved 2026-08-15. Official documentation and upstream source were preferred; forum threads are
cited only as evidence of *field behaviour*, and are marked as such.

**Upstream source / official documentation**
- [WirePlumber 0.5 Bluetooth configuration](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html) — `bluez5.roles` defaults, `bluez5.hfphsp-backend`, `bluez5.enable-msbc`, `monitor.bluez.seat-monitoring`
- [wireplumber `bluetooth.conf` example](https://github.com/PipeWire/wireplumber/blob/master/src/config/wireplumber.conf.d.examples/bluetooth.conf) — full property list incl. `bluez5.hw-offload-sco`, `bluez5.default.rate`
- [PipeWire `spa/plugins/bluez5/defs.h`](https://github.com/PipeWire/pipewire/blob/master/spa/plugins/bluez5/defs.h) — `SPA_BT_UUID_*` and `spa_bt_profile_from_uuid()`; establishes the remote-perspective naming
- [PipeWire `spa/plugins/bluez5/backend-native.c`](https://github.com/PipeWire/pipewire/blob/master/spa/plugins/bluez5/backend-native.c) — `SPA_BT_PROFILE_HFP_HF` dispatches the **AG** state machine
- [Linux DT binding: broadcom-bluetooth.yaml](https://www.kernel.org/doc/Documentation/devicetree/bindings/net/broadcom-bluetooth.yaml) — `brcm,bt-pcm-int-params`, sco-routing value table
- [`bcm2837-rpi-3-b.dts`](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/broadcom/bcm2837-rpi-3-b.dts) — the `bt` node sets only `shutdown-gpios`
- [Raspberry Pi DT overlays README](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/overlays/README) — `googlevoicehat-soundcard`, `disable-wifi`, `disable-bt`
- [Raspberry Pi: Using OTG mode on Raspberry Pi SBCs](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-009276-WP/Using-OTG-mode-on-Raspberry-Pi-SBCs) — why the Pi 3B cannot be a USB gadget
- [Android: USB digital audio](https://source.android.com/docs/core/audio/usb) — "limited USB Audio Class 1 (UAC1) support" in host mode; supported formats and rates
- [BlueZ `main.conf`](https://github.com/bluez/bluez/blob/master/src/main.conf) — `[General] Class` semantics (major+minor only), `[Policy]` reconnect keys
- [TinyUSB documentation / changelog](https://docs.tinyusb.org/en/latest/info/changelog.html) — UAC1 and UAC2 support
- [TinyUSB `uac2_headset` example](https://github.com/hathach/tinyusb/tree/master/examples/device/uac2_headset)
- [TinyUSB `dcd_rp2040.c`](https://github.com/hathach/tinyusb/blob/master/src/portable/raspberrypi/rp2040/dcd_rp2040.c) — RP2040 ISO limits, errata-15 workaround

**Build tooling (PlatformIO track, ADR-0008)**
- [arduino-pico PlatformIO docs](https://arduino-pico.readthedocs.io/en/latest/platformio.html) — `platformio.ini` keys, `board_build.core = earlephilhower`, `-DUSE_TINYUSB`, Windows long-path requirement
- [maxgerhardt/platform-raspberrypi](https://github.com/maxgerhardt/platform-raspberrypi/) — the actively maintained PlatformIO RP2040/RP2350 platform
- [OpenStickFoundation/wizio-pico](https://github.com/OpenStickFoundation/wizio-pico) — `framework = baremetal`; bundles pico-sdk **1.4.0**; maintenance explicitly deprioritised by its author. **Rejected.**
- [arduino-pico#2707 — USB Audio Class / TinyUSB](https://github.com/earlephilhower/arduino-pico/issues/2707) (closed, no documented fix) — the basis for risk R15

**Upstream issues (evidence of real defects)**
- [tinyusb#2838 — uac2_headset broken on RP2040](https://github.com/hathach/tinyusb/issues/2838) (closed; fixed by PR #1802)
- [tinyusb#1728 — UAC2 headset fails after playback time](https://github.com/hathach/tinyusb/issues/1728) (**open**)
- [raspberrypi/linux#2229 — HSP/HFP not working on Pi 3](https://github.com/raspberrypi/linux/issues/2229) (**open since 2017**)
- [Bluetooth: hci_bcm: Configure SCO routing automatically (patchwork)](https://patchwork.ozlabs.org/patch/926903/)

**Reusable prior art**
- [malacalypse/rp2040_i2s_example](https://github.com/malacalypse/rp2040_i2s_example) — PIO `i2s_bidi_slave`, pin adjacency rules, DMA double-buffering helpers
- [suikan4github rpp_driver `I2sSlaveDuplex`](https://suikan4github.github.io/rpp_driver/classrpp__driver_1_1I2sSlaveDuplex.html) — duplex I2S slave up to 192 kHz above 120 MHz sysclk

**Field reports (behaviour, not specification)**
- [Gentoo Forums: PipeWire as a Bluetooth headset for a phone](https://forums.gentoo.org/viewtopic.php?t=1174685) — WirePlumber instability when the HFP HF path is exercised as a system service
- [btstack-dev: Reason for BCM43438 no SCO over HCI](https://groups.google.com/g/btstack-dev/c/pZmp9QVWYBk) — not a hardware limit; a vendor-configuration/documentation gap
- [Raspberry Pi Forums: full-duplex I2S behaviour](https://forums.raspberrypi.com/viewtopic.php?t=317483) — "only the direction started first works" failure mode
- [Collabora: PipeWire Bluetooth support status update](https://www.collabora.com/news-and-blog/news-and-events/pipewire-bluetooth-support-status-update.html) (2022) — native backend scope, mSBC dependency on kernel + chipset
