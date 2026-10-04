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
from .ssh_access import KeyInstallResult, SshAccess, validate_endpoint


NpuParser = Callable[[str, str], dict[str, Any]]


class DeviceAdapter:
    def __init__(
        self,
        ssh: SshAccess,
        *,
        npu_parser: NpuParser = npu_smi.parse_npu,
        inventory_sources: list[InventorySource] | None = None,
        key_bootstrap: ExternalKeyBootstrap | None = None,
        bootstrap_state=None,
    ) -> None:
        self.ssh = ssh
        self.npu_parser = npu_parser
        self.inventory_sources = list(inventory_sources or [])
        self.key_bootstrap = key_bootstrap
        self.bootstrap_state = bootstrap_state

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
        key_auth_ok, key_auth_error = self.ssh.check_key_auth(server)
        if key_auth_ok:
            result = {"ok": True, "method": "existing-key", "attempts": 0}
            if self.bootstrap_state is not None:
                receipt = self.bootstrap_state.bootstrap_receipt(server["id"])
                if receipt and receipt["state"] != "verified":
                    try:
                        receipt = self.bootstrap_state.finish_bootstrap(server["id"], receipt["operation_id"], "verified")
                    except Exception as exc:
                        return {**result, "ok": False, "state": "recording_failed", "key_auth_verified": True,
                                "error": "密钥登录已验证，但原安装回执未能更新", "receipt": receipt,
                                "recording_error_type": type(exc).__name__}
                if receipt:
                    result["receipt"] = receipt
            return result
        if key_auth_error:
            return {"ok": False, "method": None, "attempts": 0, "error": key_auth_error}
        installation = self._recorded_installation(server, "default-identity", 0,
                                                   lambda: self.ssh.install_key_with_default_identity(server))
        if not installation.authentication_rejected or installation.recording_error:
            return self._verify_installation(server, installation, "default-identity", 0)
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
            installation = self._recorded_installation(server, "external-bootstrap", index,
                                                       lambda: self.key_bootstrap.run(server, self.ssh.public_key, password))
            if not installation.authentication_rejected or installation.recording_error:
                return self._verify_installation(server, installation, "external-bootstrap", index)
            error = installation.error
        return {"ok": False, "method": None, "attempts": len(passwords), "error": error}

    def _recorded_installation(self, server, method, attempts, install):
        if self.bootstrap_state is None:
            return KeyInstallResult("not_started", "持久安装回执不可用；尚未发送远端密钥写入")
        claimed, receipt = self.bootstrap_state.reserve_bootstrap(server["id"], method, attempts)
        if not claimed:
            state = "unknown" if receipt["state"] == "sending" else receipt["state"]
            return KeyInstallResult(state, "原密钥安装已发送或完成，登录尚未确认；请先核对原结果，未重复写入", receipt=receipt)
        try:
            result = install()
        except Exception as exc:
            result = KeyInstallResult("unknown", f"密钥安装结果不确定：{type(exc).__name__}；未重复写入")
        try:
            receipt = self.bootstrap_state.finish_bootstrap(server["id"], receipt["operation_id"], result.state)
        except Exception as exc:
            return KeyInstallResult(result.state, result.error, result.authentication_rejected, receipt, type(exc).__name__)
        return KeyInstallResult(result.state, result.error, result.authentication_rejected, receipt)

    def _verify_installation(self, server, installation, method, attempts) -> dict[str, Any]:
        receipt = installation.receipt
        result = {"ok": False, "method": receipt["method"] if receipt else method,
                  "attempts": receipt["attempts"] if receipt else attempts, "installation": installation.state}
        if receipt:
            result["receipt"] = receipt
        if installation.recording_error:
            return {**result, "state": "recording_failed", "recording_error_type": installation.recording_error,
                    "error": f"密钥安装结果为 {installation.state}，但回执保存失败；未重复写入"}
        if installation.state != "completed":
            return {**result, "state": installation.state, "error": installation.error}
        try:
            ok, error = self.ssh.check_key_auth(server)
        except Exception as exc:
            return {**result, "state": "verification_failed", "error": f"密钥已安装，验证失败：{type(exc).__name__}"}
        if ok and receipt:
            try:
                result["receipt"] = self.bootstrap_state.finish_bootstrap(server["id"], receipt["operation_id"], "verified")
            except Exception as exc:
                return {**result, "state": "recording_failed", "key_auth_verified": True,
                        "recording_error_type": type(exc).__name__, "error": "密钥已安装且登录已验证，但回执保存失败"}
        return {**result, "ok": ok, "state": "verified" if ok else "verification_failed",
                **({} if ok else {"error": error or "密钥已安装，但监控密钥登录未通过验证"})}

    # ---- Inventory ------------------------------------------------------
    def discover_servers(self) -> list[dict[str, Any]]:
        return merge_sources(self.inventory_sources)

    # ---- Parsing --------------------------------------------------------
    def parse_npu(self, info: str, usages: str) -> dict[str, Any]:
        return self.npu_parser(info, usages)
