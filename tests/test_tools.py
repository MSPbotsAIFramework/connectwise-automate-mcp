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
"""tools/list snapshot and error-envelope mapping. No network access."""

import asyncio
import json

import pytest

from connectwise_automate_mcp.api_client import AutomateError
from connectwise_automate_mcp.server import INSTRUCTIONS, create_mcp_server

# Tool names are a public contract: existing agents and tenant connections
# depend on them. Changing this snapshot is a breaking change.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "cwautomate_navigate": {"domain"},
    "cwautomate_status": set(),
    "cwautomate_computers_list": set(),
    "cwautomate_computers_get": {"computer_id"},
    "cwautomate_computers_search": {"query"},
    "cwautomate_computers_reboot": {"computer_id"},
    "cwautomate_computers_run_script": {"computer_id", "script_id"},
    "cwautomate_commands_list": set(),
    "cwautomate_computers_run_command": {"computer_id", "command_id"},
    "cwautomate_clients_list": set(),
    "cwautomate_clients_get": {"client_id"},
    "cwautomate_clients_create": {"name"},
    "cwautomate_clients_update": {"client_id"},
    "cwautomate_alerts_list": set(),
    "cwautomate_alerts_get": {"alert_id"},
    "cwautomate_scripts_list": set(),
    "cwautomate_scripts_get": {"script_id"},
    "cwautomate_scripts_execute": {"script_id", "computer_ids"},
    "cwautomate_scripts_history": {"computer_id"},
    "cwautomate_scripts_running": {"computer_id"},
}

ENDPOINT_ACTIONS = {
    "cwautomate_computers_reboot",
    "cwautomate_computers_run_script",
    "cwautomate_computers_run_command",
    "cwautomate_scripts_execute",
}
RECORD_WRITES = {"cwautomate_clients_create", "cwautomate_clients_update"}


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tool_names_are_stable(tools):
    assert set(tools) == set(EXPECTED_TOOLS)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_required_params(tools, name):
    assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_description_budget(tools, name):
    description = (tools[name].description or "").strip()
    assert description, f"{name} has no description"
    assert len(description) <= 500
    assert len(description.splitlines()[0]) <= 100


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_annotations_match_side_effects(tools, name):
    annotations = tools[name].annotations
    assert annotations is not None
    writes = ENDPOINT_ACTIONS | RECORD_WRITES
    assert annotations.readOnlyHint is (name not in writes)
    assert bool(annotations.destructiveHint) is (name in ENDPOINT_ACTIONS)


@pytest.mark.parametrize("name", sorted(ENDPOINT_ACTIONS | RECORD_WRITES))
def test_write_tools_say_when_to_use_them(tools, name):
    assert "explicitly asks" in (tools[name].description or "")


def test_wait_is_capped_below_gateway_timeout(tools):
    schema = tools["cwautomate_computers_run_command"].inputSchema["properties"]["timeout_seconds"]
    maximum = next(a["maximum"] for a in schema["anyOf"] if "maximum" in a)
    assert maximum <= 100


@pytest.mark.parametrize(
    "name", ["cwautomate_computers_list", "cwautomate_clients_list", "cwautomate_alerts_list", "cwautomate_scripts_list"]
)
def test_paging_tools_cap_limit_at_200(tools, name):
    schema = tools[name].inputSchema["properties"]["limit"]
    assert next(a["maximum"] for a in schema["anyOf"] if "maximum" in a) == 200


@pytest.mark.parametrize("name", ["cwautomate_computers_search", "cwautomate_scripts_history"])
def test_single_page_tools_keep_limit_1000(tools, name):
    schema = tools[name].inputSchema["properties"]["limit"]
    assert next(a["maximum"] for a in schema["anyOf"] if "maximum" in a) == 1000


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (400, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (0, "upstream_error", True),
    ],
)
def test_error_envelope(status, code, retryable):
    envelope = json.loads(AutomateError(status, "boom").to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("cwautomate_status", {}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


def test_navigate_needs_no_credentials():
    content, _ = asyncio.run(create_mcp_server().call_tool("cwautomate_navigate", {"domain": "alerts"}))
    assert json.loads(content[0].text)["tools"] == ["cwautomate_alerts_list", "cwautomate_alerts_get"]
