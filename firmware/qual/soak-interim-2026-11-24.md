# Pixel to S31 Bluetooth soak, interim 2026-11-24

Snapshot through **07:51:39 UTC (02:51:39 ET)**. The logs span **3 h 07 min 46 s**, with **3 h 07 min 28 s** covered by the two parts and an **18 s** gap between them.

| Part | First to last evidence (UTC) | Duration | BR/EDR ACL up polls | Route-ACK timeouts |
| --- | --- | --- | --- | --- |
| `part1` | 04:43:53 to 05:06:35 | 22 min 42 s | 8/8 | 1,048 |
| `part2` | 05:06:53 to 07:51:39 | 2 h 44 min 46 s | 55/55 | 7,605 |
| Total | 04:43:53 to 07:51:39 | 3 h 07 min 28 s covered | 63/63 | 8,653 |

- **ACL events and reconnects.** The Pixel event logs contain zero ACL connects and zero disconnects for the S31. The first poll already showed ACL up. There are no transition timestamps or reconnect times to report.
- **Longest observed connection.** The longest run of up polls was **2 h 42 min 06 s** in `part2`, from 05:06:53 to 07:48:59 UTC. The approximately three-minute polling interval leaves room for an unlogged drop between polls. The estimate stops at the last up poll and excludes the gap between parts.
- **S31 faults.** No reset, panic or brownout markers appeared in the serial capture.
- **Route-ACK timeouts.** The 8,653 timeouts average about one every 1.30 s of covered time. They are expected with the inter-board harness unwired and are excluded from the fault count.

The summarizer reads complete lines from `events.jsonl`, `samples.jsonl`, and `s31-serial.log`. The command assumes the Pixel history clock uses America/New_York when converting event timestamps to UTC. This snapshot had no Pixel ACL events to convert.

```sh
evidence=../lark_bridge-evidence/20261124-bt-soak
python3 -B rig/esp/summarize_soak.py --pixel-timezone America/New_York \
  "$evidence/part1" "$evidence/part2"
```

This is an interim read of the evidence. The soak job and its logs were left alone.
