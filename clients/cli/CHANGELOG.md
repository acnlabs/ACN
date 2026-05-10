# Changelog

All notable changes to `@acnlabs/acn-cli` are documented here.

## [0.7.0] - 2026-05-10

### Added
- `acn pay` — create a payment task from the configured agent to a
  named seller agent, with `--amount`, `--currency`, `--method`,
  `--network`, `--description`, and `--metadata` flags. The
  `from_agent` field is derived from the local CLI config so callers
  cannot accidentally spoof a different sender.
- `acn wallet tasks` — list payment tasks where the configured agent
  is buyer or seller, with optional `--limit`. Output formatted as a
  human-readable table.
- `acn wallet stats` — show payment statistics for the configured
  agent (counts and totals split by buyer vs seller role, plus
  per-status breakdown).
- `acn wallet estimate` — estimate the cost of calling another
  agent's service before invoking it, taking
  `--input-tokens` / `--output-tokens` for token-priced agents.

### Changed
- The `acn-client` SDK dependency series now tracks the v0.7.x line.
  The CLI handles the v0.7.0 breaking changes (lowercase
  `PaymentMethod` / `PaymentNetwork` enum values, renamed
  `PaymentTask` fields) so end-users do not need to.
- Aligned with `acn-client` v0.7.1, which fixes the `PaymentStats`
  shape returned by `acn wallet stats`.

## [0.6.3] - 2026-05-09

### Changed
- Coordinated patch release matching the `acn-client` SDK.

## [0.6.2] - 2026-05-07

### Changed
- Initial publication under the `@acnlabs/acn-cli` package name.
