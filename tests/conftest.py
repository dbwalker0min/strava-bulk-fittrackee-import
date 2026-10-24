from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from strava_bulk_import.archive import Archive
from strava_bulk_import.client import Client

GPX = b"""<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="40" lon="-120"><time>2026-01-01T12:00:00Z</time></trkpt>
<trkpt lat="40.01" lon="-120"><time>2026-01-01T12:01:00Z</time></trkpt>
</trkseg></trk></gpx>"""
PHOTO = b"\x89PNG\r\n\x1a\nsynthetic-test"


def make_archive(tmp_path: Path, rows=None, files=None, captions=None) -> Archive:
    if rows is None:
        rows = [
            dict(
                **{
                    "Activity ID": "001",
                    "Activity Date": "Jan 1, 2026, 12:00:00 PM",
                    "Activity Type": "Ride",
                    "Activity Name": "Synthetic",
                    "Activity Description": "Generated",
                    "Filename": "activities/001.gpx",
                    "Distance": "1500",
                    "Moving Time": "60",
                    "Elapsed Time": "90",
                    "Activity Private Note": "Private",
                    "Media": "",
                }
            )
        ]
    columns = sorted(set().union(*(r.keys() for r in rows)))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("activities.csv", output.getvalue())
        for name, content in (files if files is not None else {"activities/001.gpx": GPX}).items():
            z.writestr(name, content)
        if captions:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=["Media Filename", "Media Caption"])
            writer.writeheader()
            writer.writerows(captions)
            z.writestr("media.csv", output.getvalue())
    return Archive(path)


class Server:
    def __init__(self):
        self.calls = []
        self.mapping = None
        self.state = None
        self.token = None
        self.workouts = 0
        self.media = 0
        self.fail_upload = False
        self.fail_bind = False
        self.fail_begin = False
        self.fail_media = False
        self.fail_release = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if path == "/api/auth/profile":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "username": "synthetic",
                        "created_at": "today",
                        "timezone": "America/Los_Angeles",
                    }
                },
            )
        if path == "/api/config":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "version": "1.3.6",
                        "max_single_file_size": 1048576,
                        "max_image_size": 5242880,
                    }
                },
            )
        if path == "/api/sports":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "sports": [
                            {
                                "label": "Cycling (Sport)",
                                "id": 1,
                                "is_active": True,
                                "is_active_for_user": True,
                            }
                        ]
                    }
                },
            )
        if "/identities/" in path:
            if method == "GET":
                return (
                    httpx.Response(200, json=self.mapping) if self.mapping else httpx.Response(404)
                )
            if self.fail_bind:
                raise httpx.ReadTimeout("synthetic", request=request)
            assert request.headers["X-Identity-Claim"] == self.token
            self.mapping = {"workout_id": json.loads(request.content)["workout_id"]}
            self.state = "bound"
            return httpx.Response(201, json=self.mapping)
        if "/claims/" in path:
            incoming = request.headers["X-Identity-Claim"]
            if method == "DELETE":
                assert request.headers["X-Identity-Reconciled"] == "true"
                assert incoming == self.token or self.token is None
                self.state, self.token = None, None
                if self.fail_release:
                    raise httpx.ReadTimeout("synthetic", request=request)
                return httpx.Response(204)
            if path.endswith("begin-upload"):
                assert incoming == self.token
                first = self.state == "reserved"
                self.state = "uploading"
                if self.fail_begin:
                    raise httpx.ReadTimeout("synthetic", request=request)
                return httpx.Response(
                    201 if first else 200,
                    json={
                        "state": "uploading",
                        "upload_authorized": first,
                        "recovery_required": not first,
                    },
                )
            if self.mapping:
                return httpx.Response(200, json={"state": "bound", **self.mapping})
            if self.token and incoming != self.token:
                return httpx.Response(409, json={"error": "claim_busy"})
            self.token = incoming
            self.state = self.state or "reserved"
            return httpx.Response(
                200, json={"state": self.state, "recovery_required": self.state == "uploading"}
            )
        if path == "/api/media":
            self.media += 1
            if self.fail_media:
                raise httpx.ReadTimeout("synthetic", request=request)
            return httpx.Response(201, json={"id": f"photo{self.media}"})
        if path.startswith("/api/media/"):
            return httpx.Response(200, json={})
        if path in {"/api/workouts", "/api/workouts/no_gpx"}:
            self.workouts += 1
            if self.fail_upload:
                raise httpx.ReadTimeout("synthetic", request=request)
            return httpx.Response(201, json={"data": {"workouts": [{"id": "native1"}]}})
        raise AssertionError(f"Unexpected request: {method} {path}")


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def client(tmp_path, server):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "server": "https://fit.example",
                "access_token": "test",
                "expires_at": time.time() + 3600,
            }
        )
    )
    result = Client(
        "https://fit.example", credentials, transport=httpx.MockTransport(server.handle)
    )
    yield result
    result.close()


@pytest.fixture
def archive(tmp_path):
    result = make_archive(tmp_path)
    yield result
    result.close()
