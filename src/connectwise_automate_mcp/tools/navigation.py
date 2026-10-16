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
"""Tool discovery (kept for compatibility) and the connection check."""

from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .._json import compact as to_json
from ..api_client import AutomateClient
from ._common import LOCAL, READ_ONLY, ClientFactory, run_tool

DOMAINS: dict[str, tuple[str, list[str]]] = {
    "computers": (
        "Endpoints: list, get, search; reboot, run scripts and catalog commands (changes endpoints).",
        [
            "cwautomate_computers_list",
            "cwautomate_computers_get",
            "cwautomate_computers_search",
            "cwautomate_computers_reboot",
            "cwautomate_computers_run_script",
            "cwautomate_commands_list",
            "cwautomate_computers_run_command",
        ],
    ),
    "clients": (
        "Clients (customers): list, get with locations, create, update.",
        ["cwautomate_clients_list", "cwautomate_clients_get", "cwautomate_clients_create", "cwautomate_clients_update"],
    ),
    "alerts": ("Alerts (read-only in the Automate API).", ["cwautomate_alerts_list", "cwautomate_alerts_get"]),
    "scripts": (
        "Script catalog, execution on computers (changes endpoints), history and running scripts.",
        [
            "cwautomate_scripts_list",
            "cwautomate_scripts_get",
            "cwautomate_scripts_execute",
            "cwautomate_scripts_history",
            "cwautomate_scripts_running",
        ],
    ),
}


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=LOCAL)
    async def cwautomate_navigate(
        domain: Annotated[Literal["computers", "clients", "alerts", "scripts"], Field(description="Area to describe.")],
    ) -> str:
        """Describe the tools of one Automate area. All tools are already callable; this changes nothing."""
        summary, tools = DOMAINS[domain]
        return to_json({"domain": domain, "summary": summary, "tools": tools})

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_status() -> str:
        """Check the Automate connection (server, auth method, login works) and list the tool areas."""

        async def action(client: AutomateClient) -> dict:
            await client.verify()
            return {
                "connected": True,
                "server": client.server_url,
                "auth_method": client.auth_method,
                "domains": {name: summary for name, (summary, _tools) in DOMAINS.items()},
            }

        return await run_tool("cwautomate_status", client_factory, action)
