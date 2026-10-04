from __future__ import annotations

import signal
import threading
import hashlib

from .api import App, AppServer
from .db import Database
from .device_adapter import DeviceAdapter
from .inventory import LOW_PRIORITY_TAG
from .probe import HostProbe
from .scheduler import AdaptiveScheduler
from .settings import Settings
from .observability import observed
from mindie_diagnostics import get_recorder, wrap_context


def run_inventory_import(app, operation) -> None:
    """Make a failed background import visible to normal reads and health."""
    app.inventory_state = {"state": "running"}
    with get_recorder("npu-top").operation("top.inventory.import") as diagnostic:
        try:
            result = operation()
        except Exception as exc:
            diagnostic.fail("inventory_import_failed", exception=exc)
            app.inventory_state = {"state": "failed", "error_type": type(exc).__name__,
                                   "error": "Configured inventory import failed; existing hosts are retained",
                                   "diagnostic_ref": diagnostic.summary()["operation_id"]}
        else:
            app.inventory_state = result if isinstance(result, dict) else {"state": "completed"}
            if app.inventory_state["state"] == "failed":
                diagnostic.fail("inventory_bootstrap_incomplete")
                app.inventory_state.update(error="Configured inventory has incomplete hosts; completed results are retained",
                                           error_type="BootstrapIncomplete", diagnostic_ref=diagnostic.summary()["operation_id"])


def import_inventory(app, inventory):
    db, adapter, scheduler = app.db, app.adapter, app.scheduler
    existing = {(item["host"], int(item["port"]), item["username"]): item for item in db.list_servers()}
    results = []
    for item in inventory:
        endpoint = (item["host"], int(item["port"]), item["username"])
        result = {"host": item["host"], "registration": "not_started", "auth": {"ok": False, "state": "not_started"}}
        stage = "register"
        try:
            server_record = existing.get(endpoint)
            if server_record is None:
                token = "|".join(map(str, endpoint)).encode()
                server_record = db.upsert_server({**item, "id": hashlib.sha256(token).hexdigest()[:32]})
            else:
                tags = [tag for tag in server_record.get("tags", []) if tag != LOW_PRIORITY_TAG]
                for tag in item.get("tags", []):
                    if tag != LOW_PRIORITY_TAG and tag not in tags:
                        tags.append(tag)
                if not item.get("workspace_enabled", True):
                    tags.append(LOW_PRIORITY_TAG)
                if tags != server_record.get("tags", []):
                    db.update_server(server_record["id"], tags=tags)
                    server_record = {**server_record, "tags": tags}
            result.update(registration="completed", server_id=server_record["id"])
            stage = "bootstrap"
            with get_recorder("npu-top").operation("top.inventory.bootstrap") as operation:
                auth = adapter.bootstrap_with_passwords(server_record, [])
                result["auth"] = auth
                if not auth.get("ok"):
                    operation.fail("bootstrap_failed", detail=auth.get("error"))
            if auth.get("ok"):
                stage = "schedule_observation"
                scheduler.collect_now(server_record["id"])
            else:
                stage = "record_auth_failure"
                db.record_failure(server_record["id"], str(auth.get("error") or "监控密钥不可用"),
                                  operation.summary()["duration_ms"])
            existing[endpoint] = server_record
        except Exception as exc:
            result["failure"] = {"stage": stage, "error_type": type(exc).__name__}
            if stage == "register":
                result["registration"] = "unknown"
            elif stage == "bootstrap":
                result["auth"] = {"ok": False, "state": "unknown", "error_type": type(exc).__name__}
        results.append(result)
    failed = sum(not row["auth"].get("ok") or bool(row.get("failure")) for row in results)
    return {"state": "failed" if failed else "completed", "failed_hosts": failed, "results": results}


@observed("top.serve")
def main() -> None:
    settings = Settings.load()
    adapter = DeviceAdapter.from_settings(settings)
    # Resolve every explicit source before creating a partial fleet or
    # contacting any host. No configured sources is a valid empty inventory.
    inventory = adapter.discover_servers()
    settings.prepare()
    db = Database(settings.state_dir / "monitor.sqlite3", settings.sqlite_max_mb * 1024 * 1024)
    db.initialize()
    adapter.ensure_key()
    probe = HostProbe(adapter, settings.ssh_timeout, settings.hbm_busy_threshold_mb)
    scheduler = AdaptiveScheduler(settings, db, probe)
    app = App(settings, db, adapter, scheduler)
    server = AppServer((settings.bind, settings.port), app)

    def stop(*_: object) -> None:
        # BaseServer.shutdown() must run outside the serve_forever thread.
        threading.Thread(target=server.shutdown, name="nfm-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    scheduler.start()

    app.inventory_state = {"state": "pending"}
    threading.Thread(target=wrap_context(lambda: run_inventory_import(app, lambda: import_inventory(app, inventory))),
                     name="nfm-inventory-import", daemon=True).start()
    print(f"npu-top: http://{settings.bind}:{settings.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()
