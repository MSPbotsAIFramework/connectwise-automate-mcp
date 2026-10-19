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
"""Alerts raised by Automate monitors (read-only in the API)."""

from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import AutomateClient
from ._common import (
    READ_ONLY,
    ClientFactory,
    OptionalClientId,
    PageLimit,
    Skip,
    and_conditions,
    compact,
    list_result,
    page_params,
    run_tool,
)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_alerts_list(
        computer_id: Annotated[int | None, Field(description="Only alerts of this computer.", ge=1)] = None,
        client_id: OptionalClientId = None,
        severity: Annotated[int | None, Field(description="Only alerts with this severity id (Severity.Id).")] = None,
        limit: PageLimit = None,
        skip: Skip = None,
    ) -> str:
        """List alerts, optionally for one computer, client or severity.

        Alerts are read-only in the Automate API: they cannot be acknowledged or closed here.
        """

        async def action(client: AutomateClient) -> dict:
            size, offset, query = page_params(limit, skip)
            condition = and_conditions(
                f"Client.Id = {client_id}" if client_id is not None else None,
                f"Severity.Id = {severity}" if severity is not None else None,
            )
            path = f"Computers/{computer_id}/Alerts" if computer_id is not None else "Alerts"
            payload = await client.get(path, compact({**query, "condition": condition}))
            return list_result("alerts", payload, size, offset)

        return await run_tool("cwautomate_alerts_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_alerts_get(alert_id: Annotated[int, Field(description="Alert id.", ge=1)]) -> str:
        """Get one alert by id: message, severity, computer, client and time."""

        async def action(client: AutomateClient) -> Any:
            return await client.get(f"Alerts/{alert_id}")

        return await run_tool("cwautomate_alerts_get", client_factory, action)
