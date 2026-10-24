import hashlib
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

from strava_bulk_import.auth import authorization_url
from strava_bulk_import.cli import app
from strava_bulk_import.client import Client, ImportProblem, origin, save_private
from strava_bulk_import.journal import RunLock


def test_origin_rejects_unsafe_endpoints():
    for value in [
        "http://fit.example",
        "https://u:p@fit.example",
        "https://fit.example/api",
        "https://fit.example?x=1",
        "https://fit.example#fragment",
    ]:
        with pytest.raises(ImportProblem):
            origin(value)
    assert origin("https://fit.example/") == "https://fit.example"


def test_readonly_blocks_all_writes_and_refresh(client, server):
    client.readonly = True
    for method in ["POST", "PUT", "PATCH", "DELETE"]:
        with pytest.raises(ImportProblem, match="forbids"):
            client.request(method, "/api/workouts")
    client.auth["expires_at"] = 0
    with pytest.raises(ImportProblem, match="expired"):
        client.request("GET", "/api/sports")
    assert not server.calls


def test_refresh_rotates_tokens_atomically_and_reuses_new_access(tmp_path):
    path = tmp_path / "private" / "credentials.json"
    save_private(
        path,
        {
            "server": "https://fit.example",
            "client_id": "client",
            "client_secret": "secret",
            "refresh_token": "old-refresh",
            "access_token": "old-access",
            "expires_at": 0,
        },
    )
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/api/oauth/token":
            data = parse_qs(request.content.decode())
            assert data["refresh_token"] == ["old-refresh"]
            assert data["client_secret"] == ["secret"]
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 864000,
                },
            )
        assert request.headers["Authorization"] == "Bearer new-access"
        return httpx.Response(200, json={})

    client = Client("https://fit.example", path, transport=httpx.MockTransport(handle))
    client.request("GET", "/api/sports")
    assert calls == ["/api/oauth/token", "/api/sports"]
    assert json.loads(path.read_text())["refresh_token"] == "new-refresh"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    client.close()


def test_redirects_do_not_forward_credentials(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "server": "https://fit.example",
                "access_token": "secret",
                "expires_at": time.time() + 3600,
            }
        )
    )
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/api"})

    client = Client("https://fit.example", path, transport=httpx.MockTransport(handle))
    with pytest.raises(ImportProblem):
        client.request("GET", "/api/sports")
    assert calls == ["https://fit.example/api/sports"]
    client.close()


def test_different_server_cannot_read_credentials(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text('{"server":"https://other.example"}')
    with pytest.raises(ImportProblem, match="different server"):
        Client("https://fit.example", path)


def test_pkce_authorization_url():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    url = authorization_url(
        "https://fit.example", "client", "http://127.0.0.1:8765/callback", "random-state", verifier
    )
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/profile/apps/authorize"
    assert query["state"] == ["random-state"]
    assert query["code_challenge"] == ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"]
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == {
        "profile:read",
        "workouts:read",
        "workouts:write",
        "media:write",
    }


def test_offline_dryrun_creates_no_state(tmp_path, archive):
    state = tmp_path / "never-created"
    result = CliRunner().invoke(
        app, ["import", str(archive.zip.filename), "--dryrun", "--state-dir", str(state)]
    )
    assert result.exit_code == 0, result.output
    assert not state.exists()


def test_dryrun_does_not_change_export(tmp_path, archive):
    path = archive.zip.filename
    before = hashlib.sha256(open(path, "rb").read()).hexdigest()
    assert CliRunner().invoke(app, ["import", str(path), "--dry-run"]).exit_code == 0
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == before


def test_limit_requires_positive(tmp_path, archive):
    result = CliRunner().invoke(
        app, ["import", str(archive.zip.filename), "--dryrun", "--limit", "0"]
    )
    assert result.exit_code == 2
    assert "--limit" in result.output


def test_mutating_import_requires_server(archive):
    result = CliRunner().invoke(app, ["import", str(archive.zip.filename)])
    assert result.exit_code == 2
    assert "--server is required" in result.output


def test_run_lock_released_on_close(tmp_path):
    first = RunLock(tmp_path)
    with pytest.raises(ImportProblem, match="Another"):
        RunLock(tmp_path)
    first.close()
    second = RunLock(tmp_path)
    second.close()
