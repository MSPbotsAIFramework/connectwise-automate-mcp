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
"""Credential middleware: 401/400 on bad headers, per-request isolation."""

import asyncio

import pytest
from starlette.testclient import TestClient

from connectwise_automate_mcp.__main__ import build_http_app
from connectwise_automate_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
    normalize_server_url,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {
    "X-Cwa-Server": "https://your-server.hostedrmm.com",
    "X-Cwa-Client-Id": "client-id",
    "X-Cwa-Username": "integrator",
    "X-Cwa-Password": "password",
}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("drop", list(CREDS))
def test_each_required_header_is_required(drop):
    headers = {k: v for k, v in CREDS.items() if k != drop}
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert drop in resp.json()["message"]


def test_placeholder_values_count_as_missing():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | {"X-Cwa-Password": "${CWA_PASSWORD}"})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "extra",
    [{"X-Cwa-Server": "http://automate.example.com"}, {"X-Cwa-Auth-Method": "token"}],
)
def test_bad_values_return_400(extra):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS | extra)
    assert resp.status_code == 400


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("cwautomate_navigate", "cwautomate_computers_list", "cwautomate_scripts_execute", "cwautomate_alerts_get"):
        assert name in resp.text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://your-server.hostedrmm.com", "https://your-server.hostedrmm.com"),
        ("your-server.hostedrmm.com/", "https://your-server.hostedrmm.com"),
        ("https://rmm.example.com/cwa/api/v1/", "https://rmm.example.com"),
        ("https://rmm.example.com/automate", "https://rmm.example.com"),
        ("https://rmm.example.com:8443/rmm", "https://rmm.example.com:8443/rmm"),
        ("http://localhost:5000", "http://localhost:5000"),
        ("http://rmm.example.com", None),
        ("https://", None),
    ],
)
def test_normalize_server_url(value, expected):
    assert normalize_server_url(value) == expected


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def _raw(extra: dict[str, str] | None = None) -> list[tuple[bytes, bytes]]:
    return [(k.lower().encode(), v.encode()) for k, v in (CREDS | (extra or {})).items()]


def test_headers_reach_request_context_and_are_reset():
    seen = _run_middleware(_raw({"X-Cwa-Server": "rmm.example.com/cwa/api/v1"}))
    creds = seen["credentials"]
    assert (creds.client_id, creds.username, creds.password) == ("client-id", "integrator", "password")
    assert creds.resolved_auth_method == "integrator"
    assert seen["client"].server_url == "https://rmm.example.com"
    assert _credentials_var.get() is None


def test_two_factor_code_implies_user_auth_unless_told_otherwise():
    assert _run_middleware(_raw({"X-Cwa-2fa": "123 456"}))["credentials"].resolved_auth_method == "user"
    seen = _run_middleware(_raw({"X-Cwa-2fa": "123456", "X-Cwa-Auth-Method": "Integrator"}))
    assert seen["credentials"].resolved_auth_method == "integrator"


def test_concurrent_requests_do_not_share_credentials():
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().username
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().username))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(user: str) -> dict:
        return {"type": "http", "path": "/mcp", "headers": _raw({"X-Cwa-Username": user})}

    async def both():
        await asyncio.gather(middleware(scope("tenant-a"), receive, send), middleware(scope("tenant-b"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a", "tenant-a"), ("tenant-b", "tenant-b")]


def test_credentials_repr_hides_secrets():
    seen = _run_middleware(_raw({"X-Cwa-Password": "very-secret-value", "X-Cwa-2fa": "424242"}))
    text = repr(seen["credentials"])
    assert "very-secret-value" not in text and "424242" not in text and "client-id" not in text
