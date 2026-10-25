"""Typed data contracts; on-disk JSON field names remain backward compatible."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, NotRequired, Protocol, TypeAlias, TypedDict, cast

from .errors import ImportProblem

Outcome: TypeAlias = Literal[
    "candidate", "would_import", "imported", "recovered", "skipped", "excluded", "blocked"
]
UploadOutcome: TypeAlias = Literal["imported", "recovered", "skipped"]
JournalPhase: TypeAlias = Literal["reserved", "beginning", "uploading", "bound", "releasing"]
HTTPMethod: TypeAlias = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
Stage: TypeAlias = Literal[
    "selecting",
    "checking identity",
    "preparing files",
    "claiming",
    "uploading photos",
    "uploading workout",
    "binding identity",
]
Recording: TypeAlias = tuple[str, bytes]
Photo: TypeAlias = tuple[str, bytes, str]
JSONObject: TypeAlias = dict[str, object]


class WorkoutMetadata(TypedDict):
    sport_id: int
    title: str
    description: str
    notes: str
    equipment_ids: list[str]
    workout_visibility: Literal["private"]
    media_visibility: Literal["private"]
    analysis_visibility: NotRequired[Literal["private"]]
    map_visibility: NotRequired[Literal["private"]]
    distance: NotRequired[float]
    duration: NotRequired[int]
    workout_date: NotRequired[str]
    media_attachment_ids: NotRequired[list[str]]


class OAuthClientCredentials(TypedDict):
    server: str
    client_id: NotRequired[str]
    client_secret: NotRequired[str]


class Credentials(OAuthClientCredentials):
    access_token: str
    expires_at: float
    refresh_token: NotRequired[str]


class JournalEntry(TypedDict):
    token: str
    phase: JournalPhase
    workout_id: NotRequired[str]
    media_ids: NotRequired[list[str]]
    previous_media_ids: NotRequired[list[str]]


class ProgressReporter(Protocol):
    """Presentation hooks; the importer never depends on a terminal or Rich."""

    def start(self, total: int) -> None: ...
    def activity(self, external_id: str, stage: Stage) -> None: ...
    def advance(self, outcome: Outcome) -> None: ...


def object_value(value: object) -> JSONObject:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ImportProblem("Expected a JSON object")
    return cast(JSONObject, value)


def string_value(value: object) -> str:
    if not isinstance(value, str):
        raise ImportProblem("Expected a string field")
    return value


def list_value(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ImportProblem("Expected a JSON array")
    return cast(list[object], value)


def int_value(value: object) -> int:
    if type(value) is not int:
        raise ImportProblem("Expected an integer field")
    return value


def journal_entry(value: object) -> JournalEntry:
    data = object_value(value)
    string_value(data.get("token"))
    phase = data.get("phase")
    if not isinstance(phase, str) or phase not in {
        "reserved",
        "beginning",
        "uploading",
        "bound",
        "releasing",
    }:
        raise ImportProblem("Invalid journal phase; preserve the journal for investigation")
    for key in ("media_ids", "previous_media_ids"):
        if key in data:
            for item in list_value(data[key]):
                string_value(item)
    if "workout_id" in data:
        string_value(data["workout_id"])
    return cast(JournalEntry, data)


def credential_fields(value: Mapping[str, object]) -> OAuthClientCredentials:
    result: OAuthClientCredentials = {"server": string_value(value.get("server"))}
    if "client_id" in value:
        result["client_id"] = string_value(value["client_id"])
    if "client_secret" in value:
        result["client_secret"] = string_value(value["client_secret"])
    return result
