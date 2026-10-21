# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Async client for the ConnectWise Automate REST API."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from ._json import error_envelope
from .config import API_ROOT

logger = logging.getLogger("connectwise_automate_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 2
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

AuthMethod = Literal["integrator", "user"]

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own token, so
# sharing the pool across tenants is safe.
_shared_http_client: httpx.AsyncClient | None = None


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _shared_http_client


# status_code -> (error code, retryable). 0 means no HTTP response at all
# (connection error or timeout).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    422: ("invalid_argument", False),
    429: ("rate_limited", True),
}


def classify_status(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class AutomateError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Automate API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify_status(self.status_code)
        return error_envelope(code, self.message, retryable)


class AmbiguousWriteError(AutomateError):
    """A write lost its connection before a response: it may or may not have run."""

    def __init__(self, method: str, path: str):
        super().__init__(0, f"Connection to Automate dropped during {method} {path}; the request may or may not have run")


@dataclass(frozen=True)
class AutomateCredentials:
    server_url: str
    client_id: str
    username: str
    password: str
    two_factor_code: str | None = None
    auth_method: AuthMethod | None = None
    # Set when a header value is malformed: tools then return invalid_argument
    # instead of calling Automate, while tools/list still works.
    header_error: str | None = None

    @property
    def resolved_auth_method(self) -> AuthMethod:
        if self.auth_method:
            return self.auth_method
        return "user" if self.two_factor_code else "integrator"

    def __repr__(self) -> str:  # never leak secrets into logs or tracebacks
        return (
            f"AutomateCredentials(server_url={self.server_url!r}, auth_method={self.resolved_auth_method!r}, "
            "secrets=<redacted>)"
        )


class AutomateClient:
    """Automate API wrapper for one caller's credentials.

    The access token is requested lazily and kept only on this instance, which
    lives for a single MCP request; nothing is cached across requests.
    """

    def __init__(self, credentials: AutomateCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.header_error = credentials.header_error
        self.server_url = credentials.server_url.rstrip("/")
        self._token: str | None = None

    def _client(self) -> httpx.AsyncClient:
        if self.header_error:
            # A malformed header (e.g. a non-https server URL) must never reach the network.
            raise AutomateError(400, self.header_error)
        return self._http or _get_shared_http_client()

    def _url(self, path: str, version: str = "v1") -> str:
        return f"{self.server_url}{API_ROOT.format(version=version)}/{path.lstrip('/')}"

    async def _authenticate(self) -> str:
        creds = self._credentials
        body = {"UserName": creds.username, "Password": creds.password}
        if creds.resolved_auth_method == "user" and creds.two_factor_code:
            body["TwoFactorPasscode"] = "".join(creds.two_factor_code.split())
        headers = {"Accept": "application/json", "ClientId": creds.client_id}
        try:
            resp = await self._client().post(self._url("apitoken"), json=body, headers=headers)
        except httpx.RequestError as e:
            raise AutomateError(0, f"Network error requesting an Automate API token: {type(e).__name__}") from e
        logger.info("automate_api method=POST path=apitoken status=%s", resp.status_code)
        if resp.status_code in (400, 401, 403):
            raise AutomateError(401, "Automate rejected the username / password or client id (token request failed)")
        if resp.status_code >= 400:
            raise AutomateError(resp.status_code, f"Automate token endpoint returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        token = data.get("AccessToken") if isinstance(data, dict) else None
        if not token:
            if isinstance(data, dict) and (data.get("IsTwoFactorRequired") or data.get("IsInternalTwoFactorRequired")):
                raise AutomateError(
                    401,
                    "This Automate account requires two-factor authentication; use an integrator account "
                    "(X-Cwa-Auth-Method: integrator) or send a current X-Cwa-2fa code",
                )
            raise AutomateError(502, "Automate token response had no AccessToken")
        return token

    async def verify(self) -> None:
        """Obtain a token, proving the server URL, client id and login work."""
        await self._bearer()

    @property
    def auth_method(self) -> AuthMethod:
        return self._credentials.resolved_auth_method

    async def _bearer(self, renew: bool = False) -> str:
        if renew or not self._token:
            self._token = await self._authenticate()
        return self._token

    # ---- requests -------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None, version: str = "v1") -> Any:
        return await self.request("GET", path, params=params, version=version)

    async def post(self, path: str, body: Any) -> Any:
        return await self.request("POST", path, body=body)

    async def patch(self, path: str, body: Any) -> Any:
        return await self.request("PATCH", path, body=body)

    async def request(
        self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None, version: str = "v1"
    ) -> Any:
        """Call ``{server}/cwa/api/{version}/{path}``; on 401 gets a new token once and retries."""
        resp = await self._send(method, path, params, body, version, await self._bearer())
        if resp.status_code == 401:
            resp = await self._send(method, path, params, body, version, await self._bearer(renew=True))
        if resp.status_code >= 400:
            raise AutomateError(resp.status_code, _error_message(resp, path))
        return _parse_body(resp)

    async def _send(
        self, method: str, path: str, params: dict[str, Any] | None, body: Any, version: str, token: str
    ) -> httpx.Response:
        url = self._url(path, version)
        query = {k: _param_value(v) for k, v in (params or {}).items() if v is not None}
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "ClientId": self._credentials.client_id}
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            error: AutomateError | None = None
            try:
                resp = await self._client().request(method, url, params=query, json=body, headers=headers)
                status = resp.status_code
            except httpx.RequestError as e:
                status = 0
                if method != "GET":
                    error = AmbiguousWriteError(method, path)
                elif isinstance(e, httpx.TimeoutException):
                    error = AutomateError(0, f"Timed out calling Automate ({method} {path})")
                else:
                    error = AutomateError(0, f"Network error calling Automate ({method} {path}): {type(e).__name__}")

            logger.info(
                "automate_api method=%s path=%s status=%s ms=%d attempt=%d",
                method, path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            # 429 is refused before processing, so any method may retry it; other
            # failures are retried only for reads, so a write never runs twice.
            retryable = status == 429 or (method == "GET" and (status == 0 or status >= 500))
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue
            if error is not None:
                raise error
            assert resp is not None
            return resp


def _param_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _parse_body(resp: httpx.Response) -> Any:
    if not resp.content or not resp.content.strip():
        return None
    try:
        return resp.json()
    except ValueError:
        return {"raw_response": resp.text[:2000]}


def _error_message(resp: httpx.Response, path: str) -> str:
    fallback = f"Automate returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return f"{fallback}: {text[:300]}" if text else fallback
    if isinstance(payload, dict):
        parts = []
        for key in ("Message", "message", "Error", "error", "ExceptionMessage"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
                break
        state = payload.get("ModelState")
        if isinstance(state, dict):
            for field, problems in state.items():
                for problem in problems if isinstance(problems, list) else [problems]:
                    parts.append(f"{field}: {problem}")
        if parts:
            return f"{fallback}: {' - '.join(parts)[:500]}"
    return fallback
