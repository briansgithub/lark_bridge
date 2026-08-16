# E04 — What exactly is the Hollyland Lark A1 receiver, electrically and to ALSA?

- **Status:** Not started
- **Resolves risk:** R9 (probability 2, impact 3, score 6)
- **Gates milestone:** M1 (Stage A)
- **Script:** `tests/stage-a-lark/run.sh`

## Question

1. Which sample rates, formats and channel counts does the receiver's USB Audio Class
   capture interface actually support?
2. Is 48 kHz among them? (ADR-0007 assumes yes — the whole graph is built on it.)
3. Is the audio mono, stereo, or mono duplicated into one channel of a stereo stream?
4. Does the receiver apply its own AGC, limiting or noise gate that will fight ours?

## Why it cannot be answered by reading

Hollyland does not publish USB descriptors, and reviews describe sound quality, not
`bDescriptorType`. This is a five-minute measurement.

## Method

```bash
./tests/stage-a-lark/run.sh
```

which runs, at minimum:

```bash
lsusb -v -d <vid>:<pid>                       # full descriptors, including altsettings
cat /proc/asound/cards
arecord -D hw:CARD=<name> --dump-hw-params    # the authoritative capability list
arecord -D hw:CARD=<name> -f S16_LE -r 48000 -c 1 -d 60 lark.wav
udevadm info -a -n /dev/snd/<node>            # stable match keys for the udev rule
```

Then `tools/audio/level_meter.py` for noise floor, DC offset, clipping and per-channel
content.

## Runs

| # | Date | Rates offered | Formats | Channels | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-08-16 | **48000 only** | S16_LE, S24_3LE | 2 (bit-identical) | CONFIRMED |

## Result

`3547:0407` "Shenzhen Hollyland Technology Wireless Microphone", USB port `1-1.3`.

```
FORMAT:   S16_LE S24_3LE
CHANNELS: 2
RATE:     48000
```

| Measurement | Value |
|---|---|
| Sample rate | **48 000 Hz, fixed** — no other rate offered |
| Formats | S16_LE, S24_3LE |
| Channels | 2, but **bit-identical** (max difference **0 LSB** across a 5 s capture) |
| Idle noise floor | −50.6 dBFS RMS, −39.3 dBFS peak (room + preamp) |
| Mixer controls | **none** — only a read-only `Capture Channel Map` |

## Verdict

**CONFIRMED, and favourably.**

1. **48 kHz is fixed and matches ADR-0007's graph rate exactly.** There is therefore **zero
   resampling at the first hop**, and the assumption the whole audio architecture rests on is
   now measured rather than assumed. ADR-0007 stands unchanged.
2. **The stereo stream is mono duplicated.** Both channels are bit-identical, so the receiver
   carries one microphone into two channels. The graph should take a single channel; a downmix
   would be harmless but pointless, and there is no risk of the 6 dB loss that downmixing a
   correlated pair would otherwise imply.
3. **No device-side gain control exists.** This confirms the plan's decision to apply microphone
   gain on the PipeWire `bridge.mic` node — that turned out to be the only available option, not
   a stylistic preference. Device mixers also vanish on replug, node volumes do not.
4. The receiver exposes **capture only**, with no playback endpoint — exactly the Android
   limitation this whole project exists to work around, now confirmed at the source.

### Not yet answered

Whether the transmitter applies internal AGC or noise gating. Not visible from descriptors; it
would show up as non-linearity in an acoustic level sweep. The U15 acoustic sweep was linear at
~1 dB per volume step over 30 dB, which is **weak evidence against** aggressive AGC, but that
sweep was not designed to test it. Worth a dedicated run before trusting absolute levels.

## Consequences for the plan

| Finding | What happens |
|---|---|
| 48 kHz supported, mono or stereo | Nominal. ADR-0007 holds unchanged. Proceed. |
| 48 kHz **not** supported | ADR-0007 must be revisited — the graph rate was chosen partly because the Lark was assumed to be 48 kHz. A resample appears at the very first hop. |
| Audio on one channel of a stereo pair | `bridge.mic.capture` needs `stream.dont-remix = true` and an explicit channel pick, not a downmix — a downmix would cost 6 dB. |
| Receiver applies aggressive AGC | Document it. It will interact badly with any gain staging and with future AEC; note it in `docs/hardware/lark-a1.md` as a known constraint. |

## Deliverables beyond the verdict

- `docs/hardware/lark-a1.md` filled in with the measured table.
- `pi/udev/90-bridge-lark.rules` populated with real match keys.
- `config/bridge.toml.example` `[devices.lark]` IDs filled in.

## Follow-up questions this raised

_(fill in — e.g. does the receiver renumerate identically after a power cycle?)_
