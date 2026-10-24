# Changelog

## Unreleased

Initial 0.1.0 implementation; not yet published to PyPI.

### Added

- `strava-fittrackee` Python package/CLI, with import, login, refresh and recovery
  commands; Python 3.11+ support and uv-based development.
- Strava ZIP/CSV reading, GPX/FIT/TCX and gzip support, and normalization of leading
  whitespace before older XML declarations without modifying source archives.
- Outdoor cycling defaults for both electric and non-electric rides; explicit
  indoor/virtual and non-cycling exclusions, with `--include-indoor` and
  `--all-sports` opt-ins. Missing GPS alone does not exclude an outdoor ride.
- Supported trackless/manual workouts, titles, descriptions, private notes,
  JPEG/PNG photos and captions; private import visibility and optional photo omission.
- Offline `--dryrun` / `--dry-run` and authenticated GET-only previews; separate
  candidate, exclusion, identity-skip and blocker reporting.
- Native OAuth browser login with PKCE and loopback callback, automatic refresh
  during mutating commands and protected local credential storage.
- Exact Strava-identity skipping, fenced claims, durable SQLite recovery journal
  and explicit operator recovery without automatic uncertain-upload retries.
- Target-version/upload-size preflight, active sport-label mapping, exact activity
  selection and import limits.
- Rich terminal progress for imports/dryruns, including selected/processed count,
  percentage, current stage, elapsed time and ETA. `--no-progress` and redirected
  output retain plain logs; early stops/interruption retain partial completion.
- Typed workout/credential/journal models, constrained states/outcomes and a
  progress protocol, with strict mypy source checks and JSON boundary validation.
  Existing saved credentials/journals need no migration.
- 93 synthetic tests and Linux/Windows CI on Python 3.11 and 3.14.
- Cross-machine handoff in PROJECT_STATUS.md, design in ARCHITECTURE.md and
  development guardrails in AGENTS.md.

### Fixed

- Platform-specific locking branches now type-check correctly on Windows and POSIX.

### Known limits

- Requires FitTrackee 1.3.6 with the HA app's external identity API v1; live OAuth
  and synthetic end-to-end import verification remain pending.
- Upstream rejects zero-distance/duration manual workouts; unsupported formats,
  videos, missing media and invalid payloads are reported rather than fabricated.
- No equipment migration, updates to already mapped workouts, automatic
  cross-provider matching or automatic cleanup of uncertain uploads/media.
- Unlabelled indoor rides may remain eligible. Current-scope full-archive totals
  and FIT inspection performance have not yet been established.
