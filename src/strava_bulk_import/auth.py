"""Interactive authorization-code login with PKCE and a loopback-only callback."""

from __future__ import annotations

import base64
import getpass
import hashlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .archive import ImportProblem
from .client import origin, save_private, update_tokens

SCOPES = "profile:read workouts:read workouts:write media:write"


def authorization_url(base: str, client_id: str, redirect: str, state: str, verifier: str) -> str:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return (
        base
        + "/profile/apps/authorize?"
        + urlencode(
            dict(
                client_id=client_id,
                redirect_uri=redirect,
                response_type="code",
                scope=SCOPES,
                state=state,
                code_challenge=challenge,
                code_challenge_method="S256",
            )
        )
    )


def login(base: str, credentials: Path, client_id: str, port: int) -> None:
    base = origin(base)
    secret = getpass.getpass("FitTrackee OAuth client secret: ")
    if not secret:
        raise ImportProblem("Client secret is required")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    callback: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # Authorization codes must never enter logs.

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            valid = (
                parsed.path == "/callback"
                and query.get("state") == [state]
                and len(query.get("code", [])) == 1
            )
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if valid:
                callback["code"] = query["code"][0]
            self.wfile.write(
                b"Authorization received. Return to the terminal."
                if valid
                else b"Invalid callback."
            )

    redirect = f"http://127.0.0.1:{port}/callback"
    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 1
        url = authorization_url(base, client_id, redirect, state, verifier)
        print("Open this URL to authorize the importer:\n" + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while not callback and time.monotonic() < deadline:
            server.handle_request()
    if not callback:
        raise ImportProblem("Login timed out; run login again")
    with httpx.Client(timeout=30, follow_redirects=False) as http:
        response = http.post(
            base + "/api/oauth/token",
            data=dict(
                client_id=client_id,
                client_secret=secret,
                grant_type="authorization_code",
                redirect_uri=redirect,
                code=callback["code"],
                code_verifier=verifier,
            ),
        )
    if response.status_code != 200:
        raise ImportProblem("OAuth exchange rejected; check client settings and log in again")
    auth = dict(server=base, client_id=client_id, client_secret=secret)
    update_tokens(auth, response.json())
    save_private(credentials, auth)
    print("Login saved. Credentials refresh automatically during imports.")
