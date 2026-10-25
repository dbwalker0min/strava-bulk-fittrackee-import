# Project status

Updated: 2026-10-24 (America/Los_Angeles).

## Purpose and decisions

Build a history of **outdoor cycling rides, electric and non-electric**, to help
plan future rides. Indoor/virtual cycling and non-cycling activities are excluded
by default. An absent recording or GPS track does not prove a ride was indoors.

Repository: https://github.com/dbwalker0min/strava-fittrackee

- Package/command: `strava-fittrackee`; Python module: `strava_fittrackee`.
- Initial version: 0.1.0 in development, not published to PyPI.
- Development branch: `codex/initial-importer`.
- [PR #1](https://github.com/dbwalker0min/strava-fittrackee/pull/1)
  targets `main` and was verified open when this handoff was written.
- Baseline before the Typer CLI refactor: `c754c58`.
- Current work uses Typer commands/options with Rich progress on the same
  branch/PR; consult Git history for the exact current revision.
- Target contract: FitTrackee **1.3.6** plus the HA app's external identity API v1
  (app 0.1.2+). The client currently rejects other upstream versions.
- The HA app/API and future incremental synchronizer are separate projects. Do
  not copy deployment code or superseded HA-app PR #32 importer code here.

## Implemented

- Bounded ZIP/CSV reading, GPX/FIT/TCX and gzip support, and a narrow repair for
  leading whitespace before older exports' XML declarations. Originals stay intact.
- Cycling scope, including e-bikes; `--all-sports` and `--include-indoor` opt-ins.
  Explicit indoor types/CSV flags and FIT sub-sport metadata drive exclusions.
- Native recording uploads or supported manual workout creation; titles,
  descriptions, private notes, JPEG/PNG photos and captions. Imports default private.
- Server identity lookup skips already mapped activities; reruns do not update
  those workouts or add omitted photos. No cross-provider matching or merging.
- Offline `--dryrun` and authenticated GET-only dryrun. Neither refreshes tokens,
  reserves claims, creates state files nor uploads anything.
- Browser OAuth authorization-code login with PKCE, loopback callback and automatic
  refresh during mutating commands. Client registration is still a manual UI step.
- Private SQLite recovery journal, fenced claims, explicit known-workout recovery
  and operator-confirmed release. Uncertain native creation is never auto-retried.
- Typer command signatures, generated help, option validation and typed command
  values. Existing commands, flags, aliases and recovery choices are preserved.
- Typed workout/credential/journal contracts, constrained states and outcomes,
  JSON boundary validation and strict mypy source checks.
  Existing state JSON/SQLite formats remain compatible.
- Rich terminal progress for imports/dryruns: selected/processed count, percentage,
  stage, elapsed time and ETA. Redirected output and `--no-progress` stay plain;
  interrupts/early stops never force completion.
- Linux/Windows CI on Python 3.11 and 3.14, plus development instructions in
  [AGENTS.md](AGENTS.md) and user commands in [README.md](README.md).

## Naming update (2026-10-24)

Repository, distribution and command use `strava-fittrackee`. The intended
checkout folder name is also `strava-fittrackee`, but Windows denied renaming
the existing local `strava-bulk-fittrackee-import` folder. Close processes using
that checkout, rename it to `strava-fittrackee` and reopen the project. Recreate
the ignored `.venv` with `uv venv --clear` and `uv sync --locked --dev` after
moving the checkout so installed command paths use the new location.
The source package uses `src/strava_fittrackee/` and Python imports use
`strava_fittrackee` because Python identifiers cannot contain hyphens. The old
`strava_bulk_import` module name is removed; update external Python imports.
Existing credentials/journals and CLI commands are unchanged.

Verified locally on Python 3.14: 113 synthetic tests, Ruff lint/format, strict
mypy, wheel/sdist builds and both command/module help entry points. This is
local verification only; live verification remains pending as described below.

## Verification and limits

The Typer/Rich CLI passes **113 synthetic tests locally on Python 3.11
and 3.14**, Ruff lint/format, strict mypy (including Windows platform analysis) and
wheel/sdist builds. Terminal and redirected-output CLI smoke checks used a generated
archive. Added regression coverage invokes public Typer commands and verifies flag/alias
validation, recovery exclusivity before state access, progress counting, interruption/early
stop cleanup, read-only behavior and legacy credential/journal compatibility.

Historical CI baseline `cdd2ef0` passed 65 tests and all checks across Linux/Windows
with Python 3.11/3.14. The same workflow now runs the expanded suite and strict
checks; consult current PR checks for the latest commit's result. These checks do
not prove live OAuth, native uploads, HA runtime behavior or production readiness.

The actual export was inspected locally without uploading personal data. A complete
offline scan before cycling/indoor filters found 2,909 candidates and 155 blockers,
including 248 manual candidates. **Those counts are historical and do not describe
the current defaults.** The subsequent full scan with FIT indoor classification was
stopped before completion; current filtered totals and scan performance remain to
be measured. A real FIT sample and synthetic trailing-session metadata were checked.

Remaining limitations:

- FitTrackee 1.3.6 rejects zero-distance/duration manual workouts. Do not fabricate
  distance or tracks to bypass this. Manual dates have minute precision; ambiguous
  DST times are blocked.
- Unlabelled indoor rides cannot be reliably identified. Missing GPS alone is not
  an exclusion. FIT classification reads through trailing metadata, so scan time
  deserves measurement on a realistic archive.
- Unsupported recordings (`.bin`), videos, missing photos, excess attachments and
  overlong metadata are blockers when in scope. `--no-photos` omits attachments;
  skipped mapped workouts will not receive them on a later rerun.
- Equipment migration is deferred. Optional non-cycling sports may need explicit
  mappings; see README. E-bike Ride maps to Cycling (Sport) by default, while
  E-Mountain Bike Ride maps to Mountain Biking (Electric).
- Claims coordinate clients but cannot guarantee exactly-once creation across
  separate HTTP transactions. Unknown upload outcomes need operator recovery.
- Upstream static photo URLs are accessible to anyone who knows the exact URL.
  The user accepted this behavior for the family deployment; do not reopen that
  decision without a material change or user request.

## Continue on another machine

Read this file, [AGENTS.md](AGENTS.md), [ARCHITECTURE.md](ARCHITECTURE.md),
[CHANGELOG.md](CHANGELOG.md), then README. Run these commands from your chosen
parent directory; all subsequent paths are relative to the repository:

```sh
git clone https://github.com/dbwalker0min/strava-fittrackee.git
cd strava-fittrackee
git fetch origin
git switch codex/initial-importer
git pull --ff-only
git status --short --branch
uv sync --locked --dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv build
```

If PR #1 has merged, switch to updated `main` instead. Check the actual branch and
PR status before starting new work. Python 3.11+ is supported; `.python-version`
selects 3.14 for local development. With an existing checkout, fetch/switch/pull
rather than cloning over local changes. The same commands work in PowerShell.

Private inputs and state **do not travel with Git**:

- Place a securely transferred Strava export at `data/strava-export.zip`; that
  directory is ignored. No absolute machine-specific paths belong in instructions.
- Default state is `.strava-fittrackee` under the current user's home directory.
  `--state-dir PATH` overrides it. It holds `credentials.json`, `journal.sqlite3`
  and a process lock file. Never commit it or upload it as a CI artifact.
- To resume an unfinished import elsewhere, stop mutating commands on the old
  machine, then securely transfer the entire quiescent state directory, preserving
  its journal and any SQLite sidecar files. Restrict access to the destination user.
  Do not copy only credentials or discard unresolved claim tokens. The process lock
  is local; it does not coordinate two machines using separate copies.
- Refresh rotates credentials. Do not alternate between stale credential copies.
  Fresh login is appropriate when tokens have expired; preserve the recovery
  journal for the same target account. Without it, uncertain claims can require
  administrator investigation. See README for recovery and account/origin scoping.

Safe offline preview from the repository root:

```sh
uv run strava-fittrackee import data/strava-export.zip --dryrun --limit 10
```

A dryrun with `--server` requires existing valid credentials. Register the OAuth
client and use the documented login command before online work. No client secret,
access token or personal archive is included in this repository.

## Next work

1. Register an OAuth client and verify login/refresh against the dedicated
   FitTrackee test account using synthetic fixtures. Live authentication is
   implemented but **not yet verified**.
2. Verify a synthetic recording and manual workout, photo/caption upload, identity
   binding and rerun skipping over HTTPS. Test interruption/recovery using exact
   fixture IDs; do not use personal activities for development verification.
3. Complete a current-scope offline scan and measure FIT processing time. Review
   exclusions/blockers and any desired sport overrides with the user.
4. Before a personal import, confirm target account/origin and protected target
   backups; start with a small explicit selection. No personal upload has occurred.
5. Review/merge the PR and decide publication/versioning separately. Do not publish
   to PyPI or change the HA deployment as part of routine client development.

Update this status after meaningful work, separating local/CI results from actual
live verification. Record user-facing changes in CHANGELOG and design changes in
ARCHITECTURE.
