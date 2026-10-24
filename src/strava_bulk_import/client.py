"""HTTPS-only transport and OAuth credential handling. No automatic write retries."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .archive import ImportProblem


class APIError(ImportProblem):
    def __init__(self, status: int, code: str):
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


def save_private(path: Path, data: dict[str, Any]) -> None:
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
    ):
        self.base = origin(base)
        self.credentials = credentials
        self.readonly = readonly
        self.http = httpx.Client(timeout=180, follow_redirects=False, transport=transport)
        try:
            self.auth = json.loads(credentials.read_text())
        except (OSError, ValueError) as exc:
            self.http.close()
            raise ImportProblem("Run login first; credential file is missing or invalid") from exc
        if not isinstance(self.auth, dict) or self.auth.get("server") != self.base:
            self.http.close()
            raise ImportProblem("Credentials belong to a different server")

    def close(self) -> None:
        self.http.close()

    def refresh(self) -> None:
        if self.readonly:
            raise ImportProblem("Token expired: run login or refresh outside --dryrun")
        fields = {key: self.auth[key] for key in ("client_id", "client_secret", "refresh_token")}
        fields["grant_type"] = "refresh_token"
        try:
            response = self.http.post(self.base + "/api/oauth/token", data=fields)
        except httpx.HTTPError as exc:
            raise ImportProblem(
                "Token refresh failed; log in again if the outcome is uncertain"
            ) from exc
        if response.status_code != 200:
            raise ImportProblem("Token refresh rejected; run login again")
        update_tokens(self.auth, response.json())
        save_private(self.credentials, self.auth)

    def request(
        self,
        method: str,
        path: str,
        *,
        missing_ok: bool = False,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
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
                method, self.base + path, headers=request_headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ImportProblem("Network failure; journal preserves any uncertain upload") from exc
        if missing_ok and response.status_code == 404:
            return None
        if not response.is_success:
            # Expose only known companion codes, never private response bodies.
            code = "request_rejected"
            try:
                candidate = response.json().get("error")
                if candidate in {
                    "claim_busy",
                    "claim_expired",
                    "claim_expired_use_new_token",
                    "identity_already_mapped",
                    "storage_unavailable",
                }:
                    code = candidate
            except (ValueError, AttributeError):
                pass
            raise APIError(response.status_code, code)
        if response.status_code == 204:
            return {}
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError as exc:
            raise ImportProblem("Invalid API response; inspect journal before retrying") from exc


def update_tokens(auth: dict[str, Any], tokens: dict[str, Any]) -> None:
    if (
        not isinstance(tokens.get("access_token"), str)
        or not isinstance(tokens.get("refresh_token"), str)
        or not isinstance(tokens.get("expires_in"), (int, float))
        or not math.isfinite(tokens["expires_in"])
        or tokens["expires_in"] <= 0
    ):
        raise ImportProblem("Invalid token response")
    auth.update(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=time.time() + tokens["expires_in"],
    )
