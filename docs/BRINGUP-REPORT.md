# Hardware bring-up report — 2026-08-16

First session on real hardware. Written for whoever picks this up next, including the
mistakes, because several of them are traps that would otherwise be walked into again.

---

## THE PROJECT'S CORE PREMISE IS PROVEN

**Mode 1W works end to end.** Speaking into the Lark A1 transmitter is heard by Discord on
the Pixel 7a, through the bridge:

```
Lark A1 transmitter ──2.4 GHz──► Lark receiver ──USB──► Pi
   └─► bridge.mic ─► HFP/mSBC over eSCO ─► Pixel 7a ─► Discord
Pixel ─► HFP downlink ─► bridge.callout ─► USB DAC (wired output)
```

Confirmed by the operator ("Discord is registering my voice through the bridge") and
corroborated objectively — evidence in `docs/experiments/results/M5/working-system.txt`:

| Measurement | Value |
|---|---|
| Link | eSCO, **mSBC wideband**, Transparent air mode, 60-byte frames |
| SCO throughput | **rx +1337 / tx +1338 packets per 10 s** — mSBC nominal 1330, symmetric |
| Tone injected into the Lark | −18.4 dBFS at the bridge input, 19.1 dB SNR, no clipping |
| Android `active_communication_device` | `bt_sco_hs` **larkbridge** (also `computed` and `applied`) |
| SCO routing | `0x01` Transport |

This satisfies acceptance criterion 1 in `PLAN.md` §14 — *the far end hears the Lark, not the
phone's built-in microphone* — for VoIP. Native cellular is still untested (SIM reports
`LOADED,NOT_READY`).

**Milestone M5 (Stage C: Lark → HFP uplink) achieved.**

---

**Also: the project's two highest-scoring risks are closed, both favourably.**

---

## Status at a glance

### Test ladder — 15 passing

| Tier | Tests | State |
|---|---|---|
| Pi platform | U01–U05 | ✅ all pass |
| Audio devices | U10, U13, U15 | ✅ pass |
| Bluetooth | U20, U21, U22 | ✅ pass |
| Phone control | U30, U32, U33, U34 | ✅ pass |
| Pico | U40–U46 | ⬜ not started (needs diode) |
| Dongle basics | U11, U12, U14 | ⬜ superseded in practice by U13/U15 |

### Risks resolved

| Risk | Was | Now |
|---|---|---|
| **R2** SCO never reaches the host on BCM43438 | P3 × I5 = **15** | **CLOSED** — fixed and verified |
| **R4** WirePlumber unstable in the HFP HF path | P3 × I4 = **12** | Largely closed: SLC completes, no crashes observed. 30-min soak still owed. |
| R15 openocd `bcm2835gpio` unavailable | P3 × I3 = 9 | **CLOSED** — both GPIO drivers compiled in, `rp2040.cfg` ships |
| R9 Lark not 48 kHz | P2 × I3 = 6 | **CLOSED** — 48 kHz fixed, exactly the graph rate |

**R1 (single-radio HFP + A2DP coexistence) remains open and is now the top risk.** Its
measurement loop is built and proven (U22), so spike S3 can run unattended.

### Measured constants

| Constant | Value |
|---|---|
| Rig noise floor (loopback) | −89.2 dBFS RMS, linear ±0.21 dB over 40 dB |
| Rig usable dynamic range | 71.5 dB |
| Acoustic path (speaker → Lark) | speaker vol 19 → −19.5 dBFS, **31.0 dB SNR** |
| A2DP capture loop | capture gain 30 → −19.7 dBFS, **53.5 dB** above floor |
| Lark | 48 000 Hz fixed, S16/S24, 2 ch **bit-identical** (mono duplicated) |
| HFP codec achieved | **mSBC**, eSCO, Transparent air mode, 60-byte frames |

---

## What went right

**Risk-first ordering paid off immediately.** The plan put spikes S1–S3 before any product
code specifically because R2 could reshape the project. It nearly did: the default SCO routing
is broken on this hardware, and finding that on day one rather than after building a routing
daemon on top of it is the entire value of that sequencing.

**Measurement beat inference, repeatedly.** Three separate times a confident reading of upstream
source or a plausible physical theory was wrong, and the measurement corrected it within
minutes. The `bluez5.roles` convention is the clearest case — see below.

**Calibrating the instrument before the device under test.** U13 exists to measure the rig's own
error floor, and it immediately caught that the C-Media card ships with **Auto Gain Control on**.
AGC would have silently rescaled every level measurement and could have masked dropouts by
pumping gain during silence. That would not have looked like a bug; it would have looked like
clean data. An instrument with AGC is not an instrument.

**"What this does NOT prove" on every test.** Forcing each unit test to state its own limits
stopped several green ticks from being over-read — notably U21 (device connects) versus U22
(audio actually recoverable), which are very different claims.

**Stable identity by USB port path.** The plan insisted on port paths over card numbers and
serials. Both dongles turned out to report the **identical** USB serial `202405280846`, so
serial-based identity would have silently swapped the DUT and the instrument.

**`exit 78` for absent hardware.** Distinguishing "not connected" from "failed" meant a missing
device never once read as a defect, and the ladder paused cleanly instead of producing noise.

**Seeing the phone directly.** `adb exec-out screencap` meant the phone's UI was read, not
narrated — pairing was driven end to end (open settings, scan, tap device, accept dialog) with
screenshots as evidence at each step.

---

## What went wrong

Honest list. Several of these are recorded because the *class* of error will recur.

### 1. Asserted the wrong Bluetooth role convention, and propagated it

The worst one. From reading `spa_bt_profile_from_uuid()` and `backend-native.c`, the plan
claimed `bluez5.roles` names the **remote's** role, and that table was written into `PLAN.md`
§6.1, the WirePlumber config's prominent warning block, E02, and the S2 spike script.

It is the opposite. Measured by setting each value and reading back `bluetoothctl show`:
`a2dp_source` → we advertise Audio Source; `hfp_hf` → we advertise Handsfree. The shipped config
would have made the Pi present to Android as a *phone* rather than a headset.

The subtlety that caused it is real: internal `SPA_BT_PROFILE_*` constants **do** describe the
remote's role (the phone shows as `HFP_AG`), while the `bluez5.roles` config key names our own.
Both readings were right about different things. **The lesson is not "read more carefully" — it
is that a five-minute experiment outranks an hour of source reading.**

### 2. Stated a conclusion the evidence didn't support

Mid-debugging, claimed the config file was "decisively" breaking the bluez monitor, based on
comparing two logs — one of which had been truncated by `head -25` *before* the relevant line.
The claim was retracted a minute later, but it should never have been made. Truncated output is
not evidence of absence.

### 3. Asserted a physical cause without testing it

A2DP tone readings sat 15–20 dB below the signal's own peak. Hypothesised clock drift across the
asynchronous A2DP link, and built a frequency search to compensate. The search found the tone at
**exactly 1000 Hz**, disproving it. The real cause was insufficient settle time — A2DP buffers
150–250 ms and the sink ramps on stream start, so a 2 s wait was capturing ramp-up. The
frequency search was kept (it is correct for genuinely asynchronous paths, and the Pico's USB
audio will need it), but it was built for the wrong reason.

### 4. Config in the wrong section, silently doing nothing

`monitor.bluez.seat-monitoring = disabled` was placed inside `monitor.bluez.properties`. It is a
**profile feature**. In the wrong section it parses without error and has no effect whatsoever.
Symptom: adapter fine, `bluetoothctl` fine, WirePlumber reports `active` — and Bluetooth audio
simply does not exist. Cost roughly an hour.

### 5. Self-inflicted disconnection

`loginctl terminate-user admin` was run while logged in as `admin` over SSH. It worked exactly
as documented.

### 6. Repeated environment mistakes

- **Heredoc + stdin redirection**, three times. `python3 - <<'PY' < file` makes Python read the
  *program* from stdin, so the data never arrives. Fixed structurally: reducers are real files
  now, never inline heredocs.
- **MSYS2 path translation.** Git Bash rewrites POSIX paths to Windows form only when they are
  separate *arguments* to a native binary, never inside a `-c` code string. Embedded paths
  reached `py.exe` as `/b/Desktop/...`. Fixed by passing JSON via stdin.
- **CRLF injection.** Editing a shell script with Python on Windows — `write_text` translates
  `\n` to `\r\n` — produced `$'\r': command not found` on the Pi. This is precisely the failure
  `.gitattributes` and the CI check exist to prevent, introduced by bypassing both.
- **awk `$1` assignment.** Modifying a field rebuilds `$0` using `OFS`, which destroyed the `=`
  in the TOML parser and silently returned whole lines.

### 7. Unfixed

`tools/bt/hci_vendor_cmd.py` fails with `EINVAL` binding the raw HCI socket while the adapter is
up. `hcitool` works and the production service uses it, so this is not blocking — but the tool
was written specifically because `hcitool` is deprecated and may vanish.

---

## Traps for whoever comes next

1. **`bluez5.roles` names YOUR role.** `a2dp_source` + `hfp_hf` + `hsp_hs`. Verify with
   `bluetoothctl show`: you want `Audio Source (110a)` and `Handsfree (111e)`.
2. **`monitor.bluez.seat-monitoring = disabled` goes in `wireplumber.profiles`**, not in
   `monitor.bluez.properties`. Headless Bluetooth does not work without it.
3. **SCO routing resets to PCM on controller power-up.** `bridge-btfw.service` handles it, but if
   HFP audio is one-directional, check `hcitool -i hci0 cmd 0x3f 0x1d` first — the first
   parameter must be `01`.
4. **The HFP card only appears once SCO is active**, i.e. during a call. An absent
   `bluez_card.<phone>` with a completed SLC is normal, not a fault.
5. **Android initiates to headsets; the Pi should not chase it.** Connecting from the Pi gave
   `br-connection-page-timeout`; tapping the device on the phone connected instantly.
6. **The AB13X dongles are single combo-jack.** Inserting a 3-pole plug makes them re-enumerate
   *without* an audio input interface at all. They can only ever be outputs.
7. **User-unit logs do not reach the journal on this image** (`No journal files were found`).
   Debug PipeWire by running it in the foreground with `WIREPLUMBER_DEBUG=D`.
8. **Restarting `bluetooth.service` drops all connections** and orphans PipeWire's endpoint
   registrations. Restart WirePlumber afterwards, and expect to reconnect devices.
9. **`pw-loopback -P <props>` silently defeats `--playback <target>`.** With any playback
   property dict at all the stream lands on the DEFAULT sink and nothing reports an error.
   Measured, same call otherwise identical:

   | invocation | result |
   |---|---|
   | no `-P` | `bluez_output…` **correct** |
   | `-P "{ node.pause-on-idle=false }"` | dongle B — wrong |
   | `-P "{ media.role=Communication … }"` | dongle B — wrong |
   | `-P "{ target.object=<sink> … }"` | dongle B — wrong |

   Re-supplying `target.object` by hand does **not** rescue it, so this is not simply "`-P`
   overwrites what `--playback` set". Do not pass `-P` on a leg whose target matters. This cost
   an evening: the uplink appeared to work only because WirePlumber had *also* auto-linked the
   Lark straight to the HFP sink, so the mic was being summed into the call twice by a path
   nobody configured.
10. **Verify where a loopback actually landed — never assume.** `--capture`/`--playback` are a
    *request*. `bridge_supervisor.py` checks with `pw-link -l` and restarts the leg if it is
    wrong. Use plain `pw-link -l`; adding `-I` right-aligns object IDs so every line begins with
    whitespace, the node-header test never fires, and the parser silently returns zero links.
11. **`pkill -f "pw-loopback …"` over SSH kills your own session.** The remote shell's command
    line contains the pattern, so it matches itself; you get exit 255 and lose all output. Kill
    by PID captured from `$!`.

---

## Still open

| Item | Notes |
|---|---|
| **R1 — HFP + A2DP on one radio** | **Measured PARTIAL — see E03.** Intermittent; our own stack tears A2DP down under active SCO. Config-level mitigations exhausted; four untried non-config avenues listed in E03 before it may be called a hardware limitation. Mode 1 is deferred at the user's request. |
| Mode 1W signal path | **Working and verified.** `bridge-supervisor` builds `Lark → HFP sink` and `HFP source → dongle A` on call-up and tears them down on call-end. |
| E02 part 3 | **PASS** — 30-minute HFP soak clean. |
| SIM `LOADED,NOT_READY` | Native cellular calls may not place; VoIP (Discord) works and was used throughout. |
| Pico track | Soldered except the **1N400x diode**. Until it is fitted, do **not** connect Pi 5 V → VSYS; power the Pico from USB only. |
| Reboot persistence | `bridge-btfw.service` is enabled but has not yet survived an actual reboot. |

---

## Bench state

- Pi `larkbridge` at `192.168.0.251`, Debian 13 trixie, kernel 6.18.34, `throttled=0x0`
- PipeWire 1.4.2 / WirePlumber 0.5.8 / BlueZ 5.82, user session with lingering
- USB: Lark `1-1.3`, dongle A (AB13X) `1-1.5`, dongle B (C-Media) `1-1.4`
- Bluetooth paired + trusted: iWorld `50:D7:1B:74:34:D6`, Pixel 7a `5C:33:7B:CB:BF:C5`
- Phone on ADB over 5 GHz Wi-Fi from the control PC
