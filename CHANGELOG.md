# Changelog

All notable changes are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/). Each completed milestone from `PLAN.md` §8 lands as an
entry under Unreleased, and each spike lands with a link to its experiment report.

## [Unreleased]

### Added
- `PLAN.md` — full architecture, feasibility assessment, repository design, milestones, test matrix,
  risk register and acceptance criteria.
- M0 repository skeleton: build façade, linting, CI workflows, ADRs 0001–0008.
- Spike protocols S1–S3 as runnable scripts with pre-written experiment reports.
- Deterministic PipeWire/WirePlumber/BlueZ configuration fragments.
- A strict USB-BT500 call-controller plus wired-AUX release profile, call-only exact-interface
  watchdog, deterministic AUX volume verification, and no-autolink ownership policy.
- A resumable five-cycle and 3,600-second `bt500-aux` qualification harness with full evidence
  capture, hard-failure monitoring, call recycle proof, and SHA-256 manifests.
- Exact-commit release packaging and guarded immutable-lower-root installation, including an
  explicit transactional gate for persistent onboard-Bluetooth disablement.

### Live qualification (provisional)
- Passed the two-minute AEC-disabled USB-BT500 HFP/SCO transport gate with wired AUX output.
- Passed objective AEC measurements: two acceptance-eligible double-talk cycles reached 16.09 dB
  and 12.69 dB measured echo suppression. Two additional echo-only diagnostics reached 37.47 dB
  and 37.95 dB but are not credited as double-talk cycles.
- Stopped and deferred the remaining interactive cycles at the user's direction after provisional
  AEC acceptance. The original five-cycle gate therefore remains incomplete.
- Attempted a 3,600-second active-call pre-persistence soak, but it correctly refused to start
  after the Pixel became unavailable and the opening state was `CALL_DOWN`.
- Persisted exact commit `c63c823ab9d6dfa1837b05517f903d25c6b5c96a` from release archive
  `LarkBridge-bt500-aux-c63c823ab9d6-20260825T212148Z.zip` (SHA-256
  `c085897770cdd54d1a9ff1b39c688cc6ef2dbbd9e41e9f79e87379505075c1d3`) after verifying all
  450 manifest entries.
- The first promoted reboot exposed an idle-only AUX readiness defect: `CALL_DOWN` left the
  required `0.85` volume unobserved and unverified. Commit `c63c823` corrected deterministic volume
  application and read-back without requiring an active call.
- Passed the corrective second-boot baseline under boot ID
  `925eb01c-f17d-4cea-96ee-d505f20db7a3`: immutable lower root and boot filesystem read-only, only
  the exact USB-BT500 controller present, strict `CALL_DOWN` readiness at AUX `0.85`, and all
  required services active with zero restarts. Total boot time was 18.132 seconds, so the Netplan
  fast path was retained.
- A fresh Pixel-dependent post-reboot call and the final 3,600-second active-call soak remain
  deferred. This is not a full release qualification, and only two qualifying double-talk cycles
  are complete.

### Decisions
- **ADR-0001** Three operating modes; Mode 1W (Bluetooth call + wired output) is the default until
  spike S3 proves single-radio HFP+A2DP coexistence.
- **ADR-0002** Routing is declarative via `module-loopback`, not an imperative link daemon.
  `pw-link` is banned from production code.
- **ADR-0003** `bridged` is Python; nothing in the sample path is.
- **ADR-0004** The Pi is the I2S clock master; the Pico is a PIO slave.
- **ADR-0005** UAC2 primary, UAC1 fallback, decided empirically at milestone M9.3.
- **ADR-0006** PipeWire runs in a lingering user session, never system-wide.
- **ADR-0007** Fixed 48 kHz internal graph.
- **ADR-0008** PlatformIO + arduino-pico is the primary firmware build; CMake + pico-sdk is the CI
  reference build; firmware sources stay Arduino-API-free so both compile identical code.

### Known open risks
- **R1/R2** — single-radio HFP+A2DP viability and SCO-over-HCI on the BCM43438 are unresolved until
  spikes S1 and S3 run on hardware. Both can change the shape of the project.

[Unreleased]: https://github.com/OWNER/rpi-lark-bridge/compare/HEAD...HEAD
