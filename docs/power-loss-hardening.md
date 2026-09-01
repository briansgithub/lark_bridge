# LarkBridge abrupt-power-loss hardening

Status: implementation and host tests are complete; SD conversion and physical-cut
validation have **not** started. Never run the destructive preparation commands on the
only working card.

## Safety boundary

The deployed Pi is not modified by this workflow until all of the following exist:

1. A byte-for-byte image of the complete source SD card.
2. A separate recovery card flashed from that image.
3. A successful physical boot from the recovery card.
4. A safety-evidence record whose backup hash still verifies.

The preparation scripts reject a missing or changed backup, an untested recovery card,
the running root disk, mounted target partitions, unexpected partition tables, and a
target that is not explicitly repeated as confirmation. The source card remains the
rollback path. Perform conversion on a clone.

No kernel, Device Tree, bootloader, partition, or filesystem command in this repository
has been run on the live Pi as part of this work.

## Resulting storage design

The sealed card has three partitions:

| Partition | Runtime mode | Purpose |
|---|---|---|
| FAT boot | read-only | Firmware, kernel, initramfs, immutable boot configuration |
| ext4 root | read-only lower layer | OS and LarkBridge image; a tmpfs overlay receives runtime writes |
| 1 GiB ext4 `LARKDATA` | read-write, journaled | Only essential durable state |

`LARKDATA` contains:

- a 64 MiB-capped, 14-day-capped system journal;
- the system random seed;
- the live BlueZ pairing database plus two alternating, checksummed snapshots;
- two alternating, checksummed `bridge.toml` slots;
- a bounded recovery/storage-health ledger.

Network leases, caches, sockets, `/tmp`, PipeWire/WirePlumber graph state, supervisor
status, and other service state remain in RAM. SSH host keys live in the immutable root
image. Normal package operations are refused and unattended package services are
masked; software updates are replacement images.

`bridge-storage-guard.service` completes before Bluetooth and the UID-1000 user manager.
It verifies the selected configuration slot, checks BlueZ state, recovers a damaged
configuration from the alternate slot, recovers pairing data from the latest valid
snapshot, and publishes `/run/larkbridge/storage-health.json`. If `LARKDATA` does not
mount, immutable configuration and pairing state are materialized into the RAM overlay
and the device reports `DEGRADED`; SSH remains independent of the guard.

The guard does not alter AEC policy, graph construction, routing, or call lifecycle.

## Build a disposable clone

These commands are examples for a Linux recovery host. Substitute exact paths and
devices after verifying them independently.

1. Image the complete source card with a trusted imaging tool, flash that image to a
   separate recovery card, boot the Pi from the recovery card, and record its card
   serial and `/proc/sys/kernel/random/boot_id`.
2. Create the mandatory record:

   ```sh
   python3 scripts/powerloss/safety-evidence.py create \
     --image /safe/larkbridge-full-card.img \
     --output /safe/larkbridge-safety-evidence.json \
     --recovery-card-serial RECOVERY_CARD_SERIAL \
     --recovery-boot-id RECOVERY_BOOT_ID \
     --acknowledge-recovery-boot
   ```

3. Attach a different clone to the Linux recovery host. With every clone partition
   unmounted, create `LARKDATA`:

   ```sh
   sudo scripts/powerloss/repartition-offline-device.sh \
     --device /dev/CLONE \
     --confirm-device /dev/CLONE \
     --safety-evidence /safe/larkbridge-safety-evidence.json \
     --apply
   ```

4. Mount clone partition 2 at `/mnt/lark-root`, partition 1 at
   `/mnt/lark-root/boot/firmware`, and partition 3 at
   `/mnt/lark-root/var/lib/larkbridge-persist`.
5. The disposable image must contain `initramfs-tools` and Debian's
   [`overlayroot`](https://packages.debian.org/trixie/all/overlayroot) package before
   sealing. This is an image-build prerequisite, not a normal deployed package update.
   The preparation script refuses to continue if either is absent.
6. Seal the mounted clone:

   ```sh
   sudo scripts/powerloss/prepare-offline-root.sh \
     --root-mount /mnt/lark-root \
     --confirm-root /mnt/lark-root \
     --safety-evidence /safe/larkbridge-safety-evidence.json \
     --apply
   ```

7. Unmount all three filesystems and run the read-only acceptance helper. Do not boot
   the clone if any check reports an error:

   ```sh
   sudo scripts/powerloss/verify-offline-device.sh \
     --device /dev/CLONE \
     --output /safe/larkbridge-preflight-fsck.txt
   ```

The image builder installs the overlay policy, produces a new initramfs, writes
read-only root/boot entries, creates the persistent state slots, captures immutable
fallbacks, and installs the early guard and recovery probe.

## Persistent-state changes

Configuration is never edited in place on `LARKDATA`. Validate and atomically commit a
candidate into the inactive slot:

```sh
sudo python3 /usr/local/lib/rpi-lark-bridge/powerloss/lark_state.py \
  config-write --source /path/to/candidate-bridge.toml
```

The pointer changes only after the file, checksum manifest, and containing directories
have been synced. A cut at any earlier point leaves the prior slot selected. Pairing
snapshots use the same two-slot rule. A five-minute timer seals a snapshot only after the
BlueZ tree is stable for the complete copy; the previous snapshot remains selected if it
changes during capture.

A committed configuration becomes active on the next boot. This avoids restarting the
audio supervisor in the middle of a call.

## First-boot gate

Before any abrupt cut, a normal boot must pass:

```sh
sudo python3 /usr/local/lib/rpi-lark-bridge/powerloss/powerloss_verify.py
```

The JSON result must have `"ready": true`. It proves that the root overlay is active,
its lower filesystem and the boot filesystem are read-only, storage state is recoverable,
the journal verifies, SSH identity and entropy policy are present, required services are
active, Bluetooth is powered, the Lark and output endpoints exist, routing has no missing
or unexpected links, stale AEC ownership is absent in `CALL_DOWN`, and current-boot logs
contain no critical filesystem, power, watchdog, or restart-loop evidence.

## Manual campaign

Create a campaign only after the first-boot gate and the safety record pass:

```sh
rig powerloss campaign-init \
  --safety-evidence /safe/larkbridge-safety-evidence.json
```

For a normal state, enter or verify that state and arm the cut:

```sh
rig powerloss arm --campaign CAMPAIGN --state idle
# Disconnect when the command says to do so.
rig powerloss observe-off --campaign CAMPAIGN
rig powerloss reconnect --campaign CAMPAIGN
```

`observe-off` requires SSH to disappear. `reconnect` enforces at least ten seconds off,
tells the operator when to reconnect, waits for a new boot ID, repeatedly runs the full
acceptance probe, and compares pre/post configuration, pairing identities, and SSH host
identity. Evidence is written under the campaign directory.

Early-boot cuts use a controlled preparatory shutdown:

```sh
rig powerloss arm --campaign CAMPAIGN --state early-boot-3
# Disconnect after the preparatory shutdown and wait at least ten seconds.
# Reconnect power exactly as this command is started:
rig powerloss early-start --campaign CAMPAIGN --ack-power-connected-now
# Disconnect at the audible/printed prompt.
rig powerloss observe-off --campaign CAMPAIGN --acknowledge-early-cut
rig powerloss reconnect --campaign CAMPAIGN
```

The 1/3/5/10/20-second early cuts remain human-timed and therefore approximate. UART or
controlled power would improve timing precision, but is not required for corruption and
automatic-recovery acceptance.

The controller tracks this minimum matrix:

| Category | Required passing cuts |
|---|---:|
| Early boot at 1, 3, 5, 10, and 20 seconds | 1 each (5 total) |
| Idle / `CALL_DOWN` | 5 |
| Bluetooth connection or recovery | 5 |
| PipeWire/WirePlumber restart | 5 |
| Supervisor graph construction | 5 |
| Active call with AEC | 5 |
| Call teardown | 5 |
| Controlled persistent/journal write | 5 |
| Seeded random cuts | 10 |
| **Total** | **50** |

Use `--state random --random-context STATE --seed SEED` for each seeded-random cut. The
active-call category is intentionally refused unless the supervisor reports a live AEC
owner. The call-teardown category requires `--ack-state-ready` immediately after the
far end is released. Functional call audio must also be exercised when the far-end rig is
available; set `boot_functional_probe_command` in `rig/inventory.toml` and the controller
will make that probe a required recovery gate. The storage probe cannot replace an
end-to-end audio test.

Review progress with:

```sh
rig powerloss status --campaign CAMPAIGN
```

Any failed run prevents campaign completion. Preserve its evidence and diagnose it; do
not simply delete or relabel the run.

### Pixel car-chaos profile

Use the compact Pixel profile after clean cold-boot timing has passed and before image
acceptance. It preserves the same backup/recovery-card safety gate while reducing the
campaign to five high-risk cuts whose exact schedule is recorded before the first cut:

- one cut one second after power-on;
- one seeded cut three to seven seconds after power-on;
- one seeded cut 12 to 18 seconds after power-on, during service and Bluetooth startup;
- one seeded cut during Bluetooth recovery; and
- one seeded cut during a synchronized persistent-state write.

Create the campaign with a recorded seed, then arm its cases in order:

```sh
rig powerloss campaign-init \
  --profile pixel-chaos \
  --seed 20260901 \
  --safety-evidence /safe/larkbridge-safety-evidence.json
rig powerloss arm-next --campaign CAMPAIGN
```

For early-boot cases, follow the printed `early-start`, `observe-off`, and `reconnect`
commands. For the two running-system cases, follow the printed `observe-off` and
`reconnect` commands. The profile enforces a 12-second cold-off interval. Every recovery
must pass the full read-only/storage/service/config/pairing acceptance probe, preserve
the exact pairing identity, report the Pixel connected with no active repair transaction,
and show a boot-to-Pixel connection time of at most 25 seconds. `arm-next` refuses to
advance past an active or failed case, so mistimed cuts remain visible rather than being
silently replaced.

If an armed sequence is interrupted without a physical cut, close it explicitly with
`rig powerloss abort --campaign CAMPAIGN --reason REASON`. The run remains failed and
auditable; begin a fresh campaign after correcting the interruption.

## Final acceptance

After 50 passing cuts, shut down, remove the card, and run
`verify-offline-device.sh` again with a new output file. Acceptance requires:

- zero boot/root corruption;
- zero lost configuration, pairing, or SSH identity;
- automatic `READY` or tested `DEGRADED` recovery after every cut;
- no manual filesystem repair during the campaign;
- no service, graph, AEC lifecycle, journal, thermal, voltage, or restart-loop regression;
- functional call audio after recovery whenever the far-end test is available.

Keep the full-card backup, the verified recovery card, campaign evidence, and final fsck
output together as the release record.
