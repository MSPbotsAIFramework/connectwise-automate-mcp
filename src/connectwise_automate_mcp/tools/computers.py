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
"""Computers (agents): inventory, search, reboot, scripts and catalog commands."""

import re
import time
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import AutomateClient, AutomateError
from ._common import (
    ENDPOINT_ACTION,
    READ_ONLY,
    ClientFactory,
    ComputerId,
    Limit,
    OptionalClientId,
    PageLimit,
    ScriptParameters,
    Skip,
    WaitSeconds,
    and_conditions,
    compact,
    list_result,
    page_params,
    quote,
    rows,
    run_tool,
    sleep_poll,
    wait_seconds,
)
from .scripts import run_and_wait, script_request

CommandId = Annotated[str, Field(description="Catalog command id from cwautomate_commands_list.", min_length=1)]
_TERMINAL = {"success", "failed", "terminated"}
_REBOOT = re.compile(r"reboot|restart", re.IGNORECASE)


def _is_terminal(status: Any) -> bool:
    return isinstance(status, str) and status.lower() in _TERMINAL


async def execute_command(
    client: AutomateClient, computer_id: int, command_id: str, parameters: list[str], timeout: int
) -> dict:
    """Issue a catalog command and poll its execution row until it finishes or the wait ends."""
    execution = await client.post(
        f"Computers/{computer_id}/CommandExecute",
        {"ComputerId": computer_id, "Command": {"Id": command_id}, "Parameters": parameters},
    )
    execution = execution if isinstance(execution, dict) else {}
    execution_id = execution.get("Id")
    started = time.monotonic()
    poll_errors = 0
    while execution_id is not None and not _is_terminal(execution.get("Status")) and time.monotonic() - started < timeout:
        await sleep_poll()
        try:
            found = rows(await client.get(f"Computers/{computer_id}/CommandExecute", {"ids": str(execution_id)}))
        except AutomateError as e:
            if e.status_code not in (0, 429) and e.status_code < 500:
                raise
            poll_errors += 1
            continue
        execution = next((row for row in found if isinstance(row, dict) and row.get("Id") == execution_id), execution)
    completed = _is_terminal(execution.get("Status"))
    return compact(
        {
            "computer_id": computer_id,
            "command_id": command_id,
            "execution_id": execution_id,
            "completed": completed,
            "status": execution.get("Status"),
            "output": execution.get("Output"),
            "waited_seconds": round(time.monotonic() - started),
            "poll_errors": poll_errors or None,
            "note": None if completed else "Not finished yet; check again later instead of issuing it again.",
        }
    )


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_computers_list(
        client_id: OptionalClientId = None,
        location_id: Annotated[int | None, Field(description="Automate location id.", ge=1)] = None,
        status: Annotated[Literal["online", "offline", "all"] | None, Field(description="Agent status (default all).")] = None,
        limit: PageLimit = None,
        skip: Skip = None,
    ) -> str:
        """List computers (agents), optionally for one client or location, or only online / offline ones."""

        async def action(client: AutomateClient) -> dict:
            size, offset, query = page_params(limit, skip)
            condition = and_conditions(
                f"Client.Id = {client_id}" if client_id is not None else None,
                f"Location.Id = {location_id}" if location_id is not None else None,
                {"online": "Status = 'Online'", "offline": "Status = 'Offline'"}.get(status or ""),
            )
            payload = await client.get("Computers", compact({**query, "condition": condition}))
            return list_result("computers", payload, size, offset)

        return await run_tool("cwautomate_computers_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_computers_get(computer_id: ComputerId) -> str:
        """Get one computer's details: OS, status, last contact, client, location, hardware and agent info."""

        async def action(client: AutomateClient) -> Any:
            return await client.get(f"Computers/{computer_id}")

        return await run_tool("cwautomate_computers_get", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_computers_search(
        query: Annotated[str, Field(description="Text the computer name contains.", min_length=1)],
        client_id: OptionalClientId = None,
        limit: Limit = None,
    ) -> str:
        """Search computers by part of their name, optionally within one client."""

        async def action(client: AutomateClient) -> dict:
            text = query.strip()
            if not text:
                raise ValueError("query must not be blank.")
            size, offset, params = page_params(limit, None)
            condition = and_conditions(
                f"ComputerName like {quote('%' + text + '%')}",
                f"Client.Id = {client_id}" if client_id is not None else None,
            )
            payload = await client.get("Computers", {**params, "condition": condition})
            return list_result("computers", payload, size, offset)

        return await run_tool("cwautomate_computers_search", client_factory, action)

    @mcp.tool(annotations=ENDPOINT_ACTION)
    async def cwautomate_computers_reboot(
        computer_id: ComputerId,
        command_id: Annotated[str | None, Field(description="Catalog command to use (default: the catalog's reboot command).")] = None,
    ) -> str:
        """Reboot a computer through Automate's command catalog and wait for the result.

        Interrupts the user of that machine: only when the user explicitly asks
        for that computer. If completed is false, check the computer instead of
        rebooting again.
        """

        async def action(client: AutomateClient) -> dict:
            chosen, name = (command_id or "").strip() or None, None
            if chosen is None:
                catalog = [c for c in rows(await client.get("Commands")) if isinstance(c, dict)]
                reboot = next((c for c in catalog if _REBOOT.search(str(c.get("Name") or ""))), None)
                if not reboot or not reboot.get("Id"):
                    names = ", ".join(str(c.get("Name")) for c in catalog if c.get("Name")) or "(none)"
                    raise ValueError(f"No reboot / restart command in this server's catalog; pass command_id. Available: {names}")
                chosen, name = str(reboot["Id"]), reboot.get("Name")
            result = await execute_command(client, computer_id, chosen, [], wait_seconds(None))
            return compact({**result, "command_name": name})

        return await run_tool("cwautomate_computers_reboot", client_factory, action)

    @mcp.tool(annotations=ENDPOINT_ACTION)
    async def cwautomate_computers_run_script(
        computer_id: ComputerId,
        script_id: Annotated[int, Field(description="Automate script id (from cwautomate_scripts_list).", ge=1)],
        parameters: ScriptParameters = None,
    ) -> str:
        """Run a script on one computer and wait for its outcome. Changes the endpoint.

        Only when the user explicitly asks. If completed is false, read
        cwautomate_scripts_history instead of running it again.
        """

        async def action(client: AutomateClient) -> dict:
            [run] = await run_and_wait(client, [computer_id], script_request(script_id, parameters), wait_seconds(None))
            return {"script_id": script_id, **run}

        return await run_tool("cwautomate_computers_run_script", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_commands_list() -> str:
        """List the command catalog (id, name, level) used by cwautomate_computers_run_command."""

        async def action(client: AutomateClient) -> dict:
            items = rows(await client.get("Commands"))
            return {"commands": items, "count": len(items)}

        return await run_tool("cwautomate_commands_list", client_factory, action)

    @mcp.tool(annotations=ENDPOINT_ACTION)
    async def cwautomate_computers_run_command(
        computer_id: ComputerId,
        command_id: CommandId,
        parameters: Annotated[list[str] | None, Field(description="Positional parameters the command expects, in order.")] = None,
        timeout_seconds: WaitSeconds = None,
    ) -> str:
        """Send a catalog command to a computer and wait for its output. Changes the endpoint.

        Only when the user explicitly asks. The API user's command level limits
        which commands run. If completed is false, do not send it again.
        """

        async def action(client: AutomateClient) -> dict:
            return await execute_command(client, computer_id, command_id.strip(), parameters or [], wait_seconds(timeout_seconds))

        return await run_tool("cwautomate_computers_run_command", client_factory, action)
