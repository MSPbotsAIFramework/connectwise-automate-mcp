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
"""Authentication, retries, polling and tool request shapes against a mocked Automate API."""

import json

import httpx
import pytest

from connectwise_automate_mcp import api_client
from connectwise_automate_mcp.api_client import AutomateClient, AutomateCredentials, AutomateError
from connectwise_automate_mcp.server import _credentials_var, create_mcp_server
from connectwise_automate_mcp.tools import _common

SERVER = "https://rmm.example.com"
CREDS = AutomateCredentials(server_url=SERVER, client_id="client-id", username="integrator", password="pw")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)
    monkeypatch.setattr(_common.asyncio, "sleep", instant)


def token_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"AccessToken": "tok-1", "TokenType": "Bearer", "ExpirationDate": "2099-01-01T00:00:00"})


class Recorder:
    """Mock transport: replies by path below /cwa/api/v1/ (or v2/)."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = {"apitoken": token_ok} | (routes or {})
        self.default = default if default is not None else []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.split("/cwa/api/", 1)[1].split("/", 1)[1]
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [
            r for r in self.requests if r.url.path.endswith(f"/{path}") and (method is None or r.method == method)
        ]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict, credentials: AutomateCredentials = CREDS):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(credentials)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder, credentials: AutomateCredentials = CREDS) -> AutomateClient:
    return AutomateClient(credentials, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- authentication --------------------------------------------------------


async def test_integrator_token_request_and_bearer():
    rec = Recorder({"Clients": [{"Id": 1}]})
    assert await _client(rec).get("Clients", {"pageSize": 5}) == [{"Id": 1}]
    token_request, api_request = rec.requests
    assert str(token_request.url) == f"{SERVER}/cwa/api/v1/apitoken"
    assert token_request.headers["ClientId"] == "client-id"
    assert body(token_request) == {"UserName": "integrator", "Password": "pw"}
    assert api_request.headers["Authorization"] == "Bearer tok-1"
    assert api_request.headers["ClientId"] == "client-id"
    assert str(api_request.url) == f"{SERVER}/cwa/api/v1/Clients?pageSize=5"


async def test_user_auth_sends_two_factor_passcode():
    creds = AutomateCredentials(server_url=SERVER, client_id="c", username="tech", password="pw", two_factor_code="123 456")
    rec = Recorder()
    await _client(rec, creds).get("Clients")
    assert body(rec.requests[0]) == {"UserName": "tech", "Password": "pw", "TwoFactorPasscode": "123456"}


async def test_two_factor_required_is_explained(monkeypatch):
    rec = Recorder({"apitoken": lambda r: httpx.Response(200, json={"IsTwoFactorRequired": True})})
    result = await _call(monkeypatch, rec, "cwautomate_status", {})
    assert result["error"]["code"] == "unauthorized"
    assert "two-factor" in result["error"]["message"]


async def test_bad_login_is_unauthorized_without_leaking_secrets(monkeypatch):
    rec = Recorder({"apitoken": lambda r: httpx.Response(401, json={"Message": "bad"})})
    creds = AutomateCredentials(server_url=SERVER, client_id="cid-value-1", username="user-value-2", password="pw-value-3")
    result = await _call(monkeypatch, rec, "cwautomate_status", {}, creds)
    assert result["error"]["code"] == "unauthorized"
    text = json.dumps(result)
    assert not any(v in text for v in ("cid-value-1", "user-value-2", "pw-value-3"))


async def test_401_gets_a_new_token_once():
    tokens = iter(["tok-1", "tok-2"])
    rec = Recorder(
        {
            "apitoken": lambda r: httpx.Response(200, json={"AccessToken": next(tokens)}),
            "Clients": lambda r: httpx.Response(200 if r.headers["Authorization"] == "Bearer tok-2" else 401, json=[]),
        }
    )
    assert await _client(rec).get("Clients") == []
    assert len(rec.calls("apitoken")) == 2


async def test_status_reports_connection(monkeypatch):
    result = await _call(monkeypatch, Recorder(), "cwautomate_status", {})
    assert result["connected"] is True
    assert result["auth_method"] == "integrator"
    assert result["server"] == SERVER


# ---- retries ---------------------------------------------------------------


async def test_get_retries_5xx_then_succeeds():
    replies = iter([httpx.Response(503), httpx.Response(200, json=[])])
    rec = Recorder({"Clients": lambda r: next(replies)})
    assert await _client(rec).get("Clients") == []
    assert len(rec.calls("Clients")) == 2


async def test_post_is_not_retried_on_5xx():
    rec = Recorder({"Clients": lambda r: httpx.Response(500, json={"Message": "boom"})})
    with pytest.raises(AutomateError) as err:
        await _client(rec).post("Clients", {"Name": "x"})
    assert err.value.status_code == 500
    assert "boom" in err.value.message
    assert len(rec.calls("Clients")) == 1


async def test_dropped_write_is_reported_as_unknown_outcome(monkeypatch):
    def drop(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    rec = Recorder({"POST Computers/7/CommandExecute": drop})
    result = await _call(monkeypatch, rec, "cwautomate_computers_run_command", {"computer_id": 7, "command_id": "2"})
    assert result["completed"] is False
    assert result["interrupted"] is True
    assert len(rec.calls("Computers/7/CommandExecute", "POST")) == 1


# ---- list tools ------------------------------------------------------------


async def test_computers_list_builds_condition_and_paging(monkeypatch):
    rec = Recorder({"Computers": [{"Id": i} for i in range(25)]})
    result = await _call(monkeypatch, rec, "cwautomate_computers_list", {"client_id": 4, "status": "online", "limit": 25, "skip": 50})
    params = rec.calls("Computers")[0].url.params
    assert params["condition"] == "(Client.Id = 4) and (Status = 'Online')"
    assert (params["pageSize"], params["page"]) == ("25", "3")
    assert (result["count"], result["has_more"], result["next_skip"], result["skip"]) == (25, True, 75, 50)


async def test_last_page_has_no_next_skip(monkeypatch):
    rec = Recorder({"Computers": [{"Id": 1}]})
    result = await _call(monkeypatch, rec, "cwautomate_computers_list", {})
    assert result["has_more"] is False and "next_skip" not in result
    assert "condition" not in rec.calls("Computers")[0].url.params


async def test_search_escapes_quotes(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "cwautomate_computers_search", {"query": "O'Brien-PC", "client_id": 2})
    condition = rec.calls("Computers")[0].url.params["condition"]
    assert condition == "(ComputerName like '%O''Brien-PC%') and (Client.Id = 2)"


async def test_alerts_for_a_computer_use_its_route(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "cwautomate_alerts_list", {"computer_id": 9, "severity": 3})
    request = rec.calls("Computers/9/Alerts")[0]
    assert request.url.params["condition"] == "Severity.Id = 3"


async def test_client_with_locations(monkeypatch):
    rec = Recorder({"Clients/3": {"Id": 3, "Name": "Example"}, "Locations": [{"Id": 30}]})
    result = await _call(monkeypatch, rec, "cwautomate_clients_get", {"client_id": 3, "include_locations": True})
    assert result == {"Id": 3, "Name": "Example", "locations": [{"Id": 30}]}
    assert rec.calls("Locations")[0].url.params["condition"] == "Client.Id = 3"


async def test_client_update_sends_json_patch(monkeypatch):
    rec = Recorder({"Clients/3": {"Id": 3}})
    await _call(monkeypatch, rec, "cwautomate_clients_update", {"client_id": 3, "city": "Springfield", "zip": "12345"})
    request = rec.calls("Clients/3", "PATCH")[0]
    assert body(request) == [
        {"Op": "replace", "Path": "/City", "Value": "Springfield"},
        {"Op": "replace", "Path": "/ZipCode", "Value": "12345"},
    ]


async def test_client_update_without_changes_is_rejected(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "cwautomate_clients_update", {"client_id": 3})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_scripts_list_filters(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "cwautomate_scripts_list", {"folder_id": 5, "search": "disk"})
    assert rec.calls("Scripts")[0].url.params["condition"] == "(Folder.Id = 5) and (Name like '%disk%')"


# ---- commands --------------------------------------------------------------


async def test_run_command_polls_until_terminal(monkeypatch):
    polls = iter([[{"Id": 55, "Status": "Executing"}], [{"Id": 55, "Status": "Success", "Output": "done"}]])
    rec = Recorder(
        {
            "POST Computers/7/CommandExecute": {"Id": 55, "Status": "Pending"},
            "GET Computers/7/CommandExecute": lambda r: httpx.Response(200, json=next(polls)),
        }
    )
    result = await _call(monkeypatch, rec, "cwautomate_computers_run_command", {"computer_id": 7, "command_id": "17", "parameters": ["a"]})
    assert body(rec.calls("Computers/7/CommandExecute", "POST")[0]) == {"ComputerId": 7, "Command": {"Id": "17"}, "Parameters": ["a"]}
    assert rec.calls("Computers/7/CommandExecute", "GET")[0].url.params["ids"] == "55"
    assert (result["completed"], result["status"], result["output"], result["execution_id"]) == (True, "Success", "done", 55)


async def test_run_command_timeout_returns_partial_result(monkeypatch):
    rec = Recorder({"POST Computers/7/CommandExecute": {"Id": 56, "Status": "Pending"}})
    result = await _call(monkeypatch, rec, "cwautomate_computers_run_command", {"computer_id": 7, "command_id": "17", "timeout_seconds": 0})
    assert result["completed"] is False
    assert result["execution_id"] == 56
    assert "note" in result


async def test_reboot_picks_the_catalog_reboot_command(monkeypatch):
    rec = Recorder(
        {
            "Commands": [{"Id": "1", "Name": "Get Info"}, {"Id": "9", "Name": "Reboot Computer"}],
            "POST Computers/7/CommandExecute": {"Id": 60, "Status": "Success"},
        }
    )
    result = await _call(monkeypatch, rec, "cwautomate_computers_reboot", {"computer_id": 7})
    assert body(rec.calls("Computers/7/CommandExecute", "POST")[0])["Command"] == {"Id": "9"}
    assert result["command_name"] == "Reboot Computer"
    assert result["completed"] is True


async def test_reboot_without_catalog_entry_lists_commands(monkeypatch):
    rec = Recorder({"Commands": [{"Id": "1", "Name": "Get Info"}]})
    result = await _call(monkeypatch, rec, "cwautomate_computers_reboot", {"computer_id": 7})
    assert result["error"]["code"] == "invalid_argument"
    assert "Get Info" in result["error"]["message"]
    assert rec.calls("Computers/7/CommandExecute") == []


# ---- scripts ---------------------------------------------------------------


async def test_script_execute_waits_for_new_history_entries(monkeypatch):
    history = {7: iter([[{"Id": 1, "ScriptId": 300, "Status": "Completed"}],
                        [{"Id": 1, "ScriptId": 300, "Status": "Completed"}],
                        [{"Id": 2, "ScriptId": 300, "Status": "Completed", "State": "Success", "DiagnosticMessage": "ok"}]]),
               8: iter([[]])}

    def script_history(request: httpx.Request) -> httpx.Response:
        computer = int(request.url.path.split("/Computers/")[1].split("/")[0])
        return httpx.Response(200, json=next(history[computer], []))

    batch = {"ContainsUnsuccessfulResults": True, "ScriptResults": [{"EntityId": 8, "ResultDetails": {"ResultStatus": 1, "Message": "offline"}}]}
    rec = Recorder(
        {
            "Computers/7/ScriptHistory": script_history,
            "Computers/8/ScriptHistory": script_history,
            "POST Batch/ScriptExecute": batch,
        }
    )
    args = {"script_id": 300, "computer_ids": [7, 8], "parameters": {"Drive": "C"}, "priority": "high"}
    result = await _call(monkeypatch, rec, "cwautomate_scripts_execute", args)
    assert body(rec.calls("Batch/ScriptExecute", "POST")[0]) == {
        "EntityType": "Computer",
        "EntityIds": [7, 8],
        "ScriptId": 300,
        "Parameters": [{"Key": "Drive", "Value": "C"}],
        "Priority": 1,
    }
    runs = {r["computer_id"]: r for r in result["runs"]}
    assert runs[7]["completed"] is True and runs[7]["state"] == "Success" and runs[7]["history_id"] == 2
    assert runs[8] == {"computer_id": 8, "launched": False, "launch_message": "offline", "completed": False}
    assert result["summary"].startswith("2 target(s): 1 succeeded, 1 failed")


async def test_script_execute_without_wait_only_launches(monkeypatch):
    rec = Recorder({"POST Batch/ScriptExecute": {"ScriptResults": [{"EntityId": 7}], "ContainsUnsuccessfulResults": False}})
    result = await _call(monkeypatch, rec, "cwautomate_scripts_execute", {"script_id": 300, "computer_ids": [7, 7], "wait": False})
    assert result["waited"] is False
    assert result["computer_ids"] == [7]
    assert rec.calls("Computers/7/ScriptHistory") == []


async def test_scripts_history_newest_first(monkeypatch):
    rec = Recorder()
    await _call(monkeypatch, rec, "cwautomate_scripts_history", {"computer_id": 7, "limit": 10})
    params = rec.calls("Computers/7/ScriptHistory")[0].url.params
    assert (params["orderBy"], params["pageSize"]) == ("HistoryDate desc", "10")
