"""Optional, explicitly configured sources of hosts to monitor.

[Role] Loads host endpoints and shared authentication references from explicit inventory paths.
[Invariants] Never walks parent checkouts, imports partial malformed files, or reads password fields.
[Timeouts & Locks] Local file reads only; no network or subprocess waits.
[Side-Effect Policy] Reads local inventory files without changing credentials or remote hosts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from .ssh_access import validate_endpoint


LOW_PRIORITY_TAG = "低优先级"


class InventoryError(ValueError):
    """An explicitly configured source could not be read completely."""


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except TimeoutError:
        raise
    except (OSError, UnicodeError) as exc:
        raise InventoryError(f"inventory source unavailable: {path} ({type(exc).__name__})") from exc


class InventorySource(Protocol):
    def servers(self) -> list[dict[str, Any]]:
        """Return server records: name, host, port, username, tags, workspace_enabled, inventory_path."""


class MachineInventoryFile:
    """A JSON file of the form ``{"machines": [{"alias", "host": {...}}]}``.

    Host endpoint fields, ``host.auth_ref`` and machine type are read.
    Container endpoints and raw credentials are ignored.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def servers(self) -> list[dict[str, Any]]:
        try:
            document = json.loads(_read_source(self.path))
        except json.JSONDecodeError as exc:
            raise InventoryError(f"invalid inventory JSON: {self.path}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("machines"), list):
            raise InventoryError(f"inventory requires a machines array: {self.path}")
        machines = document["machines"]
        servers: list[dict[str, Any]] = []
        for index, machine in enumerate(machines, 1):
            if not isinstance(machine, dict) or not isinstance(machine.get("host"), dict):
                raise InventoryError(f"invalid inventory host at record {index}: {self.path}")
            host_data = machine["host"]
            host = str(host_data.get("ip") or host_data.get("host") or "").strip()
            try:
                port = int(host_data.get("port") or 22)
            except (TypeError, ValueError) as exc:
                raise InventoryError(f"invalid inventory port at record {index}: {self.path}") from exc
            username = str(host_data.get("user") or "root")
            try:
                validate_endpoint(host, port, username)
            except ValueError as exc:
                raise InventoryError(f"invalid inventory endpoint at record {index}: {self.path}") from exc
            container = machine.get("container") or {}
            if not isinstance(container, dict):
                raise InventoryError(f"invalid inventory container at record {index}: {self.path}")
            machine_type = host_data.get("machine_type") or container.get("machine_type")
            servers.append({
                "name": str(machine.get("alias") or host), "host": host, "port": port,
                "username": username, "tags": [str(machine_type)] if machine_type else [],
                "auth_ref": host_data.get("auth_ref"),
                "inventory_path": str(self.path), "workspace_enabled": True,
            })
        return servers


class HostPoolFile:
    """A plain-text host pool: one host per line, first whitespace field only.

    Additional columns (often credentials) are never read. Hosts that appear
    only here, and not in an active inventory, are inserted with the derived
    low-priority tag so the dashboard can rank them last.
    """

    def __init__(self, path: Path, *, port: int = 22, username: str = "root") -> None:
        self.path = Path(path)
        self.port = port
        self.username = username

    def servers(self) -> list[dict[str, Any]]:
        lines = _read_source(self.path).splitlines()
        servers: list[dict[str, Any]] = []
        for index, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            host = stripped.split(maxsplit=1)[0]
            try:
                validate_endpoint(host, self.port, self.username)
            except ValueError as exc:
                raise InventoryError(f"invalid host pool endpoint at line {index}: {self.path}") from exc
            servers.append({
                "name": host, "host": host, "port": self.port, "username": self.username,
                "tags": [LOW_PRIORITY_TAG], "workspace_enabled": False,
                "inventory_path": str(self.path),
            })
        return servers


def merge_sources(sources: list[InventorySource]) -> list[dict[str, Any]]:
    """Concatenate sources in priority order, keeping the first record per endpoint."""
    seen: set[tuple[str, int, str]] = set()
    merged: list[dict[str, Any]] = []
    for source in sources:
        for server in source.servers():
            endpoint = (server["host"], int(server["port"]), server["username"])
            if endpoint in seen:
                continue
            seen.add(endpoint)
            merged.append(server)
    return merged
