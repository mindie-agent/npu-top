"""OpenSSH access for host probes: dedicated key, known_hosts, base command.

[Role] Manages the monitor-owned Ed25519 keypair, isolated `known_hosts`, and OpenSSH command construction.
[Invariants] Distinguishes `completed`, `not_started` (`authentication_rejected`), and `unknown` key-installation outcomes so uncertain remote writes are never replayed.
[Timeouts & Locks] `ssh-keygen` bounded to `15s`, `ssh -G` preflight to `8s`, key auth check to `12s`, key install to `15s` (`ConnectTimeout=8`).
[Side-Effect Policy] Local key generation under `state_dir/keys` and explicit `~/.ssh/authorized_keys` append during bootstrap.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
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


@dataclass(frozen=True)
class KeyInstallResult:
    state: str
    error: str | None = None
    authentication_rejected: bool = False
    receipt: dict[str, Any] | None = None
    recording_error: str | None = None


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
        if self.private_key.exists() and not self.public_key.exists():
            derived = subprocess.run(
                ["ssh-keygen", "-y", "-f", str(self.private_key)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            if derived.returncode == 0 and derived.stdout.strip():
                self.public_key.write_text(derived.stdout, encoding="utf-8")
                self._secure_key_permissions()
                return self.private_key
            self.private_key.unlink(missing_ok=True)
        if self.public_key.exists() or self.public_key.is_symlink():
            self.public_key.unlink(missing_ok=True)
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
            control_path = Path("ssh-control") / "%C"
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

    def check_key_auth(self, server: dict[str, Any]) -> tuple[bool, str | None]:
        try:
            result = subprocess.run(
                [*self.ssh_base(server), "true"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                timeout=12, check=False, cwd=self.working_dir,
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            return False, "SSH 连接超时"
        except OSError as exc:
            return False, f"本地 SSH 启动失败：{exc.strerror or type(exc).__name__}"
        if result.returncode == 0:
            return True, None
        error = " ".join(result.stderr.split())[-500:]
        if "Permission denied" not in error or "unix_listener:" in error:
            return False, f"SSH 连接失败：{error or f'exit {result.returncode}'}"
        return False, None

    def _default_identity_base(self, server: dict[str, Any]) -> list[str]:
        """ssh command that uses the invoking user's own default identities."""
        validate_endpoint(server["host"], int(server["port"]), server["username"])
        return [
            "ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
            "-p", str(server["port"]), f"{server['username']}@{server['host']}",
        ]

    def _authorized_keys_remote_command(self) -> str:
        if not self.public_key.exists():
            self.ensure_key()
        public_key = self.public_key.read_text(encoding="utf-8").strip()
        quoted = shlex.quote(public_key)
        return (
            "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            "chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && "
            f"(grep -qxF {quoted} ~/.ssh/authorized_keys; code=$?; "
            f"case $code in 0) ;; 1) printf '%s\\n' {quoted} >> ~/.ssh/authorized_keys ;; *) exit $code ;; esac)"
        )

    def install_key_with_default_identity(self, server: dict[str, Any]) -> KeyInstallResult:
        """Preserve the write outcome separately from later key verification."""
        try:
            check = subprocess.run(
                [*self._default_identity_base(server), "true"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=12, check=False,
                cwd=self.working_dir,
            )
        except (subprocess.TimeoutExpired, TimeoutError, OSError) as exc:
            return KeyInstallResult("not_started", f"默认身份连接检查失败：{type(exc).__name__}")
        if check.returncode != 0:
            error = " ".join(check.stderr.split())[-500:]
            rejected = "Permission denied" in error
            return KeyInstallResult("not_started", error or f"SSH exit {check.returncode}", rejected)
        remote = self._authorized_keys_remote_command()
        try:
            install = subprocess.run(
                [*self._default_identity_base(server), "sh", "-c", shlex.quote(remote)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15, check=False,
                cwd=self.working_dir,
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            return KeyInstallResult("unknown", "密钥安装超时；远端授权文件写入结果不确定，请先核对")
        except OSError as exc:
            return KeyInstallResult("not_started", f"无法启动密钥安装：{type(exc).__name__}")
        if install.returncode == 0:
            return KeyInstallResult("completed")
        return KeyInstallResult("unknown", "密钥安装未获完整成功确认；请先核对远端授权文件")

    def install_key_with_password(self, server: dict[str, Any], password: str) -> KeyInstallResult:
        """Install the monitor public key using one-time in-memory SSH_ASKPASS auth."""
        validate_endpoint(server["host"], int(server["port"]), server["username"])
        remote = self._authorized_keys_remote_command()
        command = [
            "ssh", "-T",
            "-o", "BatchMode=no",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=8",
            "-o", "ConnectionAttempts=1",
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-p", str(server["port"]),
            f"{server['username']}@{server['host']}",
            "sh", "-c", shlex.quote(remote),
        ]
        with tempfile.TemporaryDirectory(prefix="mindie-top-askpass-") as temp_raw:
            temp_dir = Path(temp_raw)
            if self.is_windows:
                helper_py = temp_dir / "askpass.py"
                helper_py.write_text(
                    "import os, sys\n"
                    "sys.stdout.write(os.environ.get('MINDIE_TOP_SSH_PASSWORD', '') + '\\n')\n",
                    encoding="utf-8",
                )
                helper = temp_dir / "askpass.cmd"
                helper.write_text(
                    f'@echo off\r\nsetlocal\r\n"{sys.executable}" "{helper_py}"\r\n',
                    encoding="utf-8",
                )
            else:
                helper = temp_dir / "askpass.sh"
                helper.write_text(
                    "#!/bin/sh\nprintf '%s\\n' \"${MINDIE_TOP_SSH_PASSWORD}\"\n",
                    encoding="utf-8",
                )
                helper.chmod(0o700)
            env = {
                **os.environ,
                "MINDIE_TOP_SSH_PASSWORD": password,
                "SSH_ASKPASS": str(helper),
                "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": os.environ.get("DISPLAY") or "mindie-top:0",
            }
            try:
                install = subprocess.run(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, text=True, timeout=15, check=False,
                    cwd=self.working_dir, env=env,
                )
            except subprocess.TimeoutExpired:
                return KeyInstallResult("unknown", "密钥安装超时；远端授权文件写入结果不确定，请先核对")
            except OSError as exc:
                return KeyInstallResult("not_started", f"无法启动密钥安装：{type(exc).__name__}")
        if install.returncode == 0:
            return KeyInstallResult("completed")
        error = " ".join((install.stderr or "").split())
        if password:
            error = error.replace(password, "[REDACTED]")
        error = error[-500:]
        if install.returncode == 255 and "Permission denied" in error:
            return KeyInstallResult("not_started", error or "密码认证被拒绝", authentication_rejected=True)
        return KeyInstallResult("unknown", error or "密钥安装未获完整成功确认；请先核对远端授权文件")
