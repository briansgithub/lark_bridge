# AEC arrival-day qualification

The normal S31 image runs ESP-SR AEC for timing and resource evidence but publishes digital silence
to HFP. It never falls back to raw LARK samples. Only an explicitly labeled H10 image with
`CONFIG_LARKBRIDGE_S31_EXPERIMENTAL_AEC_UPLINK=y` may publish processed samples before H10 passes.

## Capture topology

Do not attempt to stream PCM over the 115200-baud inter-board UART. It carries control and status
only. Use three synchronized stimulus/capture artifacts:

1. `reference.wav`: the exact speech or multitone stimulus played by the far-end computer into the
   active Pixel call.
2. `raw.wav`: a control capture with the LARK receiver connected directly to the recording computer
   as a UAC microphone, using the same microphone, speaker, gain, room, and placement.
3. `clean.wav`: the returned microphone leg recorded at the far end while the H10 bridge image is
   in the call.

Start every file from the same stimulus and retain at least two seconds of leading calibration plus
five seconds of trailing silence. The analyzer aligns correlated energy, so transport delay may
differ between the direct-UAC control and call capture. Record the S31 log alongside every clean
capture; it is the evidence for engine restarts, starvation, correction, processing time, and
minimum heap.

## Reference-delay sweep

The configured delay aligns the 48 kHz LARK microphone stream to the 16 kHz reference tapped only
after samples are actually rendered onto I2S. It is acoustic-path compensation, not a network
latency control.

1. Keep all geometry and gains fixed.
2. Build qualification images at 0, 40, 80, 120, 160, and 200 ms.
3. Capture the same stimulus three times per value.
4. Select the best median objective suppression with zero fail-closed restart.
5. Sweep ±40 ms around that value in 10 ms steps, again three captures each.
6. Freeze the winning value and its evidence in the result record. If the optimum is pinned at
   200 ms, fail the calibration gate and measure the real path before extending the fixed ring.

For a speech stimulus:

```powershell
py -3 rig/analysis/aec_metrics.py --reference reference.wav --raw raw.wav --clean clean.wav --signal speech --min-suppression-db 10 --trailing-silence 5 > metrics.json
```

For the five-frequency signal, substitute `--signal multitone`; the reference file is optional for
that analyzer mode but should still be retained.

## H10 pass criteria

All three final-value repetitions must satisfy the analyzer and the logs:

- at least 10 dB correlated echo suppression after convergence;
- raw echo above the analyzer's measurable floor (`-55 dBFS` default);
- no more than 0.01% full-scale samples in raw or clean capture;
- no AEC starvation, output drop, unexpected resynchronization, allocation failure, reset, or
  processed-ring underrun during the scored interval;
- processing maximum remains below one AEC frame period, and minimum 8-bit heap does not trend
  downward across repetitions;
- LARK unplug, I2S/reference loss, route change, call end, output heartbeat loss, and either-board
  reset each return uplink to digital silence before any raw sample can escape.

Peak headroom, latency, and minimum-heap release thresholds are characterization outputs on the
first hardware. Freeze numeric limits only after the three valid baseline runs; do not turn a
post-hoc observed value into a pass criterion for that same run.

After H10 passes, copy its calibrated delay into the production defaults, rebuild with the
experimental option renamed/disabled as appropriate, and repeat one confirmation capture from the
clean production commit.
