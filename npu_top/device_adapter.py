"""Composition root for monitor probes and shared SSH authentication.

[Role] Composes remote-dev SSH access, NPU parsing, and explicit inventory sources.
[Invariants] SSH credentials and key installation belong to remote-dev; monitoring never creates a parallel credential store.
[Timeouts & Locks] Delegates bounded authentication and connection checks to SshAccess.
[Side-Effect Policy] Reads inventory and delegates explicit credential setup; never allocates NPUs.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Sequence

from . import npu_smi
from .inventory import HostPoolFile, InventorySource, MachineInventoryFile, merge_sources
from .settings import Settings
from .ssh_access import SshAccess, validate_endpoint


NpuParser = Callable[[str, str], dict[str, Any]]


class DeviceAdapter:
    def __init__(
        self, ssh: SshAccess, *, npu_parser: NpuParser = npu_smi.parse_npu,
        inventory_sources: list[InventorySource] | None = None,
    ) -> None:
        self.ssh = ssh
        self.npu_parser = npu_parser
        self.inventory_sources = list(inventory_sources or [])

    @classmethod
    def from_settings(cls, settings: Settings) -> "DeviceAdapter":
        sources: list[InventorySource] = [MachineInventoryFile(path) for path in settings.inventory_files]
        sources.extend(HostPoolFile(path) for path in settings.host_pool_files)
        return cls(SshAccess(settings.state_dir, settings.project_root), inventory_sources=sources)

    validate_endpoint = staticmethod(validate_endpoint)

    @property
    def project_root(self) -> Path:
        return self.ssh.working_dir

    def launch(self, server: dict[str, Any], command: Sequence[str], *, timeout_ms: int):
        return self.ssh.launch(server, command, timeout_ms=timeout_ms)

    def preflight(self, server: dict[str, Any]) -> dict[str, Any]:
        return self.ssh.preflight(server)

    def connect(self, server: dict[str, Any], *, password: str | None = None) -> dict[str, Any]:
        return self.ssh.connect(server, password=password)

    def discover_servers(self) -> list[dict[str, Any]]:
        return merge_sources(self.inventory_sources)

    def parse_npu(self, info: str, usages: str) -> dict[str, Any]:
        return self.npu_parser(info, usages)
