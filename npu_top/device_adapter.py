"""Composition root for host access: SSH identity, NPU parsing, inventories.

``DeviceAdapter`` replaces the former ``WorkspaceDeviceAdapter``, which located
a sibling project by walking parent directories and Git common directories and
then imported scripts from it. Everything the adapter needs is now injected:

* ``SshAccess`` owns the monitor key and OpenSSH options;
* ``npu_parser`` interprets ``npu-smi`` output (defaults to the bundled parser);
* ``inventory_sources`` are explicit files that list hosts to monitor;
* ``key_bootstrap`` is an optional external command for one-time passwords.

The adapter observes hosts. It never decides which devices may be used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import npu_smi
from .inventory import (
    ExternalKeyBootstrap, HostPoolFile, InventorySource, MachineInventoryFile, merge_sources,
)
from .settings import Settings
from .ssh_access import SshAccess, validate_endpoint


NpuParser = Callable[[str, str], dict[str, Any]]


class DeviceAdapter:
    def __init__(
        self,
        ssh: SshAccess,
        *,
        npu_parser: NpuParser = npu_smi.parse_npu,
        inventory_sources: list[InventorySource] | None = None,
        key_bootstrap: ExternalKeyBootstrap | None = None,
    ) -> None:
        self.ssh = ssh
        self.npu_parser = npu_parser
        self.inventory_sources = list(inventory_sources or [])
        self.key_bootstrap = key_bootstrap

    @classmethod
    def from_settings(cls, settings: Settings) -> "DeviceAdapter":
        sources: list[InventorySource] = [MachineInventoryFile(path) for path in settings.inventory_files]
        sources.extend(HostPoolFile(path) for path in settings.host_pool_files)
        bootstrap = ExternalKeyBootstrap(settings.bootstrap_command) if settings.bootstrap_command else None
        return cls(
            SshAccess(settings.state_dir, settings.project_root),
            inventory_sources=sources, key_bootstrap=bootstrap,
        )

    # ---- SSH delegation -------------------------------------------------
    validate_endpoint = staticmethod(validate_endpoint)

    @property
    def project_root(self) -> Path:
        return self.ssh.working_dir

    @property
    def private_key(self) -> Path:
        return self.ssh.private_key

    @property
    def public_key(self) -> Path:
        return self.ssh.public_key

    def ensure_key(self) -> Path:
        return self.ssh.ensure_key()

    def ssh_base(self, server: dict[str, Any], *, batch_mode: bool = True) -> list[str]:
        return self.ssh.ssh_base(server, batch_mode=batch_mode)

    def preflight(self, server: dict[str, Any]) -> dict[str, Any]:
        return self.ssh.preflight(server)

    def key_auth_works(self, server: dict[str, Any]) -> bool:
        return self.ssh.key_auth_works(server)

    # ---- Credentials ----------------------------------------------------
    def bootstrap_with_passwords(self, server: dict[str, Any], passwords: list[str]) -> dict[str, Any]:
        preflight = self.ssh.preflight(server)
        if not preflight["ok"]:
            return {"ok": False, "method": None, "attempts": 0, "error": preflight["error"]}
        if self.ssh.key_auth_works(server):
            return {"ok": True, "method": "existing-key", "attempts": 0}
        if self.ssh.install_key_with_default_identity(server):
            return {"ok": True, "method": "default-identity", "attempts": 0}
        if not passwords:
            return {"ok": False, "method": None, "attempts": 0, "error": "密钥登录失败，且未提供一次性密码"}
        if self.key_bootstrap is None:
            return {
                "ok": False, "method": None, "attempts": 0,
                "error": "未配置 NFM_BOOTSTRAP_COMMAND，无法使用一次性密码安装监控公钥",
            }

        error = "密码候选均未通过认证"
        for index, password in enumerate(passwords, start=1):
            if not isinstance(password, str) or not password:
                continue
            ok, error = self.key_bootstrap.run(server, self.ssh.public_key, password)
            if ok and self.ssh.key_auth_works(server):
                return {"ok": True, "method": "external-bootstrap", "attempts": index}
            error = error or "密钥引导命令成功，但监控密钥仍无法登录"
        return {"ok": False, "method": None, "attempts": len(passwords), "error": error}

    # ---- Inventory ------------------------------------------------------
    def discover_servers(self) -> list[dict[str, Any]]:
        return merge_sources(self.inventory_sources)

    # ---- Parsing --------------------------------------------------------
    def parse_npu(self, info: str, usages: str) -> dict[str, Any]:
        return self.npu_parser(info, usages)
