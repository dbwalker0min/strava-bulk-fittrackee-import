# strava-fittrackee

Import a Strava account export ZIP into FitTrackee using its HTTPS APIs. The Python
package and command are `strava-fittrackee`; this repository retains its original
`strava-bulk-fittrackee-import` name. Requires Python 3.11 or newer.

Initial release targets **FitTrackee 1.3.6 with the Home Assistant app's external
identity API v1 (app 0.1.2+)**. It is not compatible with an unextended upstream
FitTrackee installation. No direct database access or Strava API access is used.

Development handoff: [PROJECT_STATUS.md](PROJECT_STATUS.md),
[ARCHITECTURE.md](ARCHITECTURE.md), [CHANGELOG.md](CHANGELOG.md) and
[AGENTS.md](AGENTS.md). Start with PROJECT_STATUS when continuing on another machine.

## Install and inspect

With [uv](https://docs.astral.sh/uv/):

```sh
uv sync --dev
uv run strava-fittrackee import data/strava-export.zip --dryrun
```

Or install this checkout with `pip install .`. This package has not been published
to PyPI. Keep private exports under the ignored `data/` directory.

`--dryrun` (also `--dry-run`) reads recordings and attachment references and reports
candidates, scope exclusions, already mapped activities, and blockers. Offline inspection needs no
credentials. Add `--server https://fittrackee.example` to check actual sports and
identities with existing credentials. Online dryruns send **GET requests only**:
no workouts, photos, claims, token refreshes, journal writes, or state directories
are created. An expired token requires a separate `refresh` or `login` command.
Claims cannot be inspected without a mutation; an unbound identity in a dryrun is
a candidate, not a guarantee that another client has no outstanding claim.

Output contains activity IDs and status, not titles, descriptions, GPS or tokens.
Exit status: `0` no blockers, `1` one or more activities blocked, `2` configuration,
archive or operational error, `130` interrupted. Blocked activities are not silently
counted as imported. A server error stops the run; the summary covers processed rows.

## Authentication

In FitTrackee, open **Profile → Apps** and register a client with:

- Redirect URI: `http://127.0.0.1:8765/callback`
- Scopes: `profile:read workouts:read workouts:write media:write`
- Authorization-code and refresh-token grants (FitTrackee's normal app defaults).

Then run:

```sh
uv run strava-fittrackee login --server https://fittrackee.example --client-id YOUR_CLIENT_ID
```

The command prompts privately for the client secret, opens the FitTrackee browser
consent page, validates a random OAuth state and exchanges the code with PKCE S256.
Its callback listens only on `127.0.0.1`, for up to five minutes. The callback is
local HTTP; all requests to FitTrackee require HTTPS with normal certificate
validation. Use `--port` if needed and register the matching redirect URI.

FitTrackee 1.3.6 issues 10-day access tokens and refresh tokens valid for up to 20
days from their issuance. Refresh rotates the tokens and revokes the previous
credential. Import/recovery commands refresh automatically when needed; after a
long idle period, log in again. Refresh explicitly with:

```sh
uv run strava-fittrackee refresh --server https://fittrackee.example
```

Credentials and the SQLite recovery journal default to `~/.strava-fittrackee/`.
Use `--state-dir PATH` consistently on every command to select another private
directory. Credentials are saved atomically; directories/files use 0700/0600 on
POSIX. On Windows, use a directory protected by your account's NTFS permissions.
The directory contains secrets and personal IDs: keep it out of Git and shared
folders, and preserve it between runs. Each state directory stores credentials for
one server; journal entries are additionally scoped to server and target account.
One mutating command at a time can use a state directory. A different directory
still coordinates through server claims.

## Import

The default scope is **outdoor cycling history for planning rides**. Non-cycling
activities are reported as `excluded`, rather than import failures. Use
`--all-sports` to include other sports. Indoor/virtual activities are separately
excluded unless `--include-indoor` is supplied. Both switches work in dryrun and
actual imports; `--activity-id` and `--sport-map` do not override these filters.

Indoor classification uses explicit virtual/indoor activity types, true
Trainer/Indoor/Virtual CSV flags when supplied, and FIT `sub_sport` metadata
(`indoor_cycling` or `virtual_activity`). A ride without coordinates or a recording
is **not** assumed to be indoors: outdoor trackless rides remain eligible for
manual creation. Unlabelled indoor rides cannot be identified reliably; inspect
the dryrun before importing. No title guessing is used. Exclusions create no claim,
identity, photo or workout and do not remove anything previously imported.

Start with a small selection, then rerun without the selection:

```sh
uv run strava-fittrackee import data/strava-export.zip --server https://fittrackee.example --dryrun
uv run strava-fittrackee import data/strava-export.zip --server https://fittrackee.example --limit 1
uv run strava-fittrackee import data/strava-export.zip --server https://fittrackee.example
```

`--limit N` selects the first N CSV rows, including skips/blockers; repeat
`--activity-id ID` to select exact activities. `--no-photos` omits attachments.
Reruns skip any identity already registered as `(target user, strava, Activity ID)`.
They do not update metadata or add omitted photos to those workouts. Other
providers remain independent. No GPS/date/title matching or cross-provider merging
is performed. Pre-existing workouts without Strava identities are not inferred.

Supported data:

- GPX, FIT and TCX, including gzip-compressed recordings. Members are bounded and
  read in memory; the ZIP is never extracted. Leading whitespace before an XML
  declaration in older TCX/GPX exports is removed from the uploaded copy. Last duplicate CSV columns are used,
  matching Strava's numeric SI fields: distance in metres, durations in seconds.
- Recordings with no coordinates, and activities without a recording, use native
  manual creation with distance and moving duration (elapsed duration if moving is
  absent). Activity Date is interpreted as UTC and converted to the target profile
  timezone. The native manual endpoint has minute precision; ambiguous DST times
  are blocked rather than guessed. Offline previews use UTC or `--timezone ZONE`.
- Titles, descriptions and private notes. New workouts and media are private;
  recording maps and analysis are private. Explicitly empty equipment lists prevent
  attaching unrelated target default gear. Equipment creation/mapping is deferred.
- Up to 20 JPEG/PNG photos per activity, with captions from `media.csv`. Caption,
  image-size and upload validation are also subject to the target server's limits.
  Online preflight checks the advertised image and recording byte limits before
  obtaining a claim; offline inspection cannot check target settings.

Sport mappings use live active FitTrackee labels, never hardcoded sport IDs. Rides
(including E-Bike Ride and Velomobile) default to Cycling (Sport); Virtual Ride uses
Cycling (Virtual). Walking, hiking, running, trail, mountain biking, canoeing,
kayaking, rowing and alpine/nordic skiing also have defaults. To override, provide
`--sport-map map.json`, for example `{"Ride":"Cycling (Transport)"}`. Offline
inspection cannot verify that your chosen labels are active on the target.

### Known initial-release limits

FitTrackee 1.3.6's manual endpoint rejects numeric zero distance/duration. Such
workouts are blocked without changing the distance or fabricating a recording.
With `--all-sports`, Weight Training, Workout, Yoga and Sail have no default sport equivalent; choose
an explicit compatible sport mapping if appropriate. `.bin` recordings, videos,
missing/corrupt referenced files, overlong metadata and excess attachments are
reported as blockers. No arbitrary metadata/recording fallback is used for an
unsupported file. Some file-level validation is performed by FitTrackee only.

Private gallery settings do not authenticate static photo URLs in FitTrackee 1.3.6;
anyone who knows an exact media URL can fetch it. This is the existing accepted
family-deployment behavior, not changed by this importer.

## Interrupted runs and recovery

The importer durably saves a random claim token before requesting a reservation,
then calls `begin-upload` once. It uploads photos and one native workout, saves the
returned workout ID, and binds the identity with the same claim token. A failed
bind retries using the saved ID without creating another workout. Expired unused
reservations receive a new token; uploading claims never expire automatically.

An uncertain native/photo upload or lost begin-upload response blocks that
activity on rerun. It does not grant permission for another upload. Inspect the
activity in FitTrackee and the private journal. If the exact native workout exists:

```sh
uv run strava-fittrackee recover --server https://fittrackee.example --activity-id STRAVA_ID --workout-id FITTRACKEE_ID
```

The supplied workout must belong to the authenticated account. The operator is
responsible for verifying that it is the exact activity from the uncertain upload.
This is same-identity failure recovery, not cross-provider matching.

Only after confirming **no earlier upload exists or can still complete**, release
an unresolved claim explicitly:

```sh
uv run strava-fittrackee recover --server https://fittrackee.example --activity-id STRAVA_ID --confirm-no-upload
```

This sends the API's reconciliation acknowledgement and rotates the journal token.
It does not delete anything. Interrupted releases must be repeated before importing.
Known orphan photo IDs remain in the journal for operator inspection; uncertain
photo responses may leave additional unknown orphans. No automatic deletion, claim
stealing or upload retry is performed. Lost journal tokens require administrator
investigation. Claims cannot provide exactly-once creation across independent HTTP
transactions. Keep the journal and back up the target before a full migration.

## Development and verification

```sh
uv sync --dev
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv build
```

Tests use synthetic exports and an HTTP transport double to verify API payloads,
read-only behavior, token refresh, claims and lost-response recovery. CI runs on
Linux and Windows with Python 3.11 and 3.14. No personal archives or credentials
are needed in CI. Offline inspection of the real archive is separate from a live
import: this release still needs an end-to-end OAuth/upload trial against the
dedicated FitTrackee test account before importing personal data.

API contracts verified against [FitTrackee v1.3.6 source](https://github.com/SamR1/FitTrackee/tree/v1.3.6)
and the app's [external identity API v1](https://github.com/dbwalker0min/dbwalker0min-hass-apps/blob/main/fittrackee/EXTERNAL_IDENTITIES_API.md).
