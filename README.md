# ConnectWise Automate MCP Server

An [MCP](https://modelcontextprotocol.io) server for the
[ConnectWise Automate](https://www.connectwise.com/platform/rmm/automate) REST API, the RMM
that MSPs use to monitor and manage client endpoints. It lets an AI agent read computers,
clients and their locations, alerts, the script catalog and script history and, on explicit
request, reboot computers, run scripts and catalog commands, and create or update clients.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: credentials arrive on every request as HTTP headers and are
  never stored; the API token is requested per request and discarded with it
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Cwa-Server` | Yes | string | none | https URL | Automate server URL (a bare host name gets `https://`; a trailing `/cwa/api/v1` or `/automate` is removed) | `https://your-server.hostedrmm.com` |
| `X-Cwa-Client-Id` | Yes | string | none | - | Integration Client ID issued by ConnectWise for the Automate REST API | `your-client-id` |
| `X-Cwa-Username` | Yes | string | none | - | Integrator user name (or a regular Automate user name with `user` auth) | `your-username` |
| `X-Cwa-Password` | Yes | string | none | - | Password of that user | `your-password` |
| `X-Cwa-2fa` | No | string | none | - | Current two-factor passcode, for `user` auth on an account with 2FA | `123456` |
| `X-Cwa-Auth-Method` | No | string | `integrator` (`user` when `X-Cwa-2fa` is sent) | `integrator`, `user` | How to sign in | `integrator` |

Where to get them:

- **Client ID:** the API client id ConnectWise issues to integrations through its developer
  network (developer.connectwise.com). It identifies the integration, not the MSP.
- **Integrator user:** create an **Integrator Login** in the Automate system dashboard's
  integration settings (the exact menu differs between Automate versions; see the ConnectWise
  documentation for yours), and grant the permissions the agent needs (computers, clients,
  scripts, commands). Integrator logins have no 2FA, which makes them the right choice for a
  server-to-server connection.
- **User auth** (`X-Cwa-Auth-Method: user`) signs in as a normal Automate user. With 2FA on, a
  passcode is only valid for a short time, so this mode suits interactive testing rather than
  a stored connection.
- The API user's permissions apply: command levels and group "Send Commands" rights decide
  which commands and scripts may run, and client access decides which records are visible.

How credentials are sent to Automate: the server calls
`POST {X-Cwa-Server}/cwa/api/v1/apitoken` with the `ClientId` header and the user name and
password, then sends every API call with `Authorization: Bearer <token>` and the `ClientId`
header. A 401 gets a fresh token once. Values that look like unresolved templates (`${...}`)
are treated as missing.

A request to `/mcp` without the four required headers gets HTTP 401 naming the missing ones;
a non-https server URL or an unknown auth method does not block `tools/list`; tool calls then
return an `invalid_argument` error and the server URL is never called.

## Tools

All tools return a JSON string. List tools take `limit` (default 50, max 200) and `skip` (a multiple of
`limit`; Automate pages by page number) and return the records plus
`{count, skip, has_more, next_skip}`.

| Tool | Purpose | Parameters (required in **bold**) |
|---|---|---|
| `cwautomate_navigate` | Describe the tools of one Automate area (changes nothing) | **domain** (`computers`, `clients`, `alerts`, `scripts`) |
| `cwautomate_status` | Check the Automate connection (server, auth method, login works) and list the tool areas | - |
| `cwautomate_computers_list` | List computers (agents), optionally for one client or location, or only online / offline ones | client_id, location_id, status (`online`, `offline`, `all`), limit, skip |
| `cwautomate_computers_get` | Get one computer's details: OS, status, last contact, client, location, hardware and agent info | **computer_id** |
| `cwautomate_computers_search` | Search computers by part of their name, optionally within one client | **query**, client_id, limit |
| `cwautomate_computers_reboot` | Reboot a computer through Automate's command catalog and wait for the result (**write**) | **computer_id**, command_id |
| `cwautomate_computers_run_script` | Run a script on one computer and wait for its outcome (**write**) | **computer_id**, **script_id**, parameters |
| `cwautomate_commands_list` | List the command catalog (id, name, level) used by `cwautomate_computers_run_command` | - |
| `cwautomate_computers_run_command` | Send a catalog command to a computer and wait for its output (**write**) | **computer_id**, **command_id**, parameters, timeout_seconds |
| `cwautomate_clients_list` | List clients (customers) with their ids, names and contact details | limit, skip |
| `cwautomate_clients_get` | Get one client by id, optionally with its locations | **client_id**, include_locations |
| `cwautomate_clients_create` | Create a client (**write**) | **name**, city, state, zip, country, phone |
| `cwautomate_clients_update` | Change a client's name or contact details; only the fields given change (**write**) | **client_id**, name, city, state, zip, country, phone |
| `cwautomate_alerts_list` | List alerts, optionally for one computer, client or severity | computer_id, client_id, severity, limit, skip |
| `cwautomate_alerts_get` | Get one alert by id: message, severity, computer, client and time | **alert_id** |
| `cwautomate_scripts_list` | List scripts in the Automate script catalog, optionally by folder or name | folder_id, search, limit, skip |
| `cwautomate_scripts_get` | Get a script's catalog entry: name, folder, comments, kind flags and accepted parameters | **script_id** |
| `cwautomate_scripts_execute` | Run a script on one or more computers and wait for each outcome (**write**) | **script_id**, **computer_ids**, parameters, priority (`low`, `normal`, `high`), wait, timeout_seconds, skip_offline, wake_offline |
| `cwautomate_scripts_history` | Get finished script runs on a computer, newest first, with pass/fail state and diagnostic message | **computer_id**, limit |
| `cwautomate_scripts_running` | List scripts currently running on a computer | **computer_id** |

Errors come back as a JSON envelope instead of an exception:

```json
{"error": {"code": "unauthorized", "message": "Automate rejected the username / password or client id (token request failed)", "retryable": false}}
```

`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`,
`rate_limited`, `upstream_error`. Reads are retried with backoff on 429, 5xx and network
errors; writes are retried only on 429. When a write loses its connection before Automate
answers, the result is `{"completed": false, "interrupted": true, ...}` instead of an error,
because the action may already have run.

## Run locally

```bash
uv sync
uv run python -m connectwise_automate_mcp          # listens on 0.0.0.0:8080
```

or with Docker:

```bash
docker build --platform linux/amd64 -t connectwise-automate-mcp:dev .
docker run --rm -p 8080:8080 connectwise-automate-mcp:dev
```

Configuration (environment variables, all optional): `MCP_HTTP_HOST` (default `0.0.0.0`),
`MCP_HTTP_PORT` (default `8080`), `LOG_LEVEL` (default `INFO`). Credentials are never read
from the environment.

## Testing example

```bash
# list tools
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Cwa-Server: https://your-server.hostedrmm.com" \
  -H "X-Cwa-Client-Id: your-client-id" \
  -H "X-Cwa-Username: your-username" \
  -H "X-Cwa-Password: your-password" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# offline computers of one client
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Cwa-Server: https://your-server.hostedrmm.com" \
  -H "X-Cwa-Client-Id: your-client-id" \
  -H "X-Cwa-Username: your-username" \
  -H "X-Cwa-Password: your-password" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"cwautomate_computers_list","arguments":{"client_id":12,"status":"offline"}}}'
```

Unit tests (no network access needed):

```bash
uv run pytest
```

## Delivery notes

- **Tool count (20).** Kept as is because the tool names are an existing contract; every tool
  is listed from the first `tools/list`.
- **Endpoint actions.** `cwautomate_computers_reboot`, `cwautomate_computers_run_script`,
  `cwautomate_computers_run_command` and `cwautomate_scripts_execute` act on live machines and
  are annotated destructive; `cwautomate_clients_create` / `_update` change records. Their
  descriptions tell the agent to use them only on explicit request. There are no delete tools.
- **Waiting for results.** Automate has no synchronous run. Commands are polled through their
  execution row and scripts through new entries in the computer's script history, every 3 s
  for up to 90 s by default (`timeout_seconds`, at most 100 s so the call finishes inside the
  gateway's limit). An unfinished run returns `completed: false` with ids to check later.
- **No interactive prompts.** Older versions asked the user for a client filter before an
  unfiltered list, which a stateless gateway cannot relay; lists now page instead.
- **Result size exception (SOP 2.5).** `cwautomate_computers_search` and
  `cwautomate_scripts_history` have no paging (no `skip`), so they keep a maximum of 1000; results
  are still cut to 20,000 characters with a truncation notice. The paged lists take at most 200
  per call.
- **Alerts** are read-only in the Automate API (no acknowledge / close).

## License

Apache License 2.0 - see [LICENSE](LICENSE) and [NOTICE](NOTICE).
