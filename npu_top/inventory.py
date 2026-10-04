"""Optional, explicitly configured sources of hosts to monitor.

None of these sources are discovered by walking the filesystem or by asking
Git where the monitor is checked out. The operator (or the tool that deploys
the monitor) passes concrete file paths and, optionally, one external command
that can install the monitor's public key using a one-time password.

The inventory describes *which hosts exist*. It says nothing about which
devices may be used; that decision belongs to whatever coordinates the fleet.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

from .ssh_access import KeyInstallResult, validate_endpoint


LOW_PRIORITY_TAG = "低优先级"


class InventoryError(ValueError):
    """An explicitly configured source could not be read completely."""


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InventoryError(f"inventory source unavailable: {path} ({type(exc).__name__})") from exc


class InventorySource(Protocol):
    def servers(self) -> list[dict[str, Any]]:
        """Return server records: name, host, port, username, tags, workspace_enabled, inventory_path."""


class MachineInventoryFile:
    """A JSON file of the form ``{"machines": [{"alias", "host": {...}}]}``.

    Only ``host.ip``/``host.host``, ``host.port``, ``host.user`` and the
    machine type are read. Container endpoints, credentials and any other
    fields are ignored. Hosts listed here are treated as actively managed.
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


class ExternalKeyBootstrap:
    """Run an operator-supplied command that installs the monitor public key.

    ``template`` is a shell-quoted command line whose ``{host}``, ``{port}``,
    ``{user}`` and ``{public_key_file}`` placeholders are substituted per
    argument. Exit 77 is reserved for authentication rejection before any remote
    write; only this outcome permits trying another password. Other nonzero
    exits and interrupted calls have uncertain effects. The one-time password is written to the command's stdin followed
    by a newline; it is never placed in arguments or logs. Exit status 0 means
    the key was installed. ``{python}`` expands to the running interpreter.
    """

    PLACEHOLDERS = ("host", "port", "user", "public_key_file", "python")

    def __init__(self, template: str, *, timeout: int | None = None) -> None:
        self.argv_template = shlex.split(template)
        if not self.argv_template:
            raise ValueError("bootstrap command must not be empty")
        self.timeout = timeout

    def render(self, server: dict[str, Any], public_key_file: Path) -> list[str]:
        values = {
            "host": str(server["host"]), "port": str(server["port"]), "user": str(server["username"]),
            "public_key_file": str(public_key_file), "python": sys.executable,
        }
        rendered = []
        for argument in self.argv_template:
            try:
                rendered.append(argument.format(**values))
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(f"unsupported placeholder in bootstrap command: {argument}") from exc
        return rendered

    def run(self, server: dict[str, Any], public_key_file: Path, password: str) -> KeyInstallResult:
        command = self.render(server, public_key_file)
        try:
            result = subprocess.run(
                command, input=password + "\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return KeyInstallResult("unknown", "密钥引导超过调用者时限；远端写入结果不确定，请先核对")
        except OSError as exc:
            return KeyInstallResult("not_started", f"无法执行密钥引导命令: {type(exc).__name__}")
        if result.returncode == 0:
            return KeyInstallResult("completed")
        if result.returncode == 77:
            return KeyInstallResult("not_started", "密码认证被拒绝", authentication_rejected=True)
        error = _safe_command_error(result.stdout, result.stderr)
        if password:
            error = error.replace(password, "[REDACTED]")
        return KeyInstallResult("unknown", error)


def _safe_command_error(stdout: str, stderr: str) -> str:
    try:
        payload = json.loads(stdout)
        if isinstance(payload, dict):
            return str(payload.get("message") or payload.get("error") or "密钥引导失败")[-1000:]
    except json.JSONDecodeError:
        pass
    lines = [line for line in stderr.splitlines() if not line.startswith("__MINDIE_PROGRESS__=")]
    return ("\n".join(lines) or "密钥引导失败")[-1000:]
