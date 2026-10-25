import json
import secrets

import pytest

from strava_fittrackee.archive import ImportProblem
from strava_fittrackee.importer import prepare, run, upload
from strava_fittrackee.journal import Journal

from .conftest import GPX, PHOTO, make_archive


def prepared(archive):
    return prepare(archive, archive.activities[0], {"Cycling (Sport)": 1}, "UTC", {}, True)


def test_success_and_rerun_skip(client, server, archive, tmp_path):
    journal = Journal(tmp_path / "journal.db", "account")
    result = run(
        archive,
        client=client,
        journal=journal,
        dryrun=False,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
    )
    assert result["imported"] == 1
    assert server.workouts == 1
    assert journal.get("001")["phase"] == "bound"
    result = run(
        archive,
        client=client,
        journal=journal,
        dryrun=False,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
    )
    assert result["skipped"] == 1
    assert server.workouts == 1
    journal.close()


def test_live_dryrun_only_gets(client, server, archive):
    client.readonly = True
    result = run(
        archive,
        client=client,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
    )
    assert result["would_import"] == 1
    assert all(method == "GET" for method, _ in server.calls)
    assert server.workouts == 0 and server.media == 0 and server.token is None


def test_mapped_activity_skipped_before_unsupported_sport(client, server, archive):
    server.mapping = {"workout_id": "existing"}
    archive.activities[0].row["Activity Type"] = "Yoga"
    result = run(
        archive,
        all_sports=True,
        client=client,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
    )
    assert result["skipped"] == 1
    assert not result["blocked"]


def test_lost_workout_response_never_reuploads(client, server, archive, tmp_path):
    journal = Journal(tmp_path / "journal.db", "account")
    server.fail_upload = True
    with pytest.raises(ImportProblem, match="Network"):
        upload(client, journal, archive.activities[0], prepared(archive))
    assert server.workouts == 1
    server.fail_upload = False
    with pytest.raises(ImportProblem, match="Uncertain"):
        upload(client, journal, archive.activities[0], prepared(archive))
    assert server.workouts == 1
    journal.close()


def test_lost_begin_response_never_uploads(client, server, archive, tmp_path):
    journal = Journal(tmp_path / "journal.db", "account")
    server.fail_begin = True
    with pytest.raises(ImportProblem):
        upload(client, journal, archive.activities[0], prepared(archive))
    server.fail_begin = False
    with pytest.raises(ImportProblem, match="Uncertain"):
        upload(client, journal, archive.activities[0], prepared(archive))
    assert server.workouts == 0
    journal.close()


def test_bind_failure_recovers_without_second_upload(client, server, archive, tmp_path):
    path = tmp_path / "journal.db"
    journal = Journal(path, "account")
    server.fail_bind = True
    with pytest.raises(ImportProblem):
        upload(client, journal, archive.activities[0], prepared(archive))
    assert journal.get("001")["workout_id"] == "native1"
    journal.close()
    server.fail_bind = False
    journal = Journal(path, "account")
    assert upload(client, journal, archive.activities[0], prepared(archive)) == "recovered"
    assert server.workouts == 1
    journal.close()


def test_claim_contention_prevents_upload(client, server, archive, tmp_path):
    journal = Journal(tmp_path / "journal.db", "account")
    server.token, server.state = secrets.token_urlsafe(32), "uploading"
    with pytest.raises(ImportProblem, match="claim_busy"):
        upload(client, journal, archive.activities[0], prepared(archive))
    assert server.workouts == 0
    journal.close()


def test_repeat_begin_does_not_grant_permission(client, server, archive, tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal.db", "account")
    original = client.request

    def request(method, path, **kwargs):
        if path.endswith("begin-upload"):
            return {"upload_authorized": False, "recovery_required": True}
        return original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(ImportProblem, match="not authorized"):
        upload(client, journal, archive.activities[0], prepared(archive))
    assert server.workouts == 0
    journal.close()


def test_photo_caption_attached_and_journaled(client, server, archive, tmp_path):
    row = {**archive.activities[0].row, "Media": "media/photo.png|"}
    source = make_archive(
        tmp_path,
        [row],
        {"activities/001.gpx": GPX, "media/photo.png": PHOTO},
        [{"Media Filename": "media/photo.png", "Media Caption": "caption"}],
    )
    journal = Journal(tmp_path / "journal.db", "account")
    assert upload(client, journal, source.activities[0], prepared(source)) == "imported"
    assert journal.get("001")["media_ids"] == ["photo1"]
    assert ("PATCH", "/api/media/photo1") in server.calls
    journal.close()
    source.close()


def test_uncertain_photo_does_not_retry(client, server, archive, tmp_path):
    row = {**archive.activities[0].row, "Media": "media/photo.png"}
    source = make_archive(tmp_path, [row], {"activities/001.gpx": GPX, "media/photo.png": PHOTO})
    journal = Journal(tmp_path / "journal.db", "account")
    server.fail_media = True
    with pytest.raises(ImportProblem):
        upload(client, journal, source.activities[0], prepared(source))
    with pytest.raises(ImportProblem, match="Uncertain"):
        upload(client, journal, source.activities[0], prepared(source))
    assert server.media == 1 and server.workouts == 0
    journal.close()
    source.close()


def test_manual_endpoint_payload(client, server, archive, tmp_path, monkeypatch):
    archive.activities[0].row["Filename"] = ""
    journal = Journal(tmp_path / "journal.db", "account")
    original = server.handle
    payloads = []

    def handle(request):
        if request.url.path == "/api/workouts/no_gpx":
            payloads.append(json.loads(request.content))
        return original(request)

    import httpx

    client.http.close()
    client.http = httpx.Client(transport=httpx.MockTransport(handle))
    upload(client, journal, archive.activities[0], prepared(archive))
    assert payloads[0]["distance"] == 1.5
    assert payloads[0]["duration"] == 60
    assert "map_visibility" not in payloads[0]
    journal.close()


def test_trackless_file_uses_manual(archive):
    archive.recording = lambda _, **kwargs: None
    result = prepared(archive)
    assert result.recording is None
    assert result.metadata["duration"] == 60


def test_namespace_isolation(tmp_path):
    path = tmp_path / "journal.db"
    a, b = Journal(path, "one"), Journal(path, "two")
    a.save("1", {"token": "private"})
    assert b.get("1") is None
    a.close()
    b.close()


def test_unknown_selected_id_rejected(client, archive):
    with pytest.raises(ImportProblem, match="absent"):
        run(
            archive,
            client=client,
            journal=None,
            dryrun=True,
            timezone="UTC",
            overrides={},
            include_photos=True,
            limit=1,
            activity_ids={"missing"},
            emit=lambda _: None,
        )


def test_expired_unused_claim_rotates_token(client, server, archive, tmp_path, monkeypatch):
    from strava_fittrackee.client import APIError

    journal = Journal(tmp_path / "journal.db", "account")
    old = secrets.token_urlsafe(32)
    journal.save("001", {"token": old, "phase": "reserved", "media_ids": []})
    original = client.request
    attempts = []

    def request(method, path, **kwargs):
        if method == "PUT" and "/claims/" in path:
            attempts.append(kwargs["headers"]["X-Identity-Claim"])
            if len(attempts) == 1:
                raise APIError(409, "claim_expired_use_new_token")
        return original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    assert upload(client, journal, archive.activities[0], prepared(archive)) == "imported"
    assert attempts[0] == old and attempts[1] != old
    journal.close()


def test_release_requires_no_known_workout_and_rotates_token(client, server, tmp_path):
    from strava_fittrackee.importer import release_confirmed

    journal = Journal(tmp_path / "journal.db", "account")
    old = secrets.token_urlsafe(32)
    server.token, server.state = old, "uploading"
    journal.save("001", {"token": old, "phase": "uploading", "media_ids": ["orphan"]})
    release_confirmed(client, journal, "001")
    entry = journal.get("001")
    assert entry["token"] != old and entry["phase"] == "reserved"
    assert entry["previous_media_ids"] == ["orphan"]
    assert server.state is None
    entry["workout_id"] = "known"
    journal.save("001", entry)
    with pytest.raises(ImportProblem, match="already knows"):
        release_confirmed(client, journal, "001")
    journal.close()


def test_lost_release_response_prevents_normal_import(client, server, archive, tmp_path):
    from strava_fittrackee.importer import release_confirmed

    journal = Journal(tmp_path / "journal.db", "account")
    old = secrets.token_urlsafe(32)
    server.token, server.state = old, "uploading"
    journal.save("001", {"token": old, "phase": "uploading", "media_ids": []})
    server.fail_release = True
    with pytest.raises(ImportProblem):
        release_confirmed(client, journal, "001")
    assert journal.get("001")["phase"] == "releasing"
    with pytest.raises(ImportProblem, match="Interrupted claim release"):
        upload(client, journal, archive.activities[0], prepared(archive))
    server.fail_release = False
    release_confirmed(client, journal, "001")
    assert upload(client, journal, archive.activities[0], prepared(archive)) == "imported"
    assert server.workouts == 1
    journal.close()


def test_release_refuses_bound_identity(client, server, tmp_path):
    from strava_fittrackee.importer import release_confirmed

    journal = Journal(tmp_path / "journal.db", "account")
    journal.save("001", {"token": "secret", "phase": "uploading"})
    server.mapping = {"workout_id": "existing"}
    with pytest.raises(ImportProblem, match="already mapped"):
        release_confirmed(client, journal, "001")
    assert not any(method == "DELETE" for method, _ in server.calls)
    journal.close()


def test_target_size_limit_blocks_before_claim(client, server, archive, monkeypatch):
    original = client.request

    def request(method, path, **kwargs):
        if path == "/api/config":
            return {"data": {"version": "1.3.6", "max_single_file_size": 1, "max_image_size": 1}}
        return original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    result = run(
        archive,
        client=client,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
    )
    assert result["blocked"] == 1
    assert server.token is None


def test_unsupported_server_version_fails_closed(client, server, archive, monkeypatch):
    original = client.request

    def request(method, path, **kwargs):
        if path == "/api/config":
            return {"data": {"version": "1.4.0"}}
        return original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(ImportProblem, match="1.3.6 only"):
        run(
            archive,
            client=client,
            journal=None,
            dryrun=True,
            timezone="UTC",
            overrides={},
            include_photos=True,
            limit=None,
            activity_ids=set(),
            emit=lambda _: None,
        )
    assert server.token is None
