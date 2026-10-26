"""Exercise bootstrap helper processes, remote shell writes and auth boundaries."""
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from npu_top.db import Database
from npu_top.device_adapter import DeviceAdapter
from npu_top.ssh_access import KEY_INSTALL_STARTED, KeyInstallResult, SshAccess


SERVER = {"id": "one", "name": "one", "host": "192.0.2.1", "port": 2222, "username": "root"}
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest monitor's key"


@pytest.fixture
def ssh(tmp_path):
    access = SshAccess(tmp_path, tmp_path)
    access.public_key.parent.mkdir()
    access.public_key.write_text(PUBLIC_KEY + "\n", encoding="utf-8")
    return access


@pytest.mark.skipif(os.name == "nt", reason="POSIX remote authorized_keys shell script")
@pytest.mark.parametrize("existing", ["", "ssh-ed25519 existing-key", "ssh-ed25519 existing-key\n"])
def test_install_preserves_existing_keys_without_final_newline_and_is_idempotent(ssh, tmp_path, existing):
    remote_home = tmp_path / "remote-home"
    authorized = remote_home / ".ssh" / "authorized_keys"
    authorized.parent.mkdir(parents=True)
    authorized.write_text(existing, encoding="utf-8")
    for _ in range(2):
        result = subprocess.run(
            ["sh", "-c", ssh._authorized_keys_remote_command()],
            env={**os.environ, "HOME": str(remote_home)}, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert KEY_INSTALL_STARTED in result.stdout
    lines = authorized.read_text(encoding="utf-8").splitlines()
    assert lines.count(PUBLIC_KEY) == 1
    if existing:
        assert lines[0] == existing.rstrip("\n")
    assert authorized.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Runs the POSIX askpass executable")
@pytest.mark.parametrize("password", ["spaces $ # ' \\\" `literal`", "中文密码-é", "  padded password  "])
def test_askpass_passes_literal_password_without_putting_it_in_files_or_argv(ssh, password):
    real_run = subprocess.run
    helper_path = None

    def run_ssh(argv, **kwargs):
        nonlocal helper_path
        assert password not in " ".join(argv)
        helper_path = Path(kwargs["env"]["SSH_ASKPASS"])
        assert kwargs["env"]["SSH_ASKPASS_REQUIRE"] == "force"
        for file in helper_path.parent.iterdir():
            assert password not in file.read_text(encoding="utf-8")
        helper = real_run([str(helper_path), "Password:"], env=kwargs["env"], capture_output=True, timeout=5)
        assert helper.returncode == 0
        assert helper.stdout == (password + "\n").encode()
        return subprocess.CompletedProcess(argv, 0, KEY_INSTALL_STARTED + "\n", "")

    with mock.patch("npu_top.ssh_access.subprocess.run", side_effect=run_ssh):
        assert ssh.install_key_with_password(SERVER, password).state == "completed"
    assert helper_path is not None and not helper_path.parent.exists()


@pytest.mark.skipif(os.name == "nt" or not shutil.which("ssh"), reason="OpenSSH multiplexing is POSIX-only")
def test_key_verification_disables_inherited_connection_reuse(ssh, tmp_path):
    calls = []
    real_run = subprocess.run
    with mock.patch.object(ssh, "ensure_key"), mock.patch(
        "npu_top.ssh_access.subprocess.run",
        side_effect=lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    ):
        assert ssh.check_key_auth(SERVER) == (True, None)
    config = tmp_path / "ssh_config"
    config.write_text("Host *\n  ControlMaster auto\n  ControlPath cached-session\n", encoding="utf-8")
    effective = real_run(
        ["ssh", "-G", "-F", str(config), *calls[0][1:-1]], capture_output=True, text=True, timeout=5,
    )
    assert effective.returncode == 0, effective.stderr
    assert "controlmaster false" in effective.stdout
    assert "controlpath cached-session" not in effective.stdout
    # Normal observation still benefits from multiplexing.
    with mock.patch.object(ssh, "ensure_key"):
        assert "ControlMaster=auto" in ssh.ssh_base(SERVER)


@pytest.mark.parametrize("started", [False, True])
def test_remote_permission_error_cannot_trigger_another_password_after_write_started(ssh, started):
    completed = subprocess.CompletedProcess(
        [], 255, KEY_INSTALL_STARTED if started else "", "Permission denied (password).",
    )
    with mock.patch("npu_top.ssh_access.subprocess.run", return_value=completed):
        result = ssh.install_key_with_password(SERVER, "one-time")
    assert result.authentication_rejected is not started
    assert result.state == ("unknown" if started else "not_started")


def test_password_redaction_precedes_whitespace_normalization(ssh):
    password = "secret  with\ttabs"
    completed = subprocess.CompletedProcess([], 255, "", "Permission denied: " + password)
    with mock.patch("npu_top.ssh_access.subprocess.run", return_value=completed):
        result = ssh.install_key_with_password(SERVER, password)
    assert "[REDACTED]" in result.error
    assert "secret" not in result.error


@pytest.mark.parametrize("error", [TimeoutError(), subprocess.TimeoutExpired("ssh", 15)])
def test_interrupted_password_install_has_unknown_outcome_and_cleans_helper(ssh, error):
    helper = None

    def interrupt(_argv, **kwargs):
        nonlocal helper
        helper = Path(kwargs["env"]["SSH_ASKPASS"])
        raise error

    with mock.patch("npu_top.ssh_access.subprocess.run", side_effect=interrupt):
        result = ssh.install_key_with_password(SERVER, "one-time")
    assert result.state == "unknown"
    assert not result.authentication_rejected
    assert helper is not None and not helper.exists()


def test_builtin_candidate_fallback_and_verified_receipt_do_not_save_passwords(ssh, tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    try:
        server = db.upsert_server(SERVER)
        adapter = DeviceAdapter(ssh, bootstrap_state=db)
        rejected = KeyInstallResult("not_started", "密码认证被拒绝", authentication_rejected=True)
        with (
            mock.patch.object(ssh, "preflight", return_value={"ok": True}),
            mock.patch.object(ssh, "check_key_auth", side_effect=[(False, None), (True, None)]),
            mock.patch.object(ssh, "install_key_with_default_identity", return_value=rejected),
            mock.patch.object(ssh, "install_key_with_password", side_effect=[rejected, KeyInstallResult("completed")]) as install,
        ):
            result = adapter.bootstrap_with_passwords(server, ["first-secret", "second-secret"])
        assert result["ok"] and result["state"] == "verified" and result["attempts"] == 2
        assert [call.args[1] for call in install.call_args_list] == ["first-secret", "second-secret"]
        assert "secret" not in str(db.get_server(server["id"]))
    finally:
        db.close()


def test_host_key_change_stops_before_password_install(ssh):
    adapter = DeviceAdapter(ssh)
    with (
        mock.patch.object(ssh, "preflight", return_value={"ok": True}),
        mock.patch.object(ssh, "check_key_auth", return_value=(False, "REMOTE HOST IDENTIFICATION HAS CHANGED")),
        mock.patch.object(ssh, "install_key_with_default_identity") as default,
        mock.patch.object(ssh, "install_key_with_password") as password,
    ):
        result = adapter.bootstrap_with_passwords(SERVER, ["one-time"])
    assert not result["ok"] and result["attempts"] == 0
    default.assert_not_called()
    password.assert_not_called()
