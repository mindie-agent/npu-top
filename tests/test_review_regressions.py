"""Acceptance of the real authority, credential, HTTP and import failure paths."""
import contextlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from npu_top.db import AuthorityStateError, Database, SCHEMA
from npu_top.device_adapter import DeviceAdapter
from npu_top.ssh_access import KeyInstallResult, SshAccess
from npu_top.serve import import_inventory, run_inventory_import
from test_console_routes import console, _json

SERVER = {"id": "one", "name": "one", "host": "192.0.2.1", "port": 22, "username": "root"}


def initialized(path):
    db = Database(path)
    db.initialize()
    server = db.upsert_server(SERVER)
    return db, server


def remove_or_replace_authority(db, mutate):
    try:
        mutate()
    except PermissionError as exc:
        if os.name != "nt" or exc.winerror not in (5, 32):
            raise
        # Windows itself prevents unlinking/replacing an open SQLite file.
        # Verify that protection, then exercise loss between connections on
        # the same Database instance. POSIX still tests a cached live handle.
        assert db.list_servers()
        db.close()
        mutate()


@pytest.mark.parametrize("fault", ["missing", "marker", "marker-replacement", "table", "replacement", "version"])
def test_cached_connection_cannot_return_or_modify_obsolete_authority(tmp_path, fault):
    db, _ = initialized(tmp_path / "monitor.sqlite3")
    assert db.list_servers()
    if fault == "missing":
        remove_or_replace_authority(db, db.path.unlink)
    elif fault == "marker":
        db.marker.write_text("broken")
    elif fault == "marker-replacement":
        marker = db.marker.with_suffix(".replacement")
        marker.write_bytes(db.marker.read_bytes())
        marker.replace(db.marker)
    elif fault == "replacement":
        # A same-schema replacement must not make the open old inode current.
        replacement, _ = initialized(tmp_path / "replacement")
        replacement.close()
        remove_or_replace_authority(db, lambda: replacement.path.replace(db.path))
    else:
        with contextlib.closing(sqlite3.connect(db.path)) as connection:
            connection.execute("DROP TABLE device_rollups" if fault == "table" else "PRAGMA user_version=99")
    try:
        with pytest.raises(AuthorityStateError):
            db.list_servers()
        with pytest.raises(AuthorityStateError):
            db.upsert_server({**SERVER, "name": "must-not-write"})
    finally:
        db.close()


@pytest.mark.parametrize("damage", ["none", "unique", "foreign-key", "index"])
def test_only_complete_previous_schema_acquires_receipt_column(tmp_path, damage):
    path = tmp_path / "monitor.sqlite3"
    previous = SCHEMA.replace("  bootstrap_receipt_json TEXT,\n", "")
    if damage == "unique":
        previous = previous.replace(",\n  UNIQUE(host, port, username)", "")
    elif damage == "foreign-key":
        previous = previous.replace(" REFERENCES servers(id) ON DELETE CASCADE", "")
    elif damage == "index":
        previous = previous.replace("CREATE INDEX IF NOT EXISTS idx_host_rollups_bucket ON host_rollups(bucket);", "")
    with contextlib.closing(sqlite3.connect(path)) as connection:
        connection.executescript(previous)
        connection.execute("INSERT INTO servers(id,name,host,created_at,updated_at) VALUES('one','one','192.0.2.1',1,1)")
        connection.commit()
    original = path.read_bytes()
    if damage != "none":
        with pytest.raises(AuthorityStateError):
            Database(path).initialize()
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".owner").exists()
    else:
        db = Database(path)
        db.initialize()
        assert db.get_server("one")["bootstrap_receipt"] is None
        assert db.connection().execute("PRAGMA user_version").fetchone()[0] == 2
        db.close()


def adapter_for(db, tmp_path, monkeypatch, *, install, auth=False):
    ssh = SshAccess(tmp_path, tmp_path)
    monkeypatch.setattr(ssh, "preflight", lambda *_: {"ok": True})
    monkeypatch.setattr(ssh, "check_key_auth", lambda *_: (auth, None))
    monkeypatch.setattr(ssh, "install_key_with_default_identity", install)
    return DeviceAdapter(ssh, bootstrap_state=db)


@pytest.mark.parametrize("outcome", ["unknown", "completed", "interrupted"])
def test_restarted_bootstrap_and_duplicate_registration_do_not_replay(tmp_path, monkeypatch, outcome):
    path = tmp_path / "monitor.sqlite3"
    db, server = initialized(path)
    calls = []
    def install(*_):
        calls.append("write")
        # External operation starts only after intent is committed and visible
        # to another connection, not merely staged in the caller's transaction.
        with contextlib.closing(sqlite3.connect(path)) as observer:
            receipt = json.loads(observer.execute("SELECT bootstrap_receipt_json FROM servers").fetchone()[0])
            assert receipt["state"] == "sending"
        if outcome == "interrupted":
            raise KeyboardInterrupt()
        return KeyInstallResult(outcome)
    adapter = adapter_for(db, tmp_path, monkeypatch, install=install)
    if outcome == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            adapter.bootstrap_with_passwords(server, ["password"])
    else:
        first = adapter.bootstrap_with_passwords(server, ["password"])
        assert not first["ok"]
    db.close()
    restarted = Database(path)
    restarted.initialize()
    duplicate = restarted.upsert_server({**SERVER, "id": "must-not-replace-id"})
    assert duplicate["id"] == "one"
    original_receipt = duplicate["bootstrap_receipt"]
    adapter = adapter_for(restarted, tmp_path, monkeypatch, install=lambda *_: pytest.fail("remote write replayed"))
    result = adapter.bootstrap_with_passwords(duplicate, ["new-password"])
    assert not result["ok"] and result["receipt"]["operation_id"] == original_receipt["operation_id"]
    assert len(calls) == 1
    monkeypatch.setattr(adapter.ssh, "check_key_auth", lambda *_: (True, None))
    reconciled = adapter.bootstrap_with_passwords(duplicate, [])
    assert reconciled["ok"] and reconciled["receipt"]["state"] == "verified"
    restarted.close()


def test_known_installation_survives_result_recording_failure(tmp_path, monkeypatch):
    db, server = initialized(tmp_path / "monitor.sqlite3")
    adapter = adapter_for(db, tmp_path, monkeypatch, install=lambda *_: KeyInstallResult("completed"))
    monkeypatch.setattr(db, "finish_bootstrap", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    result = adapter.bootstrap_with_passwords(server, ["must-not-retry"])
    assert result["installation"] == "completed" and result["state"] == "recording_failed"
    assert db.bootstrap_receipt("one")["state"] == "sending"
    db.close()


def test_inventory_retains_unknown_auth_and_later_recording_failure(tmp_path, monkeypatch):
    db, server = initialized(tmp_path / "monitor.sqlite3")
    auth = {"ok": False, "installation": "completed", "state": "verification_failed", "error": "readback failed"}
    adapter = SimpleNamespace(bootstrap_with_passwords=lambda *_: auth)
    app = SimpleNamespace(db=db, adapter=adapter, scheduler=SimpleNamespace(collect_now=lambda *_: None))
    monkeypatch.setattr(db, "record_failure", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    run_inventory_import(app, lambda: import_inventory(app, [server]))
    assert app.inventory_state["state"] == "failed"
    receipt = app.inventory_state["results"][0]
    assert receipt["registration"] == "completed" and receipt["auth"] == auth
    assert receipt["failure"]["stage"] == "record_auth_failure"
    db.close()


@pytest.mark.parametrize("method,path", [("GET", "/api/overview"), ("PUT", "/api/servers/s1"), ("DELETE", "/api/servers/s1")])
def test_damaged_state_is_a_structured_http_failure(method, path):
    with console() as (db, host, port):
        db.marker.write_text("broken")
        status, content_type, result = _json(host, port, method, path, {"enabled": False} if method == "PUT" else None)
        assert status == 500 and content_type.startswith("application/json")
        assert result["error_type"] == "AuthorityStateError" and result["diagnostic_ref"]


def test_later_batch_record_failure_does_not_replace_completed_key_receipt(monkeypatch):
    from test_console_routes import StubAdapter
    auth = {"ok": False, "installation": "completed", "state": "verification_failed", "error": "login not verified"}
    monkeypatch.setattr(StubAdapter, "bootstrap_with_passwords", lambda *_: auth)
    with console(seed_server=False) as (db, host, port):
        monkeypatch.setattr(db, "record_failure", lambda *_: (_ for _ in ()).throw(OSError("record failure")))
        status, _, result = _json(host, port, "POST", "/api/servers/batch", {"servers": [SERVER], "passwords": []})
        assert status == 207
        receipt = result["results"][0]
        assert receipt["registration"] == "completed" and receipt["auth"] == auth
        assert receipt["failure"]["stage"] == "record_auth_failure"


def test_password_echo_is_not_saved_in_error_receipt(tmp_path, monkeypatch):
    from npu_top.inventory import ExternalKeyBootstrap
    password = "unique-one-time-password"
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "failed: " + password))
    result = ExternalKeyBootstrap("synthetic").run(SERVER, tmp_path / "public", password)
    assert password not in result.error and "[REDACTED]" in result.error


def test_concurrent_bootstrap_reserves_one_remote_write(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    db, server = initialized(tmp_path / "monitor.sqlite3")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def install(*_):
        calls.append("write")
        entered.set()
        assert release.wait(3)
        return KeyInstallResult("unknown")
    adapter = adapter_for(db, tmp_path, monkeypatch, install=install)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(adapter.bootstrap_with_passwords, server, [])
        assert entered.wait(3)
        second = executor.submit(adapter.bootstrap_with_passwords, server, []).result(timeout=3)
        release.set()
        first_result = first.result(timeout=3)
    assert len(calls) == 1
    assert first_result["receipt"]["operation_id"] == second["receipt"]["operation_id"]
    assert second["installation"] == "unknown"
    db.close()


def test_key_install_read_error_is_not_treated_as_key_absence(tmp_path, monkeypatch):
    import shlex
    ssh = SshAccess(tmp_path, tmp_path)
    ssh.public_key.parent.mkdir()
    ssh.public_key.write_text("ssh-ed25519 SYNTHETIC test")
    real_run = subprocess.run
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 0, "", "")
        remote = shlex.split(command[-1])[0].replace("~/.ssh", shlex.quote(str(tmp_path / "remote-ssh")))
        return real_run(["sh", "-c", "grep() { return 2; }; " + remote], capture_output=True, text=True)
    monkeypatch.setattr(subprocess, "run", run)
    result = ssh.install_key_with_default_identity(SERVER)
    assert result.state == "unknown"
    assert (tmp_path / "remote-ssh" / "authorized_keys").read_text() == ""


def test_only_known_auth_rejection_allows_next_password(tmp_path, monkeypatch):
    db, server = initialized(tmp_path / "monitor.sqlite3")
    adapter = adapter_for(db, tmp_path, monkeypatch,
                          install=lambda *_: KeyInstallResult("not_started", authentication_rejected=True))
    attempts = []
    def install(_server, _key, password):
        attempts.append(password)
        if len(attempts) == 1:
            return KeyInstallResult("not_started", authentication_rejected=True)
        return KeyInstallResult("unknown", "remote outcome unknown")
    adapter.key_bootstrap = SimpleNamespace(run=install)
    result = adapter.bootstrap_with_passwords(server, ["rejected", "unknown", "must-not-send"])
    assert attempts == ["rejected", "unknown"]
    assert result["receipt"]["attempts"] == 2 and result["receipt"]["state"] == "unknown"
    db.close()
