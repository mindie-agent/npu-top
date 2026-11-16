# Acceptance

## Local console

Accept a start or restart only when:

- `npu-top serve` is the process under test (HTTP API and static frontend, one process);
- `http://127.0.0.1:8788/api/health` returns `status=ok` and `contract=observation-only` without using an HTTP proxy;
- `GET /` returns the packed dashboard (`200`), not a missing-static error;
- `/api/agent/servers` returns compact JSON with `source=cache` and an `observation` envelope whose `allocation_authority` is `false`;
- with no browser lease, health reports `mode=idle` and the configured idle interval;
- the listener remains bound to loopback.

The fleet can temporarily contain `pending` entries while its first collection finishes. Preserve ignored history and key data during rebuilds.

## Agent interface

For CLI/MCP changes, validate:

- backend tests against the installed package, frontend lint, and production build;
- one cached CLI query, which must not trigger collection;
- one bounded live query that returns a newly assigned scheduler snapshot;
- every agent payload still carries the `observation` envelope and every MCP tool description still states the observation-only contract (`tests/test_agent_view.py`, `tests/test_agent_cli_mcp.py`);
- one stdio MCP initialize, `tools/list`, and `tools/call` exchange;
- compact process/container/owner output on an occupied host;
- compact mount output and complete structured mount data.

For service failures, inspect health and recent process logs without printing passwords or files under the state directory keys folder.
