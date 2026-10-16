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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import AutomateClient, AutomateCredentials
from .config import Settings

HEADER_SERVER = "X-Cwa-Server"
HEADER_CLIENT_ID = "X-Cwa-Client-Id"
HEADER_USERNAME = "X-Cwa-Username"
HEADER_PASSWORD = "X-Cwa-Password"
HEADER_2FA = "X-Cwa-2fa"
HEADER_AUTH_METHOD = "X-Cwa-Auth-Method"
REQUIRED_HEADERS = [HEADER_SERVER, HEADER_CLIENT_ID, HEADER_USERNAME, HEADER_PASSWORD]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")
_API_SUFFIX = re.compile(r"/(cwa(/api(/v\d+)?)?|automate)/?$", re.IGNORECASE)
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}

# Per-request isolation: the middleware sets these for the duration of one
# HTTP request and resets them in `finally`. Nothing credential-related is
# ever stored at module level.
_credentials_var: contextvars.ContextVar[AutomateCredentials | None] = contextvars.ContextVar(
    "automate_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("automate_request_id", default="-")

INSTRUCTIONS = (
    "ConnectWise Automate is an RMM: it monitors MSP-managed computers "
    "(agents) grouped by client and location, raises alerts, and runs scripts "
    "and commands on endpoints.\n"
    "Usage:\n"
    "- Find a machine: cwautomate_computers_search(query=name) or "
    "cwautomate_computers_list(client_id, status); client ids come from "
    "cwautomate_clients_list.\n"
    "- Lists page with limit + skip (skip a multiple of limit); has_more and "
    "next_skip tell you whether to page on.\n"
    "- Alerts: cwautomate_alerts_list(computer_id or client_id). Alerts are "
    "read-only in the Automate API.\n"
    "- Scripts: cwautomate_scripts_list(search) -> cwautomate_scripts_get "
    "(parameters) -> cwautomate_scripts_execute; results via "
    "cwautomate_scripts_history.\n"
    "- Commands: cwautomate_commands_list gives catalog ids for "
    "cwautomate_computers_run_command.\n"
    "Rebooting, running scripts or commands, and creating or updating "
    "clients change live endpoints and records: call those tools only when "
    "the user explicitly asks, for the exact targets named. When a result "
    "says completed: false or interrupted: true, check history before "
    "running anything again."
)


def get_client_from_context() -> AutomateClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return AutomateClient(credentials)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def normalize_server_url(value: str) -> str | None:
    """https origin (+ path prefix) of the Automate server, or None when unusable."""
    text = value.strip()
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if not parts.hostname:
        return None
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        return None
    path = _API_SUFFIX.sub("", parts.path.rstrip("/"))
    return f"{parts.scheme}://{parts.netloc}{path}".rstrip("/")


class GatewayCredentialsMiddleware:
    """Reads the Automate headers on /mcp requests into the request context.

    Needs X-Cwa-Server, X-Cwa-Client-Id, X-Cwa-Username and X-Cwa-Password;
    otherwise 401.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        def header(name: str) -> str | None:
            return _clean(request.headers.get(name))

        values = {name: header(name) for name in REQUIRED_HEADERS}
        missing = [name for name, value in values.items() if not value]
        if missing:
            await JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": f"Missing header(s): {', '.join(missing)}",
                    "required_headers": REQUIRED_HEADERS,
                    "optional_headers": [HEADER_2FA, HEADER_AUTH_METHOD],
                },
                status_code=401,
            )(scope, receive, send)
            return

        server_url = normalize_server_url(values[HEADER_SERVER] or "")
        auth_method = (header(HEADER_AUTH_METHOD) or "").lower() or None
        if server_url is None:
            await self._reject(scope, receive, send, f"{HEADER_SERVER} must be an https URL such as https://your-server.hostedrmm.com")
            return
        if auth_method not in (None, "integrator", "user"):
            await self._reject(scope, receive, send, f"{HEADER_AUTH_METHOD} must be integrator or user")
            return

        credentials = AutomateCredentials(
            server_url=server_url,
            client_id=values[HEADER_CLIENT_ID] or "",
            username=values[HEADER_USERNAME] or "",
            password=values[HEADER_PASSWORD] or "",
            two_factor_code=header(HEADER_2FA),
            auth_method=auth_method,  # type: ignore[arg-type]
        )
        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(credentials)
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, message: str) -> None:
        await JSONResponse({"error": "Invalid header", "message": message}, status_code=400)(scope, receive, send)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = FastMCP(
        name="connectwise-automate-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], AutomateClient | None] = get_client_from_context

    from .tools import alerts, clients, computers, navigation, scripts

    for module in (navigation, computers, clients, alerts, scripts):
        module.register(mcp, client_factory)

    return mcp
