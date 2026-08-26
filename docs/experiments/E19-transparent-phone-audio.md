# E19 — Can the appliance carry Pixel media and microphone audio transparently, not only during calls?

- **Status:** In progress — Step 1 of 13 complete; feasibility not yet established
- **Resolves risk:** phone audio is usable only inside a call; media playback is not deterministically routed to the selected output
- **Gates milestone:** transparent phone audio release
- **Owner / date:** Claude (runtime model `claude-opus-5`), 2026-08-26

## Question

Can the LarkBridge appliance present the operator-selected microphone to a Pixel **while the phone
is idle** — outside a call — and simultaneously route the phone's A2DP media playback to the
configured aux output, without regressing the existing Lark/FIFINE selection, liveness, hotplug,
AEC, and break-before-make guarantees?

The question has three separable parts, and they must not be conflated:

1. Does phone **media** (A2DP) reach the configured output deterministically?
2. Does the phone accept a **microphone** during a call (HFP/SCO)? — believed yes today.
3. Does the phone accept a **microphone while idle**, with no call and no communication-mode
   application? — **unknown, and the crux of this experiment.**

## Why it cannot be answered by reading

Whether Android exposes a Bluetooth microphone transport outside a call is decided by the Android
audio policy on the handset, not by BlueZ, PipeWire, or this repository. BlueZ can report an HFP
profile as *connected* while Android never opens an SCO transport, so repository source and even
PipeWire graph state can show a microphone "selected" that the phone is not consuming. Only live
observation of the Pixel across its real states can distinguish an available transport from a
merely-advertised profile.

## User objective

Phone audio should pass through the appliance transparently in both directions: media and
notification audio out to the aux speaker, and the selected microphone available to the phone —
ideally at all times, not only when a call is in progress.

## Baseline and deployed state

| Item | Value |
|---|---|
| Development baseline | `3c119c572f5998103bc89425980f11c40c96f7da` (`docs(image): record retention review`) |
| Baseline branch | `codex/fifine-k054-compat` |
| Deployed commit on the Pi | `03df47e8486b99ba741d65949b83557f983d4e33` (`feat(microphone): prefer live Lark transmitters`) |
| Deployed branch | `codex/lark-transmitter-liveness` |
| Deployed profile | `pi3-usb-bt500-aux`, 498 manifest entries |
| Deployed archive SHA-256 | `4fa9e8a73c7bc95921fd735bfb2506ce05fca5963a6a96da815d144785ea3e97` |

`03df47e` is an ancestor of the baseline, and the baseline carries the image-retention record, so
the working tree is a strict superset of what is deployed.

## Protected existing behavior

The following must survive every checkpoint unchanged. Any of them regressing is a stop condition,
not a trade-off:

- Lark transmitter **liveness** detection — a receiver present with no live transmitter is not
  eligible.
- **Lark-first, FIFINE-second** priority ordering.
- Hotplug switching, including same-name replug generation changes.
- AEC, with post-AEC `bridge.mic` ownership.
- **Break-before-make** routing.
- Fail-closed handling of ambiguous microphone identity.
- No direct physical-microphone-to-phone bypass path.

## Workspace

| Path | Role |
|---|---|
| `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge-transparent-phone-audio` | **Work location** for all 13 checkpoints; branch `codex/transparent-phone-audio` |
| `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge` | Primary checkout — holds unrelated dirty E18 evidence. **Do not touch.** |
| `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge-fifine-k054` | FIFINE worktree — holds unrelated dirty E18 evidence. **Do not touch.** |
| `...\rpi_lark_mic_bridge\.claude\worktrees\e19-transparent-phone-audio-68f529` | Harness session worktree at `59c7f89` (= `master`). **Not the work location**; it predates the baseline and lacks E15–E18 and the retention record. |

The untouchable E18 evidence is, verbatim: modified `docs/experiments/E18-fifine-k054-compat.md`
and `rig/bt500_aux/microphone_hotplug.py`, plus untracked
`docs/experiments/results/E18/field/hotplug-20260826T145218Z-e8b78232a230/` and
`docs/experiments/results/E18/field/hotplug-20260826T152038Z-823abcf04751/`.

Raw disk images and Bluetooth pairing credentials must never enter Git.

## Bluetooth feasibility gate

The requested "microphone always available" behavior may be impossible on Android. These are
treated as open gates, not assumptions, until Step 3 measures them:

- Android may expose **no** Bluetooth microphone transport outside a call or a communication-mode
  application. A selected microphone on the Pi is **not** evidence that Android is consuming it.
- A2DP and HFP may both remain *connected* while only one may be *active*, forcing a transport
  switch rather than true full duplex.
- Persistent SCO, if achievable at all, may satisfy microphone availability only by degrading media
  to narrowband and displacing A2DP.
- Android — not the Pi — decides whether paused media resumes and where ringtones and some
  notifications play. The Pi restoring a route is not the same as the phone resuming playback.
- Continuous full-duplex playback may require a different AEC reference path than the call path.

If exact transparent full-duplex Bluetooth proves impossible, Step 4 must present the alternatives
(A2DP + call-time microphone, persistent SCO, USB Audio Class, or a phone-side application) and
**stop at `DECISION_REQUIRED`**. No later checkpoint may infer that choice.

## Image-preservation requirement

Step 11 is the only checkpoint authorized to mutate the Pi, and it may not deploy until the current
appliance is preserved:

- Capture a **consistent full-card raw image** — quiesce audio, Bluetooth, watchdog and persistence
  writers, `sync`, remount `LARKDATA` read-only, stream the whole card, and restore read-write and
  the services through a guaranteed cleanup path.
- Hash the complete image (SHA-256) and record size, source device, partition layout, boot ID,
  deployed manifest, configuration hash, stopped services, and restoration status.
- Store the image **outside Git** (~15 GiB, contains device secrets) and update
  [`docs/image-retention-2026-08-26.md`](../image-retention-2026-08-26.md).
- **Delete no older image.** In particular the `c63c823` image may not be retired until the new
  image has been captured, hashed, restored, and booted.

## Checkpoints

| # | Checkpoint | Status |
|---|---|---|
| 1 | Establish the isolated workspace | **COMPLETE** |
| 2 | Static Bluetooth and audio architecture audit | Not started |
| 3 | Live Pixel and Pi capability characterization | Not started |
| 4 | Feasibility verdict and approved contract | Not started — expected `DECISION_REQUIRED` |
| 5 | Encode the routing contract as tests | Not started |
| 6 | Implement deterministic idle media routing | Not started |
| 7 | Implement safe A2DP/HFP transitions | Not started |
| 8 | Integrate the approved idle microphone behavior | Not started |
| 9 | Status, CLI, policy, installer, and documentation | Not started |
| 10 | Independent local regression and safety review | Not started |
| 11 | Preserve the baseline image and deploy | Not started |
| 12 | Abbreviated live acceptance test | Not started |
| 13 | Final audit and release verdict | Not started |

## Step 1 — Establish the isolated workspace

### Deviation from the written checkpoint

Step 1 required verifying that branch `codex/transparent-phone-audio` and the sibling worktree did
**not** already exist, and stopping if either did. **Both existed.** They were inspected rather than
reused, and found to be a pre-created but never-used workspace:

- `git log --oneline codex/fifine-k054-compat..codex/transparent-phone-audio` — empty (no unique
  commits).
- `git -C <path> status --porcelain --ignored=matching --untracked-files=all` — empty (no tracked,
  untracked, or ignored files; no `rig/inventory.toml`).
- `git reflog show codex/transparent-phone-audio` — a single entry,
  `branch: Created from codex/fifine-k054-compat`. The ref had never moved.
- Both refs resolved to the identical SHA `3c119c572f5998103bc89425980f11c40c96f7da`.

The operator was shown this evidence and directed that the stale branch and worktree be **removed
and re-created fresh** from the resolved baseline, so that Step 1 executes as written and leaves no
stale artifact. That authorization is the sole basis for the deletion below.

`git branch -d` refused with *"not fully merged"*. That refusal was an artifact of comparing against
the harness session's `HEAD` (`59c7f89`, which is an *ancestor* of the baseline), not evidence of
unique work: the branch pointed at the same commit as `codex/fifine-k054-compat`, which still
carries it. Deletion was therefore performed with `-D` behind an explicit SHA-equality guard.

A second deviation is recorded for transparency: the branch keeps the `codex/` prefix at the
operator's direction, although commit `43aba28` established that Claude-authored work belongs on
`claude/` branches. The work in this experiment is Claude-authored.

### Commands and results

| Command | Result |
|---|---|
| `git worktree remove B:/.../rpi_lark_mic_bridge-transparent-phone-audio` | removed; path no longer exists |
| `git branch -d codex/transparent-phone-audio` | **refused** — "not fully merged" (see above) |
| SHA-equality guard on both refs | passed — both `3c119c5...` |
| `git branch -D codex/transparent-phone-audio` | `Deleted branch codex/transparent-phone-audio (was 3c119c5)` |
| `git worktree add -b codex/transparent-phone-audio B:/.../rpi_lark_mic_bridge-transparent-phone-audio 3c119c5` | created; 499 files checked out |
| `git -C <new> rev-parse HEAD` | `3c119c572f5998103bc89425980f11c40c96f7da` |
| `git -C <new> merge-base --is-ancestor 03df47e HEAD` | exit 0 — deployed commit is an ancestor |
| `docs/image-retention-2026-08-26.md`, `E18-fifine-k054-compat.md`, `TEMPLATE.md` present in new worktree | all present |
| `ssh -o BatchMode=yes -o ConnectTimeout=10 larkbridge 'hostname; uptime'` | `larkbridge`, up 42 min, load 0.00 |
| `ssh larkbridge 'cat /etc/larkbridge/DEPLOYED.json'` | `commit 03df47e...`, branch `codex/lark-transmitter-liveness`, profile `pi3-usb-bt500-aux`, 498 manifest entries, archive SHA-256 `4fa9e8a7...` |
| FIFINE worktree `git status --porcelain` before and after | identical — 2 modified, 2 untracked directories; evidence untouched |

No production code, no Pi state, and no configuration were changed by this checkpoint. The only
Pi access was read-only SSH.

### Decisions

- Work proceeds in the re-created sibling worktree on `codex/transparent-phone-audio` at `3c119c5`.
- The harness session worktree is documentation-only for this experiment and is not the work
  location.
- The stale workspace was deleted under explicit operator authorization and proven-lossless guards.

### Findings that change later checkpoints

- **The Pi is reachable.** `docs/image-retention-2026-08-26.md` records that the Pi was not
  reachable by its saved hostname, last IPv4 address, or last link-local IPv6 address, so that
  record claims no current image or hash. That status is now stale: `larkbridge` answered over SSH
  on 2026-08-26 and reports the expected deployed commit. The retention record is **not** edited
  here — Step 11 owns it, and Step 11 must correct that status line when it captures the image. The
  plan's "Pi was unreachable" gate no longer blocks Step 3.
- **`rig/inventory.toml` is absent** from this worktree and from every sibling except the primary
  checkout. Step 3 must create it from `rig/inventory.toml.example` before any rig-driven live
  capture. It is gitignored, uses LF endings, and must not be converted to CRLF. Prefer the stable
  `pi_host` over the arrangement-specific `pi_ip`.
- `docs/architecture/decisions/` ends at **ADR-0008**, so Step 4 creates **ADR-0009**.

### Remaining risks

- The feasibility gate above is entirely unmeasured. Nothing in this checkpoint constitutes
  evidence that idle-state microphone transparency is achievable.
- Live Pi work in Steps 3, 11 and 12 depends on continued reachability and on the operator being
  present for physical phone interaction.

### Next action

**Step 2 — static Bluetooth and audio architecture audit.** Read-only inspection of BlueZ
configuration and advertised roles, PipeWire/WirePlumber Bluetooth policy, HFP call detection and
graph construction, A2DP handling, output selection, watchdogs and reconnection, across
`bridge_supervisor.py`, `microphones.py`, `outputs.py`, status generation, CLI, systemd units and
the release installer. Produce an architecture map and a requested-versus-possible capability
matrix. No implementation changes.
