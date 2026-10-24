# Architecture

## Boundaries

`strava-fittrackee` is a local Python CLI that reads a Strava account export ZIP
and calls FitTrackee over authenticated HTTPS. It is not part of the HA app image,
does not call Strava's API and never accesses FitTrackee's database directly.

The server owns native workouts/media and the provider-independent external
identity API. This client owns archive interpretation, cycling scope, payloads,
credentials and durable recovery state. Target: FitTrackee 1.3.6 with external
identity API v1. Upgrade compatibility requires explicit verification.

## Modules

| Module under `src/strava_bulk_import/` | Responsibility |
| --- | --- |
| `cli.py` | Typer commands/options, typed command values, resource lifecycle and exit codes |
| `archive.py` | Bounded ZIP/CSV reads, activity metadata, date/unit conversion, recording inspection, indoor signals, photos/captions |
| `importer.py` | Scope filters, live sports/config preflight, identity lookups, upload/claim orchestration and recovery |
| `client.py` | HTTPS origin checks, non-following redirects, Bearer requests, token refresh and atomic credential saving |
| `auth.py` | Browser consent, random OAuth state, PKCE S256, loopback callback and code exchange |
| `journal.py` | SQLite persistence and local OS file lock |
| `models.py` | TypedDict payload/credential/journal contracts, Literal states, aliases, JSON validation and progress protocol |
| `errors.py` | Shared domain errors and intentional exclusion type |
| `progress.py` | Rich presentation and terminal/redirect handling |
| `__main__.py` | Module execution entry point |

Tests use synthetic archives, valid generated FIT messages and an HTTP transport
double. They do not require a private export or credentials. CI validates Linux
and Windows on Python 3.11 and 3.14.

## Type contracts

Source checks run in mypy strict mode. Workout metadata, OAuth credentials and
journal entries use `typing.TypedDict`; optional wire fields use `NotRequired`.
`Literal` types constrain HTTP methods, journal phases, outcomes and progress
stages. Recording/photo tuples have shared type aliases.

Typer derives command parameters, validation and help from annotated function
signatures. Shared server/state option annotations keep the four commands
consistent. Command functions pass a typed CommandOptions dataclass to the
executor; recovery's single activity ID remains separate from import's repeatable
selection. Recovery requires exactly one of workout binding or confirmed release,
validated before credentials, locks or journals are opened. Error handling keeps
established exit codes and sanitizes domain failures; Typer's pretty exception
rendering is disabled so credentials/locals cannot appear in diagnostic panels.
Tests invoke the public application through typer.testing.CliRunner.

HTTP/JSON values enter as `object` and are validated before use. Journal reads
check recovery fields before returning a typed entry; token reads/refresh validate
strings and finite numeric expiry values. State still serializes with the original
JSON keys and SQLite layout. These annotations do not require moving existing
credentials or journals. The untyped XML/FIT libraries are isolated at their import
boundaries; domain code does not use unrestricted `Any` dictionaries.

## Selection and preflight

Default selection is outdoor cycling, electric and non-electric. `--all-sports`
relaxes the cycling filter; `--include-indoor` separately allows explicitly indoor
or virtual activities. An exact activity selection or custom sport map does not
bypass scope. Indoor signals come from explicit activity types, true CSV flags,
and FIT sub-sport metadata. FIT inspection continues past GPS records to inspect
trailing session/lap/sport metadata. No title guessing or GPS-absence inference.

In-scope mapped identities are skipped before file parsing. For remaining rows,
the archive reader prepares a recording or manual payload and requested photos.
A manual workout is used only for an absent recording or a supported file without
coordinates, not an unsupported/corrupt file. Metadata limits and advertised target
upload sizes are checked before claim acquisition. Live active sport labels resolve
to IDs; offline previews cannot verify target availability or existing mappings.

ZIP members are never extracted. Reading/decompression is bounded; XML uses
`defusedxml`, FIT uses `fitdecode`. Only leading whitespace before an XML declaration
is normalized, and the uploaded bytes match the inspected copy. Source files stay
unchanged. Duplicate CSV headers use their last values, matching Strava's numeric
SI columns. Manual distance is converted from metres to kilometres; UTC activity
dates are converted to the target profile timezone for its native manual endpoint.

## Import transaction and failure model

The authoritative key is `(authenticated target user, "strava", external_id)`.
IDs remain strings. Different providers remain independent; no timestamp, GPS,
distance or title matching is performed. Native APIs create workouts/media; the
companion API binds an existing workout to its external key.

For an unbound activity:

1. Persist a fresh random claim token locally before acquiring a server reservation.
2. Acquire/renew the reservation; rotate the token if an unused reservation expired.
3. Persist the beginning phase and call `begin-upload`. Only an affirmative first
   authorization permits native creation. A repeated begin is not fresh permission.
4. Persist uploading state, upload photos/captions and journal known media IDs.
5. Upload one native recording or manual workout, then durably save its returned ID.
6. Bind that ID using the same claim token and mark the local entry bound.

A saved workout ID permits a bind retry without another native upload. An uncertain
photo/workout response or lost begin response leaves a non-expiring uploading
claim requiring investigation. There is no automatic native-write retry or claim
stealing. Known orphan media IDs are retained; an uncertain photo response may
leave an unknown orphan. Nothing is automatically deleted.

`recover --workout-id` binds an operator-verified existing workout after ownership
validation. `recover --confirm-no-upload` releases only after the operator confirms
no prior upload exists or can still complete. The releasing phase is saved first;
a lost release response blocks normal import until explicit recovery is repeated.
A confirmed release rotates the token. This is exact-identity failure recovery,
not cross-provider reconciliation or a guarantee of exactly-once native creation.

## Local state and authentication

The default state directory is under the user's home: `.strava-fittrackee/`.
Credentials are origin-bound and saved atomically. Journal entries are keyed by
server origin, target username/account creation timestamp and external activity ID.
SQLite commits use `synchronous=FULL`. A local advisory file lock serializes login,
refresh, import and recovery in one state directory. Server claims coordinate
separate clients; copying directories does not extend the local lock across hosts.

OAuth requires a manually registered FitTrackee client with `profile:read`,
`workouts:read`, `workouts:write`, `media:write`. Authorization uses PKCE and random
state; callback HTTP is restricted to `127.0.0.1`. Remote traffic requires HTTPS
with normal certificate verification. Redirects are not followed. Client secrets,
access/refresh tokens and claim secrets never belong in logs or Git.

POSIX state directories/files use 0700/0600 permissions; Windows relies on the
user's directory ACLs. Tokens rotate on refresh. Moving unfinished work requires
securely transferring the stopped client's complete journal/state, not just the
source checkout. See [PROJECT_STATUS.md](PROJECT_STATUS.md) for the portable handoff.

## Progress presentation

The core importer accepts an optional `ProgressReporter` protocol and has no Rich
or terminal dependency. The CLI owns a RichReporter context manager and passes
its hooks to the importer. Selected IDs and `--limit` determine the total. Each
completed outcome advances once, including skip/exclusion/blocked paths; an
interrupted in-progress activity does not advance. API early stops leave remaining
rows unprocessed. Progress callbacks never authorize an upload or replace the
journal; claim/recovery ordering remains authoritative.

Stage events cover selection, identity checks, file inspection, claiming, photo
upload, native workout creation and binding. Rich displays count/percentage,
elapsed time, ETA and current ID/stage. The context manager stops the live display
on exceptions and leaves actual completion visible instead of forcing 100%.

Interactive display/messages use stderr and the final summary uses stdout.
Redirecting either stream disables the bar, preserving plain per-activity stdout
logs with immediate flushing. `--no-progress` forces plain reporting. Rich markup
is disabled for activity messages so external text is rendered literally. Tests
exercise terminal and nonterminal paths without personal data or network access.

## Dryrun and reporting

Offline dryrun creates no client credentials, journal or state directory. Online
dryrun allows GET only and refuses token refresh. Neither path creates claims,
workouts or photos. A dryrun cannot inspect outstanding server claims because the
claim API has no read-only lookup; unbound candidates are not upload guarantees.

`excluded` means intentional scope filtering; `skipped` means an existing identity;
`blocked` means an import problem. Neither exclusions nor skips count as failures.
The CLI reports IDs/status without personal titles, GPS or credential bodies.
A non-conflict API error stops processing; summaries describe processed rows.
See README for exit codes and current limits, and PROJECT_STATUS for verification.
