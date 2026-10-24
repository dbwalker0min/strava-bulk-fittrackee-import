import json

import pytest

from strava_bulk_import.client import Client, read_credentials, update_tokens
from strava_bulk_import.errors import ImportProblem
from strava_bulk_import.journal import Journal
from strava_bulk_import.models import journal_entry


def test_legacy_journal_format_round_trips_without_losing_recovery_fields(tmp_path):
    path = tmp_path / "journal.db"
    original = {
        "token": "secret",
        "phase": "uploading",
        "workout_id": "native",
        "media_ids": ["photo"],
        "previous_media_ids": ["earlier"],
    }
    journal = Journal(path, "account")
    # Simulate a row written by the previous release, not the new typed writer.
    journal.db.execute(
        "INSERT INTO entries VALUES (?, ?, ?)", ("account", "001", json.dumps(original))
    )
    journal.db.commit()
    journal.close()
    journal = Journal(path, "account")
    entry = journal.get("001")
    assert entry == original
    journal.save("001", entry)
    assert journal.get("001") == original
    journal.close()


@pytest.mark.parametrize(
    "invalid",
    [
        {"token": "secret"},
        {"token": "secret", "phase": "unknown"},
        {"token": 1, "phase": "reserved"},
        {"token": "secret", "phase": []},
        {"token": "secret", "phase": "uploading", "media_ids": [1]},
        {"token": "secret", "phase": "uploading", "workout_id": 1},
    ],
)
def test_invalid_journal_fields_rejected(invalid):
    with pytest.raises(ImportProblem):
        journal_entry(invalid)


@pytest.mark.parametrize("expiry", [True, float("nan"), float("inf"), "864000", None])
def test_invalid_token_fields_are_rejected(expiry):
    with pytest.raises(ImportProblem):
        update_tokens(
            {"server": "https://fit.example"},
            {"access_token": "secret", "refresh_token": "secret", "expires_in": expiry},
        )


def test_old_credential_json_still_loads():
    original = {
        "server": "https://fit.example",
        "client_id": "client",
        "client_secret": "secret",
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": 123.0,
    }
    assert read_credentials(original) == original


def test_corrupt_credentials_do_not_expose_secret(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text('["private-secret"]')
    with pytest.raises(ImportProblem) as exc:
        Client("https://fit.example", path)
    assert "private-secret" not in str(exc.value)


@pytest.mark.parametrize("release", [False, True])
def test_cli_recovery_keeps_single_activity_id_and_existing_journal(
    client, server, tmp_path, monkeypatch, release
):
    from typer.testing import CliRunner

    from strava_bulk_import import cli

    state = tmp_path / "state"
    namespace = "https://fit.example|synthetic|today"
    journal = Journal(state / "journal.sqlite3", namespace)
    entry = {"token": "existing-claim-token", "phase": "uploading", "media_ids": []}
    journal.save("001", entry)
    journal.close()
    server.token, server.state = entry["token"], "uploading"
    original = client.request

    def request(method, path, **kwargs):
        if method == "GET" and path.endswith("/workouts/native1/identities"):
            return {"identities": []}
        return original(method, path, **kwargs)

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(cli, "Client", lambda *args, **kwargs: client)
    options = ["--confirm-no-upload"] if release else ["--workout-id", "native1"]
    result = CliRunner().invoke(
        cli.app,
        [
            "recover",
            "--server",
            "https://fit.example",
            "--state-dir",
            str(state),
            "--activity-id",
            "001",
            *options,
        ],
    )
    assert result.exit_code == 0, result.output
    journal = Journal(state / "journal.sqlite3", namespace)
    saved = journal.get("001")
    if release:
        assert saved["phase"] == "reserved" and saved["token"] != entry["token"]
    else:
        assert saved["phase"] == "bound" and saved["workout_id"] == "native1"
        assert server.mapping == {"workout_id": "native1"}
    assert server.workouts == 0
    journal.close()
