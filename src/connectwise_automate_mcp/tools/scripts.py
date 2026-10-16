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
"""Scripts: catalog, execution on computers, history and running scripts."""

import asyncio
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
    ScriptParameters,
    Skip,
    WaitSeconds,
    and_conditions,
    compact,
    list_result,
    page_params,
    parameter_pairs,
    quote,
    rows,
    run_tool,
    sleep_poll,
    wait_seconds,
)

ScriptId = Annotated[int, Field(description="Automate script id (from cwautomate_scripts_list).", ge=1)]
_PRIORITY = {"high": 1, "normal": 2, "low": 3}
_HISTORY_WINDOW = {"orderBy": "HistoryDate desc", "pageSize": 100}


async def _history(client: AutomateClient, computer_id: int) -> list:
    return rows(await client.get(f"Computers/{computer_id}/ScriptHistory", _HISTORY_WINDOW))


def script_request(
    script_id: int,
    parameters: dict[str, str] | None,
    priority: str | None = None,
    skip_offline: bool | None = None,
    wake_offline: bool | None = None,
) -> dict:
    flags = compact({"SkipsOfflineAgents": skip_offline, "WakesOfflineAgents": wake_offline})
    return compact(
        {
            "ScriptId": script_id,
            "Parameters": parameter_pairs(parameters),
            "Priority": _PRIORITY.get(priority or ""),
            "OfflineActionFlags": flags or None,
        }
    )


async def launch_batch(client: AutomateClient, computer_ids: list[int], request: dict) -> Any:
    return await client.post("Batch/ScriptExecute", {"EntityType": "Computer", "EntityIds": computer_ids, **request})


async def run_and_wait(client: AutomateClient, computer_ids: list[int], request: dict, timeout: int) -> list[dict]:
    """Launch a script on computers and poll each one's script history for the finished run.

    Automate has no synchronous run: a run is matched as the first history
    entry for this script that was not there before the launch.
    """
    baselines = dict(
        zip(
            computer_ids,
            await asyncio.gather(*(_history(client, cid) for cid in computer_ids)),
            strict=True,
        )
    )
    seen = {cid: {entry.get("Id") for entry in history if isinstance(entry, dict)} for cid, history in baselines.items()}
    batch = await launch_batch(client, computer_ids, request) or {}
    refusals = {}
    if isinstance(batch, dict) and batch.get("ContainsUnsuccessfulResults"):
        for result in batch.get("ScriptResults") or []:
            details = result.get("ResultDetails") or {} if isinstance(result, dict) else {}
            if (details.get("ResultStatus") or 0) != 0:
                refusals[result.get("EntityId")] = details.get("Message")
    started = time.monotonic()

    async def one(computer_id: int) -> dict:
        if computer_id in refusals:
            return {"computer_id": computer_id, "launched": False, "launch_message": refusals[computer_id], "completed": False}
        poll_errors = 0
        while time.monotonic() - started < timeout:
            await sleep_poll()
            try:
                history = await _history(client, computer_id)
            except AutomateError as e:
                if e.status_code not in (0, 429) and e.status_code < 500:
                    raise
                poll_errors += 1
                continue
            match = next(
                (
                    entry
                    for entry in history
                    if isinstance(entry, dict)
                    and entry.get("Id") not in seen[computer_id]
                    and entry.get("ScriptId") == request["ScriptId"]
                    and entry.get("Status") == "Completed"
                ),
                None,
            )
            if match:
                return compact(
                    {
                        "computer_id": computer_id,
                        "launched": True,
                        "completed": True,
                        "state": match.get("State"),
                        "diagnostic_message": match.get("DiagnosticMessage"),
                        "history_id": match.get("Id"),
                        "waited_seconds": round(time.monotonic() - started),
                        "poll_errors": poll_errors or None,
                    }
                )
        return compact(
            {
                "computer_id": computer_id,
                "launched": True,
                "completed": False,
                "waited_seconds": round(time.monotonic() - started),
                "poll_errors": poll_errors or None,
                "note": "Still running; read cwautomate_scripts_history later instead of launching again.",
            }
        )

    return list(await asyncio.gather(*(one(cid) for cid in computer_ids)))


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_scripts_list(
        folder_id: Annotated[int | None, Field(description="Only scripts in this folder.", ge=1)] = None,
        search: Annotated[str | None, Field(description="Text the script name contains.")] = None,
        limit: Limit = None,
        skip: Skip = None,
    ) -> str:
        """List scripts in the Automate script catalog, optionally by folder or name."""

        async def action(client: AutomateClient) -> dict:
            size, offset, query = page_params(limit, skip)
            condition = and_conditions(
                f"Folder.Id = {folder_id}" if folder_id is not None else None,
                f"Name like {quote('%' + search.strip() + '%')}" if search and search.strip() else None,
            )
            payload = await client.get("Scripts", compact({**query, "condition": condition}))
            return list_result("scripts", payload, size, offset)

        return await run_tool("cwautomate_scripts_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_scripts_get(script_id: ScriptId) -> str:
        """Get a script's catalog entry: name, folder, comments, kind flags and accepted parameters."""

        async def action(client: AutomateClient) -> Any:
            return await client.get(f"Scripts/{script_id}")

        return await run_tool("cwautomate_scripts_get", client_factory, action)

    @mcp.tool(annotations=ENDPOINT_ACTION)
    async def cwautomate_scripts_execute(
        script_id: ScriptId,
        computer_ids: Annotated[list[int], Field(description="Computer ids to run it on.", min_length=1, max_length=100)],
        parameters: ScriptParameters = None,
        priority: Annotated[Literal["low", "normal", "high"] | None, Field(description="Default normal.")] = None,
        wait: Annotated[bool | None, Field(description="Wait for the outcome (default true); false only launches.")] = None,
        timeout_seconds: WaitSeconds = None,
        skip_offline: Annotated[bool | None, Field(description="Skip computers that are offline when it fires.")] = None,
        wake_offline: Annotated[bool | None, Field(description="Send Wake-on-LAN to offline computers first.")] = None,
    ) -> str:
        """Run a script on one or more computers and wait for each outcome. Changes endpoints.

        Only when the user explicitly asks, for the exact computers named. If a
        result is not completed, read cwautomate_scripts_history instead of
        launching again.
        """

        async def action(client: AutomateClient) -> dict:
            ids = list(dict.fromkeys(computer_ids))
            request = script_request(script_id, parameters, priority, skip_offline, wake_offline)
            if wait is False:
                batch = await launch_batch(client, ids, request) or {}
                return compact(
                    {
                        "script_id": script_id,
                        "computer_ids": ids,
                        "waited": False,
                        "launch_results": batch.get("ScriptResults") if isinstance(batch, dict) else batch,
                        "contains_unsuccessful_results": batch.get("ContainsUnsuccessfulResults") if isinstance(batch, dict) else None,
                        "message": "Script launched; read the outcome with cwautomate_scripts_history.",
                    }
                )
            timeout = wait_seconds(timeout_seconds)
            runs = await run_and_wait(client, ids, request, timeout)
            succeeded = sum(1 for r in runs if r.get("state") == "Success")
            failed = sum(1 for r in runs if not r["launched"] or r.get("state") == "Failure")
            pending = sum(1 for r in runs if r["launched"] and not r["completed"])
            return {
                "script_id": script_id,
                "summary": f"{len(runs)} target(s): {succeeded} succeeded, {failed} failed, {pending} still running after {timeout}s",
                "runs": runs,
            }

        return await run_tool("cwautomate_scripts_execute", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_scripts_history(computer_id: ComputerId, limit: Limit = None) -> str:
        """Get finished script runs on a computer, newest first, with pass/fail state and diagnostic message."""

        async def action(client: AutomateClient) -> dict:
            size = limit or 50
            payload = await client.get(f"Computers/{computer_id}/ScriptHistory", {"orderBy": "HistoryDate desc", "pageSize": size})
            return list_result("history", payload, size, 0)

        return await run_tool("cwautomate_scripts_history", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_scripts_running(computer_id: ComputerId) -> str:
        """List scripts currently running on a computer."""

        async def action(client: AutomateClient) -> dict:
            items = rows(await client.get(f"Computers/{computer_id}/RunningScripts"))
            return {"running": items, "count": len(items)}

        return await run_tool("cwautomate_scripts_running", client_factory, action)
