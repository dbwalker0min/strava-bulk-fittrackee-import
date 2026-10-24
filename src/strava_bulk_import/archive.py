"""Read bounded ZIP members without extracting private export data."""

from __future__ import annotations

import csv
import gzip
import io
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from defusedxml import ElementTree  # type: ignore[import-untyped]

MAX_MEMBER = 64 * 1024 * 1024
MAX_CSV = 32 * 1024 * 1024
SPORTS = {
    "Ride": "Cycling (Sport)",
    "E-Bike Ride": "Cycling (Sport)",
    "Virtual Ride": "Cycling (Virtual)",
    "Velomobile": "Cycling (Sport)",
    "Walk": "Walking",
    "Hike": "Hiking",
    "Run": "Running",
    "Trail Run": "Trail",
    "Mountain Bike Ride": "Mountain Biking",
    "Canoe": "Canoeing",
    "Kayaking": "Kayaking",
    "Rowing": "Rowing",
    "Alpine Ski": "Skiing (Alpine)",
    "Nordic Ski": "Skiing (Cross Country)",
}


class ImportProblem(Exception):
    """A safe, user-facing explanation without credentials or response bodies."""


def number(value: str, name: str) -> float:
    try:
        result = float(value or "0")
    except ValueError as exc:
        raise ImportProblem(f"Invalid {name}") from exc
    if not math.isfinite(result) or result < 0:
        raise ImportProblem(f"Invalid {name}")
    return result


def strava_date(value: str) -> datetime:
    # Strava export Activity Date is UTC; the native manual endpoint uses profile timezone.
    months = {
        m: i
        for i, m in enumerate(
            ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
        )
    }
    match = re.fullmatch(
        r"([A-Za-z]{3}) (\d{1,2}), (\d{4}), (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)", value
    )
    if match:
        month, day, year, hour, minute, second, period = match.groups()
        h = int(hour)
        if month not in months or not 1 <= h <= 12:
            raise ImportProblem("Invalid activity date")
        try:
            return datetime(
                int(year),
                months[month],
                int(day),
                h % 12 + (12 if period == "PM" else 0),
                int(minute),
                int(second),
                tzinfo=UTC,
            )
        except ValueError as exc:
            raise ImportProblem("Invalid activity date") from exc
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result
    except ValueError as exc:
        raise ImportProblem("Invalid activity date") from exc


@dataclass(frozen=True)
class Activity:
    external_id: str
    row: dict[str, str]
    media: tuple[str, ...]

    @property
    def sport(self) -> str:
        return self.row["Activity Type"]

    @property
    def recording(self) -> str:
        return self.row.get("Filename", "").strip()

    def metadata(self, sport_id: int) -> dict[str, Any]:
        return dict(
            sport_id=sport_id,
            title=self.row.get("Activity Name", ""),
            description=self.row.get("Activity Description", ""),
            notes=self.row.get("Activity Private Note", ""),
            equipment_ids=[],
            workout_visibility="private",
            media_visibility="private",
        )

    def manual(self, sport_id: int, target_timezone: str) -> dict[str, Any]:
        result = self.metadata(sport_id)
        distance = number(self.row.get("Distance", ""), "distance") / 1000
        duration = int(
            number(self.row.get("Moving Time") or self.row.get("Elapsed Time", ""), "duration")
        )
        if distance == 0 or duration == 0:
            raise ImportProblem("FitTrackee 1.3.6 rejects zero-distance/duration manual workouts")
        if distance > 999.9 or duration > 2147483647:
            raise ImportProblem("Manual workout exceeds FitTrackee limits")
        local = strava_date(self.row["Activity Date"]).astimezone(ZoneInfo(target_timezone))
        # Endpoint has minute precision and cannot disambiguate the repeated DST hour.
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            raise ImportProblem("Manual workout falls in ambiguous DST hour")
        result.update(
            distance=distance, duration=duration, workout_date=local.strftime("%Y-%m-%d %H:%M")
        )
        return result


class Archive:
    def __init__(self, path: Path):
        self.zip = zipfile.ZipFile(path)
        names = self.zip.namelist()
        if len(names) != len(set(names)):
            self.close()
            raise ImportProblem("Archive has duplicate member names")
        self.names = set(names)
        try:
            rows = self.csv("activities.csv")
            required = {"Activity ID", "Activity Date", "Activity Type", "Filename", "Distance"}
            if not rows or not required <= rows[0].keys():
                raise ImportProblem("activities.csv is empty or missing required columns")
            self.captions = (
                {r["Media Filename"]: r.get("Media Caption", "") for r in self.csv("media.csv")}
                if "media.csv" in self.names
                else {}
            )
            self.activities = []
            seen = set()
            for row in rows:
                key = row["Activity ID"]
                if not re.fullmatch(r"[0-9]{1,64}", key) or key in seen:
                    raise ImportProblem("Missing, invalid or repeated Activity ID")
                seen.add(key)
                media = tuple(m.strip() for m in row.get("Media", "").split("|") if m.strip())
                self.activities.append(Activity(key, row, media))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.zip.close()

    def read(self, name: str, limit: int = MAX_MEMBER) -> bytes:
        member = PurePosixPath(name)
        if member.is_absolute() or ".." in member.parts or "\\" in name:
            raise ImportProblem("Unsafe archive member path")
        if name not in self.names:
            raise ImportProblem("Referenced file is missing from archive")
        if self.zip.getinfo(name).file_size > limit:
            raise ImportProblem("Archive member exceeds size limit")
        with self.zip.open(name) as handle:
            result = handle.read(limit + 1)
        if len(result) > limit:
            raise ImportProblem("Archive member exceeds size limit")
        return result

    def csv(self, name: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(self.read(name, MAX_CSV).decode("utf-8-sig"))))

    def recording(self, activity: Activity) -> tuple[str, bytes] | None:
        name = activity.recording
        if not name:
            return None
        data = self.read(name)
        if name.lower().endswith(".gz"):
            name = name[:-3]
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
                data = handle.read(MAX_MEMBER + 1)
            if len(data) > MAX_MEMBER:
                raise ImportProblem("Decompressed recording exceeds size limit")
        extension = PurePosixPath(name).suffix.lower()
        if extension not in {".gpx", ".tcx", ".fit"}:
            raise ImportProblem(f"Unsupported recording format: {extension}")
        # Older Strava TCX exports can prepend spaces before the XML declaration.
        # Remove only that prefix; upload the same normalized bytes we inspected.
        if extension in {".gpx", ".tcx"}:
            stripped = data.lstrip(b" \t\r\n")
            if stripped.startswith(b"<?xml"):
                data = stripped
        if not has_coordinates(extension, data):
            return None
        return "recording" + extension, data

    def photos(self, activity: Activity) -> list[tuple[str, bytes, str]]:
        if len(activity.media) > 20:
            raise ImportProblem("More than 20 media attachments")
        result = []
        for name in activity.media:
            extension = PurePosixPath(name).suffix.lower()
            if extension not in {".jpg", ".jpeg", ".png"}:
                raise ImportProblem(f"Unsupported media format: {extension}")
            data = self.read(name)
            if not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n")):
                raise ImportProblem("Photo signature does not match a supported image")
            result.append((name, data, self.captions.get(name, "")))
        return result


def has_coordinates(extension: str, data: bytes) -> bool:
    if extension == ".fit":
        import fitdecode  # type: ignore[import-untyped]

        try:
            with fitdecode.FitReader(io.BytesIO(data)) as reader:
                for frame in reader:
                    if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "record":
                        if (
                            frame.get_value("position_lat", fallback=None) is not None
                            and frame.get_value("position_long", fallback=None) is not None
                        ):
                            return True
            return False
        except Exception as exc:
            raise ImportProblem("Cannot parse FIT recording") from exc
    try:
        root = ElementTree.fromstring(data)
    except Exception as exc:
        raise ImportProblem("Cannot parse XML recording") from exc
    if extension == ".gpx":
        return any(
            e.tag.rsplit("}", 1)[-1] == "trkpt" and "lat" in e.attrib and "lon" in e.attrib
            for e in root.iter()
        )
    return any(e.tag.rsplit("}", 1)[-1] == "Position" for e in root.iter())
