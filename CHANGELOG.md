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
