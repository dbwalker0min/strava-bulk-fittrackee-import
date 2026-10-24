# Importer development

This is the separate Strava export importer. Package/CLI: strava-fittrackee.
The HA app and its provider-independent identity API live in dbwalker0min-hass-apps;
do not copy app deployment code or superseded PR #32 importer code into this project.

Use Python 3.11+, typed code and uv. Checks: `uv run pytest -q`,
`uv run ruff check src tests`, `uv run ruff format --check src tests`,
`uv run mypy src`, `uv build`. CI uses generated fixtures only.

Never commit private data/, credentials, journals, media, export contents or logs.
Do not upload personal activities as a development test. Live verification uses a
dedicated test account and synthetic fixtures, with exact-ID cleanup only.

Dryrun must make no server mutations, token refreshes or journal/state writes.
Server identities are authoritative for skipping. Preserve exact (user, strava,
external_id) claims and opaque activity IDs. No automatic cross-provider matching,
merging, duplicate reporting or reconciliation features.

Persist claim secrets before acquisition, and returned workout IDs before binding.
Never retry native creation after an uncertain result. Never auto-release an
uploading claim. Operator recovery must be explicit and journaled. Keep the target
origin/account boundary and refuse HTTP/redirect credential forwarding.

Maintain README.md with actual verification and known upstream limits. Local unit
checks are not live FitTrackee/HA verification. Keep scope within this repository.
