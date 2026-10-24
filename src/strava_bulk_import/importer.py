"""Import orchestration: identity lookup, fenced claims, one upload, durable bind."""

from __future__ import annotations

import json
import secrets
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from .archive import (
    RIDE_TYPES,
    SPORTS,
    Activity,
    Archive,
    indoor_reason,
)
from .client import APIError, Client
from .errors import ExcludedActivity, ImportProblem
from .journal import Journal
from .models import (
    JournalEntry,
    Outcome,
    Photo,
    ProgressReporter,
    Recording,
    UploadOutcome,
    WorkoutMetadata,
    int_value,
    list_value,
    object_value,
    string_value,
)

PREFIX = "/api/external-identities/v1"


def identity_path(kind: str, external_id: str) -> str:
    return f"{PREFIX}/{kind}/strava/{quote(external_id, safe='')}"


def profile(client: Client) -> tuple[str, str]:
    response = client.request("GET", "/api/auth/profile")
    assert response is not None
    data = object_value(response["data"])
    # Username + account creation separates replacement accounts with the same name.
    namespace = (
        client.base + "|" + string_value(data["username"]) + "|" + string_value(data["created_at"])
    )
    return namespace, string_value(data.get("timezone") or "UTC")


@dataclass
class Prepared:
    metadata: WorkoutMetadata
    recording: Recording | None
    photos: list[Photo]


def prepare(
    archive: Archive,
    activity: Activity,
    sports: dict[str, int],
    timezone: str,
    overrides: dict[str, str],
    include_photos: bool,
    include_indoor: bool = True,
) -> Prepared:
    label = overrides.get(activity.sport, SPORTS.get(activity.sport))
    if label not in sports:
        raise ImportProblem("No active FitTrackee sport mapping for " + activity.sport)
    sport_id = sports[label]
    recording = archive.recording(activity, include_indoor=include_indoor)
    metadata = activity.metadata(sport_id) if recording else activity.manual(sport_id, timezone)
    if recording:
        metadata["analysis_visibility"] = "private"
        metadata["map_visibility"] = "private"
    limits: list[tuple[Literal["title", "description", "notes"], int]] = [
        ("title", 255),
        ("description", 10000),
        ("notes", 500),
    ]
    for field, limit in limits:
        if len(metadata[field]) > limit:
            raise ImportProblem(f"{field} exceeds FitTrackee's length limit ({limit})")
    photos = archive.photos(activity) if include_photos else []
    return Prepared(metadata, recording, photos)


def active_sports(client: Client) -> dict[str, int]:
    response = client.request("GET", "/api/sports")
    assert response is not None
    return {
        string_value(s["label"]): int_value(s["id"])
        for item in list_value(object_value(response["data"])["sports"])
        for s in [object_value(item)]
        if s["is_active"] and s.get("is_active_for_user", True)
    }


def bind(client: Client, journal: Journal, key: str, entry: JournalEntry) -> None:
    client.request(
        "PUT",
        identity_path("identities", key),
        headers={"X-Identity-Claim": entry["token"]},
        json={"workout_id": entry["workout_id"]},
    )
    entry["phase"] = "bound"
    journal.save(key, entry)


def upload(
    client: Client,
    journal: Journal,
    activity: Activity,
    prepared: Prepared,
    progress: ProgressReporter | None = None,
) -> UploadOutcome:
    key = activity.external_id
    entry = journal.get(key)
    if entry and entry.get("phase") == "releasing":
        raise ImportProblem(
            "Interrupted claim release; repeat explicit recover --confirm-no-upload"
        )
    if entry and entry.get("workout_id"):
        bind(client, journal, key, entry)
        return "recovered"
    if entry and entry.get("phase") == "bound":
        raise ImportProblem("Mapping disappeared; inspect the recorded workout before retrying")
    if not entry:
        entry = JournalEntry(token=secrets.token_urlsafe(32), phase="reserved", media_ids=[])
        journal.save(key, entry)  # Secret must survive a lost claim response.
    if progress:
        progress.activity(key, "claiming")
    headers = {"X-Identity-Claim": entry["token"]}
    try:
        claim = client.request("PUT", identity_path("claims", key), headers=headers, json={})
    except APIError as exc:
        if exc.code != "claim_expired_use_new_token" or entry["phase"] != "reserved":
            raise
        entry["token"] = secrets.token_urlsafe(32)
        journal.save(key, entry)
        headers = {"X-Identity-Claim": entry["token"]}
        claim = client.request("PUT", identity_path("claims", key), headers=headers, json={})
    assert claim is not None
    if claim.get("state") == "bound":
        return "skipped"
    if claim.get("state") != "reserved" or claim.get("recovery_required"):
        raise ImportProblem("Uncertain earlier upload; use recover with its verified workout ID")
    entry["phase"] = "beginning"
    journal.save(key, entry)
    permission = client.request(
        "POST", identity_path("claims", key) + "/begin-upload", headers=headers
    )
    if not permission or permission.get("upload_authorized") is not True:
        raise ImportProblem("Upload not authorized; recovery required")
    entry["phase"] = "uploading"
    journal.save(key, entry)
    media_ids: list[str] = []
    if progress and prepared.photos:
        progress.activity(key, "uploading photos")
    for _name, content, caption in prepared.photos:
        media = client.request(
            "POST",
            "/api/media",
            files={
                "file": ("photo.png" if content.startswith(b"\x89PNG") else "photo.jpg", content)
            },
        )
        if not media or not isinstance(media.get("id"), str):
            raise ImportProblem("Photo upload outcome uncertain; inspect journal before recovery")
        media_id = string_value(media["id"])
        media_ids.append(media_id)
        entry["media_ids"] = list(media_ids)
        journal.save(key, entry)
        if caption:
            client.request(
                "PATCH", "/api/media/" + quote(media_id, safe=""), json={"description": caption}
            )
    metadata: WorkoutMetadata = {**prepared.metadata, "media_attachment_ids": media_ids}
    if progress:
        progress.activity(key, "uploading workout")
    if prepared.recording:
        filename, content = prepared.recording
        response = client.request(
            "POST",
            "/api/workouts",
            data={"data": json.dumps(metadata)},
            files={"file": (filename, content)},
        )
    else:
        response = client.request("POST", "/api/workouts/no_gpx", json=metadata)
    workouts = list_value(object_value((response or {}).get("data", {})).get("workouts", []))
    if len(workouts) != 1 or not isinstance(object_value(workouts[0]).get("id"), str):
        raise ImportProblem("Expected one workout; upload outcome requires operator recovery")
    entry["workout_id"] = string_value(object_value(workouts[0])["id"])
    journal.save(key, entry)  # A bind retry must never repeat the native upload.
    if progress:
        progress.activity(key, "binding identity")
    bind(client, journal, key, entry)
    return "imported"


def run(
    archive: Archive,
    *,
    client: Client | None,
    journal: Journal | None,
    dryrun: bool,
    timezone: str,
    overrides: dict[str, str],
    include_photos: bool,
    limit: int | None,
    activity_ids: set[str],
    emit: Callable[[str], None] = print,
    include_indoor: bool = False,
    all_sports: bool = False,
    progress: ProgressReporter | None = None,
) -> Counter[Outcome]:
    sports = (
        active_sports(client)
        if client
        else {
            label: i
            for i, label in enumerate(sorted(set(SPORTS.values()) | set(overrides.values())), 1)
        }
    )
    config = {}
    if client:
        response = client.request("GET", "/api/config")
        assert response is not None
        config = object_value(response["data"])
        if config.get("version") != "1.3.6":
            raise ImportProblem("This importer currently supports FitTrackee 1.3.6 only")
    counts: Counter[Outcome] = Counter()
    selected = [a for a in archive.activities if not activity_ids or a.external_id in activity_ids]
    if activity_ids - {a.external_id for a in selected}:
        raise ImportProblem("One or more selected activity IDs are absent from the archive")
    selected = selected[:limit]
    if progress:
        progress.start(len(selected))
    for activity in selected:
        outcome: Outcome | None = None
        if progress:
            progress.activity(activity.external_id, "selecting")
        try:
            if not all_sports and activity.sport not in RIDE_TYPES:
                raise ExcludedActivity("outside cycling scope (use --all-sports to include)")
            if not include_indoor and (reason := indoor_reason(activity)):
                raise ExcludedActivity(reason)
            if client:
                if progress:
                    progress.activity(activity.external_id, "checking identity")
                mapped = client.request(
                    "GET", identity_path("identities", activity.external_id), missing_ok=True
                )
                if mapped:
                    outcome = "skipped"
                    counts[outcome] += 1
                    emit(f"{activity.external_id}: skipped (identity already mapped)")
                    continue
            if journal and not dryrun:
                entry = journal.get(activity.external_id)
                if entry and entry.get("workout_id"):
                    assert client is not None
                    bind(client, journal, activity.external_id, entry)
                    outcome = "recovered"
                    counts[outcome] += 1
                    emit(f"{activity.external_id}: recovered mapping")
                    continue
            if progress:
                progress.activity(activity.external_id, "preparing files")
            prepared = prepare(
                archive, activity, sports, timezone, overrides, include_photos, include_indoor
            )
            if config:
                if prepared.recording and len(prepared.recording[1]) > int_value(
                    config["max_single_file_size"]
                ):
                    raise ImportProblem("Recording exceeds target file-size limit")
                if any(
                    len(photo[1]) > int_value(config["max_image_size"]) for photo in prepared.photos
                ):
                    raise ImportProblem("Photo exceeds target image-size limit")
            if dryrun:
                outcome = "would_import" if client else "candidate"
                kind = "recording" if prepared.recording else "manual"
                emit(f"{activity.external_id}: {outcome} ({kind}, {len(prepared.photos)} photos)")
            else:
                assert client is not None and journal is not None
                outcome = upload(client, journal, activity, prepared, progress)
                emit(f"{activity.external_id}: {outcome}")
            counts[outcome] += 1
        except ExcludedActivity as exc:
            outcome = "excluded"
            counts[outcome] += 1
            emit(f"{activity.external_id}: excluded ({exc})")
        except ImportProblem as exc:
            outcome = "blocked"
            counts[outcome] += 1
            emit(f"{activity.external_id}: blocked ({exc})")
            # Stop on server errors rather than compounding authentication or quota failures.
            if isinstance(exc, APIError) and exc.status not in {409}:
                break
        finally:
            if progress and outcome is not None:
                progress.advance(outcome)
    return counts


def release_confirmed(client: Client, journal: Journal, key: str) -> None:
    entry = journal.get(key)
    if not entry:
        raise ImportProblem("No local claim journal exists for this account/activity")
    if entry.get("workout_id"):
        raise ImportProblem("Journal already knows a workout; recover its mapping instead")
    if client.request("GET", identity_path("identities", key), missing_ok=True):
        raise ImportProblem("Identity is already mapped; no release is needed")
    entry["phase"] = "releasing"
    journal.save(key, entry)
    client.request(
        "DELETE",
        identity_path("claims", key),
        headers={"X-Identity-Claim": entry["token"], "X-Identity-Reconciled": "true"},
    )
    journal.save(
        key,
        JournalEntry(
            token=secrets.token_urlsafe(32),
            phase="reserved",
            media_ids=[],
            previous_media_ids=entry.get("media_ids", []),
        ),
    )
