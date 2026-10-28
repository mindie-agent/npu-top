"""Shared remote-dev authentication for read-only monitor probes.

[Role] Maps monitor records to remote-dev endpoints and its shared SSH launcher.
[Invariants] Credential references resolve in the local user's shared store; probes never install keys or create a second password store.
[Timeouts & Locks] SSH config validation is bounded to 8s, access checks to 12s, and explicit credential setup to 30s.
[Side-Effect Policy] Probes are read-only; explicit password onboarding delegates key installation and credential persistence to remote-dev.
"""
from __future__ import annotations

from dataclasses import replace
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from remote_dev import auth
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.core.ssh_transport import RemoteCompleted, capture_launch, ssh_launch


HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]+$")
USER_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")
KEY_INSTALL_STARTED = "__NPU_TOP_KEY_INSTALL_STARTED__"


def capture_ssh(access, server, command, *, timeout_ms, input=None, config_only=False, fresh=False) -> RemoteCompleted:
    """Keep plan preparation and its owned SSH process inside one probe budget."""
    started = time.monotonic()
    plan = access.launch(server, command, timeout_ms=timeout_ms, **({"fresh": True} if fresh else {}))
    if config_only:
        plan.argv.insert(1, "-G")
    remaining_ms = int(timeout_ms - (time.monotonic() - started) * 1000)
    if remaining_ms <= 0:
        raise RemoteExecutionError(
            "SSH plan preparation exhausted the probe deadline",
            category="authentication_timeout", submission_state="not_sent",
        )
    result = capture_launch(plan, input=input, timeout_ms=remaining_ms)
    if result.timed_out:
        error = subprocess.TimeoutExpired(result.args, timeout_ms / 1000,
                                          output=result.stdout, stderr=result.stderr)
        error.cleanup_error = result.cleanup_error
        raise error
    completed = RemoteCompleted(
        result.returncode, result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
        cancelled=result.cancelled, cleanup_error=result.cleanup_error,
    )
    if result.cancelled:
        error = RemoteExecutionError("SSH observation cancelled", category="cancelled",
                                     submission_state="acknowledged" if result.returncode is not None else "uncertain")
        error.completed = completed
        error.cleanup_error = result.cleanup_error
        raise error
    return completed


def validate_endpoint(host: str, port: int, username: str) -> None:
    if not host or host.startswith("-") or not HOST_PATTERN.fullmatch(host):
        raise ValueError("主机地址只允许域名、IPv4 或 IPv6 字符")
    if not USER_PATTERN.fullmatch(username):
        raise ValueError("SSH 用户名包含不支持的字符")
    if port < 1 or port > 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")


class SshAccess:
    def __init__(self, state_dir: Path, working_dir: Path) -> None:
        self.state_dir = state_dir
        self.working_dir = working_dir

    validate_endpoint = staticmethod(validate_endpoint)

    def endpoint(self, server: dict[str, Any]) -> Endpoint:
        validate_endpoint(server["host"], int(server["port"]), server["username"])
        endpoint = Endpoint(
            host=server["host"], port=int(server["port"]), user=server["username"],
            auth_ref=server.get("auth_ref"),
        )
        if auth.ProfileStore().resolve(endpoint) is not None:
            return endpoint
        identity = None
        # Existing monitor keys remain usable; new keys belong to remote-dev.
        existing_key = self.state_dir / "keys" / "id_ed25519"
        if existing_key.is_file():
            identity = str(existing_key)
        known_hosts = self.state_dir / "known_hosts"
        return Endpoint(
            host=server["host"], port=int(server["port"]), user=server["username"],
            identity_file=identity, auth_ref=server.get("auth_ref"),
            known_hosts_file=str(known_hosts) if known_hosts.is_file() else None,
        )

    def launch(
        self, server: dict[str, Any], command: Sequence[str] | None = None,
        *, timeout_ms: int | None = None, fresh: bool = False,
    ):
        endpoint = self.endpoint(server)
        if fresh:
            endpoint = replace(endpoint, ssh_mux=False)
        return ssh_launch(
            endpoint, command, timeout_ms=timeout_ms, repair=False,
        )

    def preflight(self, server: dict[str, Any]) -> dict[str, Any]:
        try:
            result = capture_ssh(self, server, None, timeout_ms=8000, config_only=True)
        except RemoteExecutionError as exc:
            return {"ok": False, "category": exc.category, "error": str(exc)}
        return {
            "ok": result.returncode == 0,
            "category": "ssh_config_valid" if result.returncode == 0 else "ssh_config_invalid",
            "error": None if result.returncode == 0 else (result.stderr or "SSH configuration rejected")[-1000:],
            **({"cleanup_error": result.cleanup_error} if result.cleanup_error else {}),
        }

    def connect(self, server: dict[str, Any], *, password: str | None = None) -> dict[str, Any]:
        """Import a supplied password once, or verify the existing shared identity."""
        try:
            if password is not None:
                configured = auth.setup_access(self.endpoint(server), password=password, timeout_ms=30000)
                return {**configured, "ok": True}
            # Explicit onboarding must verify current credentials, not a cached login.
            result = capture_ssh(self, server, ["true"], timeout_ms=12000, fresh=True)
        except RemoteExecutionError as exc:
            return {
                "ok": False, "state": "unknown" if exc.submission_state == "uncertain" else "not_ready",
                "category": exc.category, "error": str(exc), "submission_state": exc.submission_state,
                **({"bootstrap": exc.bootstrap} if hasattr(exc, "bootstrap") else {}),
                **({"cleanup_error": exc.cleanup_error} if getattr(exc, "cleanup_error", None) else {}),
            }
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            return {"ok": False, "state": "not_ready", "category": "connection_timeout", "error": "SSH 连接超时",
                    **({"cleanup_error": exc.cleanup_error} if getattr(exc, "cleanup_error", None) else {})}
        except OSError as exc:
            return {"ok": False, "state": "not_ready", "category": "local_process", "error": f"SSH 启动失败：{exc.strerror or type(exc).__name__}"}
        return {
            "ok": result.returncode == 0,
            "state": "ready" if result.returncode == 0 else "not_ready",
            "auth_ref": server.get("auth_ref"),
            "error": None if result.returncode == 0 else (result.stderr or f"SSH exit {result.returncode}")[-1000:],
            **({"cleanup_error": result.cleanup_error} if result.cleanup_error else {}),
        }
