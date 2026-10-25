import struct

import pytest
from fitdecode.utils import compute_crc

from strava_fittrackee.archive import ExcludedActivity, indoor_reason, recording_info
from strava_fittrackee.importer import run

from .conftest import make_archive


def fit_recording(sub_sport: int) -> bytes:
    # Valid synthetic FIT: record coordinates, then trailing session metadata.
    record_definition = b"\x40\x00\x00" + struct.pack("<H", 20) + b"\x02\x00\x04\x85\x01\x04\x85"
    record = b"\x00" + struct.pack("<ii", 12345, 67890)
    session_definition = b"\x41\x00\x00" + struct.pack("<H", 18) + b"\x01\x06\x01\x00"
    data = record_definition + record + session_definition + bytes([1, sub_sport])
    header = struct.pack("<BBHI4s", 14, 0x20, 21200, len(data), b".FIT")
    header += struct.pack("<H", compute_crc(header))
    payload = header + data
    return payload + struct.pack("<H", compute_crc(payload))


def inspect(archive, **kwargs):
    return run(
        archive,
        client=None,
        journal=None,
        dryrun=True,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
        **kwargs,
    )


@pytest.mark.parametrize("kind", ["Virtual Ride", "Indoor Cycling", "Indoor Ride"])
def test_indoor_type_skipped_before_missing_recording(archive, kind):
    archive.activities[0].row.update({"Activity Type": kind, "Filename": "missing.fit"})
    result = inspect(archive)
    assert result["excluded"] == 1 and not result["blocked"]


def test_virtual_opt_in(archive):
    archive.activities[0].row["Activity Type"] = "Virtual Ride"
    result = inspect(archive, include_indoor=True)
    assert result["candidate"] == 1


@pytest.mark.parametrize("field,value", [("Trainer", "true"), ("Indoor", "1"), ("Virtual", "YES")])
def test_explicit_csv_flags(archive, field, value):
    archive.activities[0].row[field] = value
    assert inspect(archive)["excluded"] == 1
    assert inspect(archive, include_indoor=True)["candidate"] == 1


def test_false_flags_and_title_are_not_used_to_guess(archive):
    archive.activities[0].row.update(
        {
            "Trainer": "false",
            "Indoor": "0",
            "Activity Name": "Indoor ride? This is an outdoor loop.",
        }
    )
    assert indoor_reason(archive.activities[0]) is None
    assert inspect(archive)["candidate"] == 1


def test_trackless_outdoor_ride_remains_eligible(archive):
    archive.activities[0].row["Filename"] = ""
    assert inspect(archive)["candidate"] == 1


def test_noncycling_scope_and_override(archive):
    archive.activities[0].row["Activity Type"] = "Walk"
    assert inspect(archive)["excluded"] == 1
    assert inspect(archive, all_sports=True)["candidate"] == 1


@pytest.mark.parametrize("sub_sport", [6, 58])
def test_fit_indoor_metadata_after_coordinates_is_detected(archive, tmp_path, sub_sport):
    content = fit_recording(sub_sport)
    assert recording_info(".fit", content) == (True, True)
    row = {**archive.activities[0].row, "Filename": "activities/001.fit"}
    source = make_archive(tmp_path, [row], {row["Filename"]: content})
    assert inspect(source)["excluded"] == 1
    assert inspect(source, include_indoor=True)["candidate"] == 1
    with pytest.raises(ExcludedActivity):
        source.recording(source.activities[0], include_indoor=False)
    source.close()


def test_fit_outdoor_metadata_retains_recording():
    assert recording_info(".fit", fit_recording(0)) == (True, False)


def test_live_exclusion_has_no_claim_journal_or_upload(client, server, archive):
    archive.activities[0].row["Activity Type"] = "Virtual Ride"
    result = run(
        archive,
        client=client,
        journal=None,
        dryrun=False,
        timezone="UTC",
        overrides={},
        include_photos=True,
        limit=None,
        activity_ids=set(),
        emit=lambda _: None,
    )
    assert result["excluded"] == 1
    assert all(method == "GET" for method, _ in server.calls)
    assert not any("/identities/" in path or "/claims/" in path for _, path in server.calls)
    assert not server.token and server.workouts == 0 and server.media == 0
