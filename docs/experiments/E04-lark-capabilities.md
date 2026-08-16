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
| 1 | | | | | |

## Result

_(fill in — paste the `--dump-hw-params` output verbatim, it is the primary evidence)_

## Verdict

_(fill in)_

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
