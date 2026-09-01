# Closed-loop boot optimization

`rig boot` measures boot time from outside the Pi and refuses to equate an open SSH port with a
working appliance. Each run records the new boot identity, required system and user services, a
powered Bluetooth adapter, the bridge health report, and a stable Lark endpoint.

## Safety levels

- **Idle readiness** proves that the unattended appliance control and audio layers are healthy.
  It is useful for preliminary profiling but cannot accept an optimization.
- **Functional readiness** additionally runs the bench-specific call/audio hook. Only functional
  results can produce `PROVISIONAL_ACCEPT`, and the recovery/soak gates are still required.
- **Phone readiness** is a deliberately narrower cold-boot campaign. It requires the exact BT500,
  both local phone-profile UUIDs, a closed pairing surface, the configured Pixel connected, an
  idle repair state, read-only hardened mounts, and a watchdog connection timestamp no later than
  25 seconds after power-on. Ten passing baseline and candidate cycles are required.

The controller refuses to time a checkout with tracked modifications. Untracked operator notes
are recorded in the manifest but do not invalidate a run.

## Inventory

The following optional flat keys belong in the ignored `rig/inventory.toml`:

```toml
boot_probe_host = "192.168.0.251" # direct TCP target; pi_host may remain an SSH alias
boot_ssh_timeout_seconds = 8
boot_shutdown_timeout_seconds = 30
boot_timeout_seconds = 120
boot_cold_off_seconds = 10

# Commands are argument arrays, never shell strings. Tokens shown below are substituted.
boot_power_off_command = []
boot_power_on_command = []
boot_serial_capture_command = [] # may use {output}, {run_dir}, {run_id}
boot_functional_probe_command = [] # may use {run_dir}, {run_id}, {candidate}
boot_variant_apply_command = [] # may use {candidate}, {revision}, {run_dir}, {run_id}
```

Automated cold runs are refused until both power commands exist. `--manual-power` instead prompts
the operator to turn real car power off, waits for SSH to disappear and the configured cold-off
interval, then prompts for power on. Serial capture and the functional hook are bench-specific so
the generic controller does not guess a relay protocol, COM port, call service, or far-end
endpoint.

## Commands

```bash
rig boot doctor
rig boot run --mode warm --candidate baseline
rig boot baseline --mode warm --candidate baseline --count 20 --require-functional
rig boot baseline --mode cold --candidate baseline --count 10 --require-functional
rig boot baseline --mode cold --candidate phone-baseline --count 10 \
  --manual-power --readiness-profile phone
rig boot compare --baseline baseline --candidate candidate-name
rig boot compare --baseline baseline --candidate candidate-name --mode warm
rig boot compare --baseline phone-baseline --candidate phone-candidate \
  --allow-phone --mode cold
rig boot screen --baseline baseline --baseline-rev REV --candidate candidate-name \
  --candidate-rev REV --pairs 10 --mode warm --require-functional
rig boot trial status
```

Artifacts are written under `artifacts/boot-run-*` and include the event timeline, manifest,
pre-boot and ready probes, systemd timing, unit state, watchdog startup/reconnect state, and the
complete boot journal. Full logs stay ignored; accepted experiment summaries belong in a later
curated report.

## Current limitation

The repository does not yet define the physical relay, UART adapter, or automated far-end call
endpoint. Until those hooks are configured and `--require-functional` passes, results are
preliminary warm-reboot/idle-readiness measurements only.

The functional command must write `functional-result.json` in `{run_dir}`. Schema version 1
requires the matching `run_id`, `pass=true`, `call_active=true`, zero dropouts, explicit
`feedback_detected=false`, and matching detected `{watermark}` proofs for both
`lark_to_far_end` and `far_end_to_output`. A successful process exit alone never proves readiness.

## Transactional trials

`scripts/install.sh --boot-only` records every managed pre-image under
`/var/lib/rpi-lark-bridge/boot-transactions`. Candidate deployment should arm the installed trial
timer; a run confirms it only after readiness. An unconfirmed candidate restores its transaction
after 120 seconds and reboots. Kernel, firmware, Device Tree, partition, and filesystem candidates
remain prohibited until out-of-band recovery exists.

The physical-Pi variant hook currently supports `baseline`, `netplan-fastpath`, and
`netplan-skip`. The latter two remain explicit experiment modes; ordinary `make install` keeps the
deployed NetworkManager behavior unchanged. See
[`boot-optimization-results-2026-08-19.md`](boot-optimization-results-2026-08-19.md) for the
provisional warm-boot result and promotion gates.
