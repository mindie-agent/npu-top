"""OpenSSH access for host probes: dedicated key, known_hosts, base command.

This module knows nothing about where the monitor is deployed or which
project manages the hosts. It only owns the monitor's private Ed25519 key,
its isolated ``known_hosts`` file, and the OpenSSH options used for probes.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any


HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]+$")
USER_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")


def validate_endpoint(host: str, port: int, username: str) -> None:
    if not host or host.startswith("-") or not HOST_PATTERN.fullmatch(host):
        raise ValueError("主机地址只允许域名、IPv4 或 IPv6 字符")
    if not USER_PATTERN.fullmatch(username):
        raise ValueError("SSH 用户名包含不支持的字符")
    if port < 1 or port > 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")


class SshAccess:
    """Monitor-owned SSH identity and command construction."""

    def __init__(self, state_dir: Path, working_dir: Path, *, is_windows: bool | None = None) -> None:
        self.state_dir = state_dir
        # ssh runs with this cwd so the relative ControlPath stays short enough
        # for the Unix-domain socket path limit even in deeply nested checkouts.
        self.working_dir = working_dir
        self.is_windows = os.name == "nt" if is_windows is None else is_windows
        self._key_permissions_ready = False

    validate_endpoint = staticmethod(validate_endpoint)

    @property
    def private_key(self) -> Path:
        return self.state_dir / "keys" / "id_ed25519"

    @property
    def public_key(self) -> Path:
        return self.private_key.with_suffix(".pub")

    @property
    def known_hosts(self) -> Path:
        return self.state_dir / "known_hosts"

    def ensure_key(self) -> Path:
        if self.private_key.exists() and self.public_key.exists():
            self._secure_key_permissions()
            return self.private_key
        self.private_key.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "npu-fleet-monitor", "-f", str(self.private_key)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "ssh-keygen failed")[-1000:])
        self._secure_key_permissions()
        return self.private_key

    def _secure_key_permissions(self) -> None:
        if self._key_permissions_ready:
            return
        self.private_key.chmod(0o600)
        if self.public_key.exists():
            self.public_key.chmod(0o600)
        if self.is_windows:
            identity = subprocess.run(
                ["whoami"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=5, check=False,
            )
            user = identity.stdout.strip()
            if identity.returncode != 0 or not user:
                raise RuntimeError("无法确定当前 Windows 用户，不能收紧监控私钥 ACL")
            acl = subprocess.run(
                ["icacls", str(self.private_key), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, check=False,
            )
            if acl.returncode != 0:
                raise RuntimeError((acl.stderr or acl.stdout or "icacls failed")[-1000:])
        self._key_permissions_ready = True

    def ssh_base(self, server: dict[str, Any], *, batch_mode: bool = True) -> list[str]:
        validate_endpoint(server["host"], int(server["port"]), server["username"])
        self.ensure_key()
        command = [
            "ssh", "-T",
            "-o", f"BatchMode={'yes' if batch_mode else 'no'}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=1",
        ]
        if not self.is_windows:
            control_path = Path("data") / "ssh-control" / "%C"
            command.extend([
                "-o", "ControlMaster=auto",
                "-o", "ControlPersist=90",
                "-o", f"ControlPath={control_path}",
            ])
        command.extend([
            "-i", str(self.private_key),
            "-o", "IdentitiesOnly=yes",
            "-p", str(server["port"]),
            f"{server['username']}@{server['host']}",
        ])
        return command

    def preflight(self, server: dict[str, Any]) -> dict[str, Any]:
        validate_endpoint(server["host"], int(server["port"]), server["username"])
        command = ["ssh", "-G", "-p", str(server["port"]), f"{server['username']}@{server['host']}"]
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=8, check=False,
        )
        return {
            "ok": result.returncode == 0,
            "category": "ssh_config_valid" if result.returncode == 0 else "ssh_config_invalid",
            "error": None if result.returncode == 0 else (result.stderr or "SSH configuration rejected")[-1000:],
        }

    def key_auth_works(self, server: dict[str, Any]) -> bool:
        try:
            result = subprocess.run(
                [*self.ssh_base(server), "true"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                timeout=12, check=False, cwd=self.working_dir,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _default_identity_base(self, server: dict[str, Any]) -> list[str]:
        """ssh command that uses the invoking user's own default identities."""
        validate_endpoint(server["host"], int(server["port"]), server["username"])
        return [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
            "-p", str(server["port"]), f"{server['username']}@{server['host']}",
        ]

    def install_key_with_default_identity(self, server: dict[str, Any]) -> bool:
        """Append the monitor public key using an already trusted identity."""
        try:
            check = subprocess.run(
                [*self._default_identity_base(server), "true"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=False,
                cwd=self.working_dir,
            )
            if check.returncode != 0:
                return False
            public_key = self.public_key.read_text(encoding="utf-8").strip()
            quoted = shlex.quote(public_key)
            remote = (
                "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys; "
                "chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; "
                f"grep -qxF {quoted} ~/.ssh/authorized_keys 2>/dev/null || printf '%s\\n' {quoted} >> ~/.ssh/authorized_keys"
            )
            install = subprocess.run(
                [*self._default_identity_base(server), "sh", "-c", shlex.quote(remote)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15, check=False,
                cwd=self.working_dir,
            )
            return install.returncode == 0 and self.key_auth_works(server)
        except (OSError, subprocess.TimeoutExpired):
            return False
