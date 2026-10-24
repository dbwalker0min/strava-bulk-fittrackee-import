import gzip
import zipfile
from datetime import UTC, datetime

import pytest

from strava_bulk_import.archive import (
    Activity,
    Archive,
    ImportProblem,
    has_coordinates,
    strava_date,
)

from .conftest import GPX, PHOTO, make_archive


def test_gzip_and_opaque_id(tmp_path, archive):
    row = {**archive.activities[0].row, "Filename": "activities/001.gpx.gz"}
    source = make_archive(tmp_path, [row], {row["Filename"]: gzip.compress(GPX)})
    assert source.activities[0].external_id == "001"
    assert source.recording(source.activities[0]) == ("recording.gpx", GPX)
    source.close()


def test_manual_units_timezone_and_private_metadata(archive):
    original = archive.activities[0]
    activity = Activity(original.external_id, {**original.row, "Filename": ""}, ())
    data = activity.manual(1, "America/Los_Angeles")
    assert data["distance"] == 1.5
    assert data["duration"] == 60
    assert data["workout_date"] == "2026-01-01 04:00"
    assert data["workout_visibility"] == "private"
    assert data["notes"] == "Private"
    assert data["equipment_ids"] == []
    assert "analysis_visibility" not in data


def test_manual_rejects_ambiguous_dst_hour(archive):
    row = {**archive.activities[0].row, "Activity Date": "Nov 1, 2026, 8:30:00 AM"}
    with pytest.raises(ImportProblem, match="DST"):
        Activity("1", row, ()).manual(1, "America/Los_Angeles")


@pytest.mark.parametrize("value", ["nan", "inf", "-10", "0", "garbage"])
def test_manual_rejects_invalid_or_zero_distance(archive, value):
    row = {**archive.activities[0].row, "Distance": value}
    with pytest.raises(ImportProblem):
        Activity("1", row, ()).manual(1, "UTC")


def test_duplicate_identity_rejected(tmp_path, archive):
    row = archive.activities[0].row
    with pytest.raises(ImportProblem, match="repeated"):
        make_archive(tmp_path, [row, row])


def test_missing_recording_is_not_silently_manual(tmp_path, archive):
    source = make_archive(tmp_path, [archive.activities[0].row], {})
    with pytest.raises(ImportProblem, match="missing"):
        source.recording(source.activities[0])
    source.close()


def test_unsupported_binary_is_not_silently_manual(tmp_path, archive):
    row = {**archive.activities[0].row, "Filename": "activities/1.bin.gz"}
    source = make_archive(tmp_path, [row], {row["Filename"]: gzip.compress(b"binary")})
    with pytest.raises(ImportProblem, match="Unsupported"):
        source.recording(source.activities[0])
    source.close()


def test_trackless_xml_detected():
    assert not has_coordinates(".gpx", b"<gpx><trk/></gpx>")
    assert not has_coordinates(".tcx", b"<TrainingCenterDatabase/>")
    assert has_coordinates(".tcx", b"<TrainingCenterDatabase><Position/></TrainingCenterDatabase>")


def test_xml_entities_rejected():
    with pytest.raises(ImportProblem):
        has_coordinates(".gpx", b'<!DOCTYPE a [<!ENTITY x "boom">]><gpx>&x;</gpx>')


def test_path_traversal_rejected(tmp_path, archive):
    row = {**archive.activities[0].row, "Filename": "../escape.gpx"}
    source = make_archive(tmp_path, [row], {row["Filename"]: GPX})
    with pytest.raises(ImportProblem, match="Unsafe"):
        source.recording(source.activities[0])
    source.close()


def test_bounded_gzip(tmp_path, archive, monkeypatch):
    import strava_bulk_import.archive as module

    row = {**archive.activities[0].row, "Filename": "activities/001.gpx.gz"}
    source = make_archive(tmp_path, [row], {row["Filename"]: gzip.compress(b"a" * 10000)})
    monkeypatch.setattr(module, "MAX_MEMBER", 100)
    with pytest.raises(ImportProblem, match="Decompressed"):
        source.recording(source.activities[0])
    source.close()


def test_duplicate_zip_members_rejected(tmp_path):
    path = tmp_path / "duplicates.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("same", b"a")
        with pytest.warns(UserWarning):
            z.writestr("same", b"b")
    with pytest.raises(ImportProblem, match="duplicate member"):
        Archive(path)


def test_captions_and_video_reporting(tmp_path, archive):
    row = {**archive.activities[0].row, "Media": "media/a.png|"}
    source = make_archive(
        tmp_path,
        [row],
        {"media/a.png": PHOTO},
        [{"Media Filename": "media/a.png", "Media Caption": "Generated"}],
    )
    assert source.photos(source.activities[0]) == [("media/a.png", PHOTO, "Generated")]
    with pytest.raises(ImportProblem, match="Unsupported media"):
        source.photos(Activity("1", row, ("media/a.mp4",)))
    source.close()


def test_dates_do_not_depend_on_system_locale():
    assert strava_date("Oct 18, 2026, 5:59:34 PM") == datetime(2026, 10, 18, 17, 59, 34, tzinfo=UTC)
    assert strava_date("2026-01-01T12:00:00Z").hour == 12
    with pytest.raises(ImportProblem):
        strava_date("Oct 18, 2026, 25:00:00 PM")


def test_strava_duplicate_columns_use_last_numeric_distance(tmp_path):
    path = tmp_path / "duplicate-columns.zip"
    text = (
        "Activity ID,Activity Date,Activity Type,Filename,Distance,Distance,Elapsed Time\n"
        "1,2026-01-01T12:00:00Z,Ride,,1.5,1500,60\n"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("activities.csv", text)
    source = Archive(path)
    assert source.activities[0].manual(1, "UTC")["distance"] == 1.5
    source.close()


def test_old_tcx_xml_whitespace_normalized_before_upload(tmp_path, archive):
    row = {**archive.activities[0].row, "Filename": "activities/001.tcx.gz"}
    xml = b'<?xml version="1.0"?><TrainingCenterDatabase><Position/></TrainingCenterDatabase>'
    source = make_archive(tmp_path, [row], {row["Filename"]: gzip.compress(b"          " + xml)})
    assert source.recording(source.activities[0]) == ("recording.tcx", xml)
    source.close()
