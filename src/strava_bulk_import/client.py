"""HTTPS-only transport and OAuth credential handling. No automatic write retries."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx

from .errors import ImportProblem
from .models import (
    Credentials,
    HTTPMethod,
    JSONObject,
    credential_fields,
    object_value,
    string_value,
)


class APIError(ImportProblem):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code
        super().__init__(f"API request failed ({status}, {code})")


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ImportProblem("Server must be an HTTPS origin without a path or credentials")
    return value.rstrip("/")


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def save_private(path: Path, data: Mapping[str, object]) -> None:
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credentials-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Client:
    def __init__(
        self,
        base: str,
        credentials: Path,
        *,
        readonly: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base = origin(base)
        self.credentials = credentials
        self.readonly = readonly
        self.http = httpx.Client(timeout=180, follow_redirects=False, transport=transport)
        try:
            loaded = object_value(json.loads(credentials.read_text()))
        except (OSError, ValueError, ImportProblem) as exc:
            self.http.close()
            raise ImportProblem("Run login first; credential file is missing or invalid") from exc
        if loaded.get("server") != self.base:
            self.http.close()
            raise ImportProblem("Credentials belong to a different server")

        try:
            self.auth = read_credentials(loaded)
        except ImportProblem:
            self.http.close()
            raise

    def close(self) -> None:
        self.http.close()

    def refresh(self) -> None:
        if self.readonly:
            raise ImportProblem("Token expired: run login or refresh outside --dryrun")
        fields = {
            "client_id": string_value(self.auth.get("client_id")),
            "client_secret": string_value(self.auth.get("client_secret")),
            "refresh_token": string_value(self.auth.get("refresh_token")),
            "grant_type": "refresh_token",
        }
        try:
            response = self.http.post(self.base + "/api/oauth/token", data=fields)
        except httpx.HTTPError as exc:
            raise ImportProblem(
                "Token refresh failed; log in again if the outcome is uncertain"
            ) from exc
        if response.status_code != 200:
            raise ImportProblem("Token refresh rejected; run login again")
        self.auth = update_tokens(self.auth, response.json())
        save_private(self.credentials, self.auth)

    def request(
        self,
        method: HTTPMethod,
        path: str,
        *,
        missing_ok: bool = False,
        headers: dict[str, str] | None = None,
        json: object = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, bytes]] | None = None,
    ) -> JSONObject | None:
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise ImportProblem("Invalid API path")
        if self.readonly and method != "GET":
            raise ImportProblem("--dryrun forbids server mutations")
        if time.time() + 60 >= self.auth.get("expires_at", 0):
            self.refresh()
        request_headers = {
            "Authorization": "Bearer " + self.auth["access_token"],
            **(headers or {}),
        }
        try:
            response = self.http.request(
                method, self.base + path, headers=request_headers, json=json, data=data, files=files
            )
        except httpx.HTTPError as exc:
            raise ImportProblem("Network failure; journal preserves any uncertain upload") from exc
        if missing_ok and response.status_code == 404:
            return None
        if not response.is_success:
            # Expose only known companion codes, never private response bodies.
            code = "request_rejected"
            try:
                candidate = object_value(response.json()).get("error")
                if isinstance(candidate, str) and candidate in {
                    "claim_busy",
                    "claim_expired",
                    "claim_expired_use_new_token",
                    "identity_already_mapped",
                    "storage_unavailable",
                }:
                    code = candidate
            except (ValueError, ImportProblem):
                pass
            raise APIError(response.status_code, code)
        if response.status_code == 204:
            return {}
        try:
            return object_value(response.json())
        except (ValueError, ImportProblem) as exc:
            raise ImportProblem("Invalid API response; inspect journal before retrying") from exc


def read_credentials(value: object) -> Credentials:
    data = object_value(value)
    access = string_value(data.get("access_token"))
    expiry = data.get("expires_at")
    if type(expiry) not in {int, float} or not math.isfinite(cast(float, expiry)):
        raise ImportProblem("Invalid stored token expiry")
    result: Credentials = {
        "server": string_value(data.get("server")),
        "access_token": access,
        "expires_at": float(cast(float, expiry)),
    }
    if "client_id" in data:
        result["client_id"] = string_value(data["client_id"])
    if "client_secret" in data:
        result["client_secret"] = string_value(data["client_secret"])
    if "refresh_token" in data:
        result["refresh_token"] = string_value(data["refresh_token"])
    return result


def update_tokens(auth: Mapping[str, object], tokens: object) -> Credentials:
    data = object_value(tokens)
    expiry = data.get("expires_in")
    if (
        type(expiry) not in {int, float}
        or not math.isfinite(cast(float, expiry))
        or cast(float, expiry) <= 0
    ):
        raise ImportProblem("Invalid token response")
    return {
        **credential_fields(auth),
        "access_token": string_value(data.get("access_token")),
        "refresh_token": string_value(data.get("refresh_token")),
        "expires_at": time.time() + cast(float, expiry),
    }
