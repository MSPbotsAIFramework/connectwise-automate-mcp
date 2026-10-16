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
"""Shared argument types, condition / paging helpers and the error-handling wrapper."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped, error_envelope
from ..api_client import AmbiguousWriteError, AutomateClient, AutomateError
from ..config import DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS, POLL_INTERVAL_SECONDS

logger = logging.getLogger("connectwise_automate_mcp.tools")

ClientFactory = Callable[[], AutomateClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITES_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
# Reboots, scripts and commands act on live endpoints and cannot be undone.
ENDPOINT_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured",
    "Automate credentials are missing for this request (X-Cwa-Server, X-Cwa-Client-Id, X-Cwa-Username, X-Cwa-Password).",
    False,
)

DEFAULT_LIMIT = 50

Limit = Annotated[int | None, Field(description=f"Results per page, 1-1000 (default {DEFAULT_LIMIT}).", ge=1, le=1000)]
Skip = Annotated[int | None, Field(description="Results to skip; use a multiple of limit (next_skip from the last page).", ge=0)]
ComputerId = Annotated[int, Field(description="Automate computer id.", ge=1)]
OptionalClientId = Annotated[int | None, Field(description="Automate client id.", ge=1)]
WaitSeconds = Annotated[
    int | None,
    Field(description=f"How long to wait for the result (default {DEFAULT_WAIT_SECONDS}, max {MAX_WAIT_SECONDS}).", ge=0, le=MAX_WAIT_SECONDS),
]
ScriptParameters = Annotated[dict[str, str] | None, Field(description="Script parameters as name -> value.")]


def compact(values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if v is not None}


def quote(value: str) -> str:
    """A string literal for an Automate condition."""
    return "'" + value.replace("'", "''") + "'"


def and_conditions(*clauses: str | None) -> str | None:
    present = [c for c in clauses if c]
    if not present:
        return None
    return present[0] if len(present) == 1 else " and ".join(f"({c})" for c in present)


def page_params(limit: int | None, skip: int | None) -> tuple[int, int, dict[str, int]]:
    """(limit, offset actually used, query). Automate pages by page number, not offset."""
    size = limit or DEFAULT_LIMIT
    page = (skip or 0) // size + 1
    return size, (page - 1) * size, {"pageSize": size, "page": page}


def rows(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("Data"), list):
        return payload["Data"]
    return []


def list_result(key: str, payload: Any, size: int, offset: int) -> dict:
    items = rows(payload)
    more = len(items) >= size
    return compact(
        {
            key: items,
            "count": len(items),
            "skip": offset,
            "has_more": more,
            "next_skip": offset + size if more else None,
        }
    )


def parameter_pairs(parameters: dict[str, str] | None) -> list[dict[str, str]] | None:
    if not parameters:
        return None
    return [{"Key": str(k), "Value": str(v)} for k, v in parameters.items()]


def wait_seconds(value: int | None) -> int:
    return DEFAULT_WAIT_SECONDS if value is None else min(value, MAX_WAIT_SECONDS)


async def sleep_poll() -> None:
    await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[AutomateClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope,
    except a write whose connection dropped, which is reported as an unknown
    outcome so the agent checks before running it again.
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except AmbiguousWriteError as e:
        logger.warning("tool=%s outcome=ambiguous ms=%d", name, _ms(started))
        return dump_json_capped(
            {
                "tool": name,
                "completed": False,
                "interrupted": True,
                "message": f"{e.message}. Verify in Automate (history / current state) before retrying.",
            }
        )
    except AutomateError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    if result is None:
        result = {"result": None, "note": "Automate returned an empty response."}
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
