# Making the Pi's state knowable, consistent, and revertible

Status: plan. Nothing here is implemented yet.
Author / date: Claude / 2026-08-22

## Why

On 2026-08-22 an audit of eleven deployed files found five that did not match the repository,
drifting in **both** directions:

| File | Finding |
|---|---|
| `10-bridge-clock.conf` | `quantum = 2048` on the Pi; **never committed anywhere**. Hand-edited 2026-08-16 21:57, four hours after that day's last commit. |
| `60-bridge-lark-format.conf` | Pi one commit **stale**; carried documentation the project had already disproved. |
| `bridge-btfw.service`, `bridge-tuning.service`, `set-sco-routing.sh` | Matched `codex/boot-optimization`, not the branch the supervisor came from. |

Two conclusions follow, and they are the whole reason for this document.

**The unit is a hybrid.** Its system layer is `codex/boot-optimization`; its user layer is
`codex/aec-crackle-diagnosis`. No single commit describes the running appliance. Notably,
master's HEAD commit — `refactor(bluetooth): make SCO routing verification-only` — is *not*
deployed, so any Bluetooth work on this unit tests routing behaviour master has already replaced.

**A silent revert was one command away.** `quantum = 2048` is the value E10's clean playback
control and E11's control were both measured against. Anyone redeploying that file from git
would have dropped the graph to 1024 and invalidated both baselines with no error and no
symptom. It is committed now, but only because someone went looking.

Three test campaigns are about to run — AEC closed-loop validation, power-loss hardening, and
fault injection. All three assume a known starting state, and two of them assume the ability to
return to it. Neither assumption is currently supported by anything but care.

## The state surface, in tiers

Tiering matters because the tiers want different treatment. Conflating them is why ad-hoc
backups fail.

**Tier 1 — versioned artifacts.** Belong in git, deployed to the Pi. Drift here is either a bug
or an uncommitted discovery.

- `~/rpi-lark-bridge/**`
- `~/.config/pipewire/pipewire.conf.d/`, `~/.config/wireplumber/wireplumber.conf.d/`
- `~/.config/systemd/user/*.service`
- `/etc/systemd/system/bridge-*.{service,timer}`, `/etc/bluetooth/main.conf.d/`
- `/usr/local/lib/rpi-lark-bridge/`
- `/boot/firmware/config.txt`, `cmdline.txt`, device-tree overlays, udev rules, tmpfiles.d

**Tier 2 — per-unit config.** Deliberately not in git; must be captured.

- `~/rpi-lark-bridge/config/bridge.toml`
- `rig/inventory.toml` (on the host; gitignored, LF endings)

**Tier 3 — learned state.** Expensive or impossible to recreate. This is the tier worth the most
and the one most likely to be lost.

- `/var/lib/bluetooth/**` — **the pairing database. Losing it means physically re-pairing the
  Pixel.** Highest value on the unit.
- `~/.local/state/wireplumber/` — `default-routes` holds sink volume and route selection
- `/var/lib/alsa/asound.state` — mixer
- `/etc/NetworkManager/system-connections/` — including the sticky `larkbridge-direct` profile
- SSH host keys (identity; matters when reimaging)

**Tier 4 — ephemeral, derived.** Capture for *diagnosis*; never restore.

- `/run/user/1000/bridge-status.json`, `pw-dump`, `pw-link -l`, loaded modules, journal

## Proposed tool: `rig/statectl.py`

Runs from the host over SSH. Stdlib only on any part that executes on the Pi — there is no
numpy, no ffmpeg and no virtualenv there.

| Command | Purpose |
|---|---|
| `manifest --commit <sha>` | Record commit, branch, dirty flag and per-file hashes for tiers 1–2 into `/etc/larkbridge/DEPLOYED.json`. Written by the deploy path. |
| `verify` | Recompute and compare against the manifest *and* a named commit. Reports drift in both directions, names the file, exits non-zero. **Step zero for every test campaign.** |
| `snapshot --label <name>` | Bundle tiers 2–3 with a checksum manifest; capture tier 4 alongside for diagnosis. |
| `restore --label <name>` | Restore tiers 2–3, bounce exactly the affected services in dependency order, verify afterwards. |
| `list`, `diff <label>` | Inspect what exists and what changed since. |

### Details that decide whether this works

**Restoring state under a running daemon does not work.** BlueZ and WirePlumber hold their state
in memory and rewrite it on exit; restoring files beneath them gets silently clobbered on the
next shutdown. Restore must stop the owning daemon first. This is the single most likely way a
naive implementation fails while appearing to succeed.

**Restore must declare its own blast radius.** Mapping:

| Restored | Requires |
|---|---|
| `bridge.toml` | restart `bridge-supervisor.service` (user) |
| `pipewire.conf.d/*` | restart `pipewire` — `bridge-supervisor` is `PartOf=pipewire.service` and follows |
| `wireplumber.conf.d/*`, `~/.local/state/wireplumber/` | stop `wireplumber`, restore, start |
| `/var/lib/bluetooth` | stop `bluetooth.service`, restore, start |

Restarting PipeWire **is itself a perturbation**. A fault-injection run must be told when a
restore disturbed the graph, or it will mistake recovery noise for a finding.

**Verify must reach outside the repo tree.** Every drifted file found in the audit lived in
`~/.config` or `/etc`. A manifest covering only `~/rpi-lark-bridge` would have caught none of them.

**Do not trust the Pi's clock for labels.** The Pi 3B has no RTC and has no NTP on the direct
cable, so wall-clock timestamps after a reboot are wrong. Pass timestamps in from the host or use
a monotonic counter — the same trap `powerlossctl` already works around.

**Snapshots must not live only on the SD card.** That card is precisely what power-loss testing
is trying to corrupt. Mirror to the host under `artifacts/state/`, gitignored.

**Verify before restore.** Restoring tier 2–3 onto a tier 1 that does not match the expected
commit produces config for the wrong code version. Refuse it.

## Interaction with power-loss hardening — read before building

`codex/power-loss-hardening` moves the appliance to a **read-only root with a tmpfs overlay**,
where tier 1 becomes an immutable image and only `LARKDATA` is writable. Under that design:

- Tier 1 becomes verify-only; "revert" means "boot a different image".
- Tiers 2–3 must live on `LARKDATA`, which is where that branch already puts them.
- **That branch already implements alternating, checksummed snapshots of the BlueZ pairing
  database and of `bridge.toml`, with recovery from the alternate slot.**

So `statectl` must **reuse `bridge-storage-guard` and the `LARKDATA` slot mechanism rather than
building a second, competing snapshot system.** Build the host-side manifest/verify and the
orchestration; delegate on-Pi durable storage to the power-loss design. Getting this wrong means
two mechanisms fighting over the same files.

This is also an argument for building `verify` first and deferring `snapshot`/`restore` until the
power-loss merge lands — or for scoping snapshot/restore to a thin wrapper from the outset.

## Phasing

**Phase 1 — `manifest` + `verify` (small).** Closes the drift hole. Gives all three campaigns a
step zero: *"the unit is commit X on the system layer, commit Y on the user layer, with no
drift."* Wire it into `rig/tests/doctor.sh` and into `scripts/install.sh` on the power-loss
branch, which is where deploys already happen.

**Phase 2 — golden baseline snapshot (small).** One blessed known-good bundle captured now, host-
mirrored, so there is something to return to even before `restore` is automated. Cheap insurance
that does not depend on Phase 3.

**Phase 3 — `snapshot` / `restore` (medium).** The daemon-stop ordering and the blast-radius
reporting are the real work, not the file copying. Required by the fault-injection campaign,
which cannot attribute failures without a reliable return to baseline.

**Not proposed:** git on the Pi (thrown away by the read-only-root merge, and uninstallable on the
direct cable — no internet), filesystem snapshots (conflicts with the same design, on a 1 GB
machine), and full SD imaging for per-test iteration (correct for power-loss work, far too slow
to iterate against; the power-loss safety boundary already mandates it).

## Known gap

The 2026-08-22 audit covered eleven files. **Not yet checked:** boot overlays,
`/boot/firmware/config.txt` and `cmdline.txt`, udev rules, tmpfiles.d, and the remaining contents
of `/usr/local/lib/rpi-lark-bridge/`. The drift list above is a lower bound, not a complete
inventory. Phase 1 should enumerate the full surface rather than encoding today's partial list.
