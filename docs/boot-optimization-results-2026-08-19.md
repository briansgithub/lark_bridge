# Warm-boot optimization results — 2026-08-19

## Scope and status

This campaign used SSH-only warm reboots on the Raspberry Pi 3B. It did not use UART, switched
power, kernel/Device Tree/bootloader changes, or automated far-end call audio. Results therefore
prove idle readiness only and cannot accept a production optimization.

The tested source was `codex/boot-optimization` at `0f5b761` for the paired Netplan fast-path
screen and `5da7ef0` for the guarded skip smoke screen. Full run evidence is under ignored local
`artifacts/boot-run-*` directories. The protected operator planning document was not modified.

## Reconciled baseline

The source deployment now matches the known-good live boot behavior: early latency tuning,
read-only SCO routing verification, the UID-1000 login-barrier override, persistent Ethernet before
cloud-init disablement, and no active static PipeWire endpoint drop-in. Install, verification,
deployed hashes, rollback ancestry, explicit rollback/reapply, and the 120-second trial rollback
were exercised on the Pi.

Three smoke boots passed at 38.908, 38.088, and 38.248 seconds. A separate ten-boot baseline had a
38.458-second median, plus 56.062- and 100.441-second tail events. The 100.441-second run captured a
real BCM43438 command-timeout/recovery sequence; the watchdog recovered automatically and the final
readiness probe passed. A diagnostic reboot reproduced that recovery path. These events are not
discarded and remain robustness evidence.

## Candidate 1: boot-only Netplan reload fast path

Process-ancestry capture proved that the Raspberry Pi NetworkManager build launches
`netplan generate`, which in turn performs udev and systemd manager reloads while NetworkManager is
starting. The candidate keeps Netplan generation but invokes the generator directly only while
systemd reports `starting`. At runtime it delegates every command to the normal Netplan CLI.

Three smoke boots passed with no tracked health events. Ten randomized pairs were then run with
seed `20260819`; every variant application had a fresh rollback transaction and every boot passed.

| Metric | Baseline | Candidate | Change |
|---|---:|---:|---:|
| Passing warm boots | 10/10 | 10/10 | no failures |
| External idle-ready median | 42.178 s | 33.835 s | -8.343 s |
| External idle-ready p95 | 42.489 s | 37.380 s | -5.109 s |
| Pi-local startup median | 18.623 s | 14.616 s | -4.008 s |
| NetworkManager median | 6.249 s | 1.876 s | -4.373 s |
| XRUN, undervoltage, HCI, restart, filesystem events | 0 | 0 | no regression |

All ten paired external deltas favored the candidate: 12.224, 6.808, 4.923, 5.079, 7.678, 4.019,
4.712, 4.668, 8.337, and 8.367 seconds. The bootstrap 95% interval for median improvement was
4.4805–8.682 seconds. The controller verdict is `PROVISIONAL_IDLE_IMPROVEMENT`: automated two-way
call audio, AEC verification during a call, robustness, and soak gates remain mandatory before
promotion. The normal installer therefore leaves this mode unchanged unless the explicit
`--networkmanager-fastpath enable` experiment option is supplied.

## Candidate 2: audited duplicate-generation skip

The second candidate skipped the boot-time generator only when all of these were true: no
`/etc/netplan` or `/run/netplan` YAML, the audited vendor policy hash matched, and the early generated
NetworkManager file existed and was empty. Otherwise it fell back to generation.

Three smoke boots passed with no tracked health events, but Pi-local startup stayed at
14.307–14.345 seconds and NetworkManager stayed at 1.892–1.909 seconds. This is effectively the same
as candidate 1, so the candidate was rejected at the smoke gate and no twenty-boot paired screen was
run.

## Hardware-free stopping point

No unexamined, safely removable userspace delay of at least 250 ms remains on the readiness critical
path:

- NetworkManager's remaining approximately 1.9 seconds persisted even when duplicate generation
  was skipped. Carrier and DHCP follow it; replacing DHCP with a fixed address would change network
  behavior and was not attempted without out-of-band recovery.
- SSH startup is approximately 0.6 seconds, but the Ethernet address becomes reachable later, so
  reordering SSH cannot improve observed readiness.
- The user manager, PipeWire, WirePlumber, supervisor, Bluetooth verifier, and other longer units run
  in parallel and were already ready when SSH became reachable in the paired campaign.
- Filesystem checks, random-seed handling, watchdogs, and recovery services are retained for
  robustness. USB/device discovery and the remaining large pre-userspace interval require UART or
  boot-stack changes and are outside the authorized recovery envelope.

The Pi was finally rebooted on the original baseline at `5da7ef0`; idle readiness passed at 43.488
seconds with zero tracked health events, the experiment files were absent, and no trial transaction
was pending.
