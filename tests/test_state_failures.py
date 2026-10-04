"""Retain authoritative monitor data and expose failed background work."""
import http.client
import json
import sqlite3
import subprocess
import threading
from types import SimpleNamespace

import pytest

from npu_top.db import AuthorityStateError, Database
from npu_top.probe import HostProbe, attach_process_details


@pytest.mark.parametrize("fault", ["missing", "empty", "table", "marker"])
def test_existing_state_is_not_reinitialized(tmp_path, fault):
    path = tmp_path / "monitor.sqlite3"
    db = Database(path)
    db.initialize()
    db.upsert_server({"id": "one", "name": "one", "host": "192.0.2.1", "port": 22, "username": "root"})
    db.close()
    if fault == "missing":
        path.unlink()
    elif fault == "empty":
        path.write_bytes(b"")
    elif fault == "table":
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE device_rollups")
    else:
        db.marker.write_text("broken")
    before = path.read_bytes() if path.exists() else None
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        Database(path).initialize()
    assert (path.read_bytes() if path.exists() else None) == before
    if fault == "table":
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT id FROM servers").fetchall() == [("one",)]


def test_missing_legacy_database_with_owned_identity_is_not_first_use(tmp_path):
    (tmp_path / "keys").mkdir()
    key = tmp_path / "keys" / "id_ed25519"
    key.write_text("synthetic-key")
    db = Database(tmp_path / "monitor.sqlite3")
    with pytest.raises(RuntimeError, match="missing from existing"):
        db.initialize()
    assert key.read_text() == "synthetic-key"
    assert not db.path.exists() and not db.marker.exists()


def test_worker_open_cannot_recreate_removed_database(tmp_path):
    db = Database(tmp_path / "db")
    db.initialize()
    db.path.unlink()
    with pytest.raises(AuthorityStateError, match="missing"):
        db.connection()
    assert not db.path.exists()


def test_background_inventory_failure_reaches_http_health_and_normal_read():
    from npu_top.api import App, AppServer
    from npu_top.serve import run_inventory_import
    app = App(SimpleNamespace(), SimpleNamespace(list_servers=lambda: [], close=lambda: None), object(),
              SimpleNamespace(runtime_state=lambda: {"collector_status": "running"}, snapshots=lambda: {}))
    def fail():
        raise OSError("synthetic import failure")
    run_inventory_import(app, fail)
    assert app.agent_servers()["runtime"]["inventory"]["state"] == "failed"
    server = AppServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = http.client.HTTPConnection(*server.server_address, timeout=3)
        client.request("GET", "/api/health")
        response = client.getresponse()
        payload = json.loads(response.read())
        assert response.status == 503
        assert payload["runtime"]["inventory"]["error_type"] == "OSError"
        client.close()
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_failed_process_detail_probe_has_distinct_evidence(monkeypatch):
    probe = HostProbe(object(), 12)
    monkeypatch.setattr(probe, "_run_script", lambda *_: subprocess.CompletedProcess([], 255, "", "channel lost"))
    details, status = probe._collect_process_details({"id": "one"}, [4])
    assert details == {} and status["state"] == "unavailable" and status["exit_code"] == 255
    devices = [{"processes": [{"pid": 4, "npu_memory_mb": 42}]}]
    attach_process_details(devices, details, {})
    assert devices[0]["processes"][0]["npu_memory_mb"] == 42
    assert devices[0]["processes"][0]["command"] is None


def test_completed_probe_survives_record_failure_without_advancing_history():
    from npu_top.scheduler import AdaptiveScheduler
    settings = SimpleNamespace(idle_interval=120, history_interval=30, infrastructure_interval=60,
                               retention_days=90, max_workers=1)
    def fail(*args):
        raise OSError("synthetic disk error")
    db = SimpleNamespace(latest_persisted=lambda: {}, close=lambda: None,
                         list_servers=lambda: [{"id": "one", "enabled": True}], record_success=fail)
    probe = SimpleNamespace(collect=lambda *_: {"server_id": "one", "collected_at": 100,
                                               "summary": {"npu_count": 8}})
    scheduler = AdaptiveScheduler(settings, db, probe)
    scheduler._collect_cycle(set())
    snapshot = scheduler.snapshots()["one"]
    assert snapshot["status"] == "online" and snapshot["summary"]["npu_count"] == 8
    assert snapshot["recording"]["state"] == "failed" and snapshot["recording"]["diagnostic_ref"]
    assert "one" not in scheduler._latest_persisted
    assert scheduler.runtime_state()["storage_failures"] == 1


@pytest.mark.parametrize("outcome", ["unknown", "completed"])
def test_bootstrap_never_replays_unknown_or_completed_write(tmp_path, monkeypatch, outcome):
    from npu_top.ssh_access import SshAccess, KeyInstallResult
    from npu_top.device_adapter import DeviceAdapter
    ssh = SshAccess(tmp_path, tmp_path)
    monkeypatch.setattr(ssh, "preflight", lambda *_: {"ok": True})
    monkeypatch.setattr(ssh, "check_key_auth", lambda *_: (False, None))
    monkeypatch.setattr(ssh, "install_key_with_default_identity", lambda *_: KeyInstallResult(
        "not_started", authentication_rejected=True))
    calls = []
    def install(*args):
        calls.append(args)
        return KeyInstallResult(outcome, "synthetic outcome")
    db = Database(tmp_path / "monitor.sqlite3")
    db.initialize()
    server = db.upsert_server({"id": "one", "name": "one", "host": "192.0.2.1", "port": 22, "username": "root"})
    adapter = DeviceAdapter(ssh, key_bootstrap=SimpleNamespace(run=install), bootstrap_state=db)
    result = adapter.bootstrap_with_passwords(server, ["first", "second"])
    db.close()
    assert len(calls) == 1
    assert result["installation"] == outcome
    assert result["state"] == ("unknown" if outcome == "unknown" else "verification_failed")
    assert not result["ok"]


def test_external_bootstrap_nonzero_is_uncertain_except_authentication_rejection(tmp_path, monkeypatch):
    from npu_top.inventory import ExternalKeyBootstrap
    bootstrap = ExternalKeyBootstrap("synthetic-command")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "failed"))
    assert bootstrap.run({"host": "x", "port": 22, "username": "u"}, tmp_path / "public", "one-time").state == "unknown"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 77, "", "denied"))
    result = bootstrap.run({"host": "x", "port": 22, "username": "u"}, tmp_path / "public", "one-time")
    assert result.state == "not_started" and result.authentication_rejected


@pytest.mark.parametrize("existing", ["private", "public"])
def test_partial_keypair_never_regenerates(tmp_path, monkeypatch, existing):
    from npu_top.ssh_access import SshAccess
    ssh = SshAccess(tmp_path, tmp_path)
    ssh.private_key.parent.mkdir()
    key = ssh.private_key if existing == "private" else ssh.public_key
    key.write_text("synthetic existing key")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not regenerate identity"))
    with pytest.raises(RuntimeError, match="密钥对不完整"):
        ssh.ensure_key()
    assert key.read_text() == "synthetic existing key"


def test_concurrent_first_start_serializes_schema_initialization(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "db"
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda _: Database(path).initialize(), range(12)))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM servers").fetchone() == (0,)
