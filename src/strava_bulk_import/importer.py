"""Import orchestration: identity lookup, fenced claims, one upload, durable bind."""

from __future__ import annotations

import json
import secrets
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .archive import SPORTS, Activity, Archive, ImportProblem
from .client import APIError, Client
from .journal import Journal

PREFIX = "/api/external-identities/v1"


def identity_path(kind: str, external_id: str) -> str:
    return f"{PREFIX}/{kind}/strava/{quote(external_id, safe='')}"


def profile(client: Client) -> tuple[str, str]:
    response = client.request("GET", "/api/auth/profile")
    assert response is not None
    data = response["data"]
    # Username + account creation separates replacement accounts with the same name.
    namespace = client.base + "|" + data["username"] + "|" + data["created_at"]
    return namespace, data.get("timezone") or "UTC"


@dataclass
class Prepared:
    metadata: dict[str, Any]
    recording: tuple[str, bytes] | None
    photos: list[tuple[str, bytes, str]]


def prepare(
    archive: Archive,
    activity: Activity,
    sports: dict[str, int],
    timezone: str,
    overrides: dict[str, str],
    include_photos: bool,
) -> Prepared:
    label = overrides.get(activity.sport, SPORTS.get(activity.sport))
    if label not in sports:
        raise ImportProblem("No active FitTrackee sport mapping for " + activity.sport)
    sport_id = sports[label]
    recording = archive.recording(activity)
    metadata = activity.metadata(sport_id) if recording else activity.manual(sport_id, timezone)
    if recording:
        metadata.update(analysis_visibility="private", map_visibility="private")
    for field, limit in [("title", 255), ("description", 10000), ("notes", 500)]:
        if len(metadata[field]) > limit:
            raise ImportProblem(f"{field} exceeds FitTrackee's length limit ({limit})")
    photos = archive.photos(activity) if include_photos else []
    return Prepared(metadata, recording, photos)


def active_sports(client: Client) -> dict[str, int]:
    response = client.request("GET", "/api/sports")
    assert response is not None
    return {
        s["label"]: s["id"]
        for s in response["data"]["sports"]
        if s["is_active"] and s.get("is_active_for_user", True)
    }


def bind(client: Client, journal: Journal, key: str, entry: dict[str, Any]) -> None:
    client.request(
        "PUT",
        identity_path("identities", key),
        headers={"X-Identity-Claim": entry["token"]},
        json={"workout_id": entry["workout_id"]},
    )
    entry["phase"] = "bound"
    journal.save(key, entry)


def upload(client: Client, journal: Journal, activity: Activity, prepared: Prepared) -> str:
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
        entry = dict(token=secrets.token_urlsafe(32), phase="reserved", media_ids=[])
        journal.save(key, entry)  # Secret must survive a lost claim response.
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
    media_ids = []
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
        media_ids.append(media["id"])
        entry["media_ids"] = list(media_ids)
        journal.save(key, entry)
        if caption:
            client.request(
                "PATCH", "/api/media/" + quote(media["id"], safe=""), json={"description": caption}
            )
    metadata = {**prepared.metadata, "media_attachment_ids": media_ids}
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
    workouts = (response or {}).get("data", {}).get("workouts", [])
    if len(workouts) != 1 or not isinstance(workouts[0].get("id"), str):
        raise ImportProblem("Expected one workout; upload outcome requires operator recovery")
    entry["workout_id"] = workouts[0]["id"]
    journal.save(key, entry)  # A bind retry must never repeat the native upload.
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
) -> Counter[str]:
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
        config = response["data"]
        if config.get("version") != "1.3.6":
            raise ImportProblem("This importer currently supports FitTrackee 1.3.6 only")
    counts: Counter[str] = Counter()
    selected = [a for a in archive.activities if not activity_ids or a.external_id in activity_ids]
    if activity_ids - {a.external_id for a in selected}:
        raise ImportProblem("One or more selected activity IDs are absent from the archive")
    for activity in selected[:limit]:
        try:
            if client:
                mapped = client.request(
                    "GET", identity_path("identities", activity.external_id), missing_ok=True
                )
                if mapped:
                    counts["skipped"] += 1
                    emit(f"{activity.external_id}: skipped (identity already mapped)")
                    continue
            if journal and not dryrun:
                entry = journal.get(activity.external_id)
                if entry and entry.get("workout_id"):
                    assert client is not None
                    bind(client, journal, activity.external_id, entry)
                    counts["recovered"] += 1
                    emit(f"{activity.external_id}: recovered mapping")
                    continue
            prepared = prepare(archive, activity, sports, timezone, overrides, include_photos)
            if config:
                if (
                    prepared.recording
                    and len(prepared.recording[1]) > config["max_single_file_size"]
                ):
                    raise ImportProblem("Recording exceeds target file-size limit")
                if any(len(photo[1]) > config["max_image_size"] for photo in prepared.photos):
                    raise ImportProblem("Photo exceeds target image-size limit")
            if dryrun:
                status = "would_import" if client else "candidate"
                kind = "recording" if prepared.recording else "manual"
                emit(f"{activity.external_id}: {status} ({kind}, {len(prepared.photos)} photos)")
            else:
                assert client is not None and journal is not None
                status = upload(client, journal, activity, prepared)
                emit(f"{activity.external_id}: {status}")
            counts[status] += 1
        except ImportProblem as exc:
            counts["blocked"] += 1
            emit(f"{activity.external_id}: blocked ({exc})")
            # Stop on server errors rather than compounding authentication or quota failures.
            if isinstance(exc, APIError) and exc.status not in {409}:
                break
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
        dict(
            token=secrets.token_urlsafe(32),
            phase="reserved",
            media_ids=[],
            previous_media_ids=entry.get("media_ids", []),
        ),
    )
