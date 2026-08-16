# E06 — Does `bcm2835-i2s` actually do simultaneous playback and capture?

- **Status:** Not started
- **Resolves risk:** R5 (probability 3, impact 3, score 9)
- **Gates milestone:** M10 (Stage G)
- **Script:** `tests/stage-g-pi-pico/run.sh`

## Question

Can the Pi 3B run its I2S playback and capture substreams **at the same time**, at 48 kHz,
with the Pi as clock master — and with which device-tree overlay?

## Why it cannot be answered by reading

The BCM2835 PCM peripheral is documented as full-duplex, and `bcm2835-i2s` is a mature
driver. But full duplex needs a *machine driver* pairing the CPU DAI with a codec node that
declares both directions, and there are repeated field reports of the practical failure
mode: **only the direction started first works**, or playback runs at apparently half speed
while capture is fine.

Our "codec" is a Pico that presents no I2C control interface, so we need either a shipped
overlay that tolerates a codec-less link or a custom one. Which shipped overlays genuinely
provide duplex is not documented — it has to be tried.

## Method

### Step 1 — fast path, no DT authoring

```
dtoverlay=googlevoicehat-soundcard
```

This is a no-external-codec, 48 kHz, Pi-as-master I2S card that ships with Raspberry Pi OS.
If it exposes both substreams, Stage G can start immediately.

```bash
cat /proc/asound/cards
aplay -l && arecord -l                      # must list the SAME card in both
arecord -D hw:CARD=<n> --dump-hw-params
# The actual test — both at once, not sequentially:
arecord -D hw:CARD=<n> -f S32_LE -r 48000 -c 2 -d 60 cap.wav &
aplay   -D hw:CARD=<n> -f S32_LE -r 48000 -c 2 tone.wav
wait
```

With the Pico running `i2s_loopback` firmware, `cap.wav` should return `tone.wav`.

### Step 2 — product overlay

Replace with the repo-owned `bridge-i2s-duplex` (`simple-audio-card` + a dummy duplex
codec) so the card name, channel count and slot width are ours and stable across OS
updates. Re-run the same test.

## Fallback ladder if duplex fails

In order, per `PLAN.md` §7.6:

1. Open both substreams from one process with `snd_pcm_link()` so they start atomically —
   PipeWire can be made to do this if the card is exposed as a single duplex device.
2. Half-duplex alternating. **Unacceptable for calls**; recorded only to close the option.
3. Two separate I2S peripherals — not available on a Pi 3.

If all fail, the Pi↔Pico transport decision in ADR-0004 must be reopened, with SPI plus
explicit rate matching as the next candidate. That would be a significant change: it
reintroduces two independent audio clocks, which is exactly what I2S was chosen to avoid.

## Runs

| # | Date | Overlay | Card name | Duplex works? | XRUNs / 60 min | Loopback exact? |
|---|---|---|---|---|---|---|
| 1 | | googlevoicehat-soundcard | | | | |
| 2 | | bridge-i2s-duplex | | | | |

## Result

_(fill in)_

## Verdict

_(fill in)_

## Consequences for the plan

| Finding | What happens |
|---|---|
| googlevoicehat duplex works | Use it for M10 step 1 and unblock Stage G immediately. Still write the product overlay. |
| Only the custom overlay works | Fine. Document what the shipped one lacks so nobody retries it. |
| Neither does duplex | Climb the fallback ladder. If `snd_pcm_link()` also fails, reopen ADR-0004. |
| Duplex works but drifts | Not this experiment's problem — that is G3, and it is the Pico's rate control (R7). |

## Follow-up questions this raised

_(fill in — e.g. does the card survive a Pico reset without needing a PipeWire restart?)_
