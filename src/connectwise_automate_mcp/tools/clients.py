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
"""Clients (the MSP's customers) and their locations."""

import asyncio
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ..api_client import AutomateClient
from ._common import (
    READ_ONLY,
    WRITES,
    WRITES_IDEMPOTENT,
    ClientFactory,
    PageLimit,
    Skip,
    compact,
    list_result,
    page_params,
    rows,
    run_tool,
)

ClientId = Annotated[int, Field(description="Automate client id.", ge=1)]
Text = Annotated[str | None, Field(description="New value.")]


def _client_fields(name: str | None, city: str | None, state: str | None, zip: str | None, country: str | None, phone: str | None) -> dict:
    return compact({"Name": name, "City": city, "State": state, "ZipCode": zip, "Country": country, "PhoneNumber": phone})


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_clients_list(limit: PageLimit = None, skip: Skip = None) -> str:
        """List clients (customers) with their ids, names and contact details."""

        async def action(client: AutomateClient) -> dict:
            size, offset, query = page_params(limit, skip)
            return list_result("clients", await client.get("Clients", {**query, "orderBy": "Name asc"}), size, offset)

        return await run_tool("cwautomate_clients_list", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def cwautomate_clients_get(
        client_id: ClientId,
        include_locations: Annotated[bool | None, Field(description="Also return the client's locations.")] = None,
    ) -> str:
        """Get one client by id, optionally with its locations."""

        async def action(client: AutomateClient) -> Any:
            if not include_locations:
                return await client.get(f"Clients/{client_id}")
            record, locations = await asyncio.gather(
                client.get(f"Clients/{client_id}"),
                client.get("Locations", {"condition": f"Client.Id = {client_id}", "pageSize": 1000}),
            )
            base = record if isinstance(record, dict) else {"client": record}
            return {**base, "locations": rows(locations)}

        return await run_tool("cwautomate_clients_get", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def cwautomate_clients_create(
        name: Annotated[str, Field(description="Client name.", min_length=1)],
        city: Annotated[str | None, Field(description="City.")] = None,
        state: Annotated[str | None, Field(description="State / province.")] = None,
        zip: Annotated[str | None, Field(description="ZIP / postal code.")] = None,
        country: Annotated[str | None, Field(description="Country.")] = None,
        phone: Annotated[str | None, Field(description="Phone number.")] = None,
    ) -> str:
        """Create a client in Automate. Only when the user explicitly asks."""

        async def action(client: AutomateClient) -> Any:
            if not name.strip():
                raise ValueError("name must not be blank.")
            return await client.post("Clients", _client_fields(name.strip(), city, state, zip, country, phone))

        return await run_tool("cwautomate_clients_create", client_factory, action)

    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def cwautomate_clients_update(
        client_id: ClientId,
        name: Text = None,
        city: Text = None,
        state: Text = None,
        zip: Text = None,
        country: Text = None,
        phone: Text = None,
    ) -> str:
        """Change a client's name or contact details; only the fields given change.

        Only when the user explicitly asks.
        """

        async def action(client: AutomateClient) -> Any:
            fields = _client_fields(name, city, state, zip, country, phone)
            if not fields:
                raise ValueError("Nothing to update: pass at least one field to change.")
            # PATCH /Clients/{id} takes JSON Patch operations.
            operations = [{"Op": "replace", "Path": f"/{key}", "Value": value} for key, value in fields.items()]
            return await client.patch(f"Clients/{client_id}", operations)

        return await run_tool("cwautomate_clients_update", client_factory, action)
