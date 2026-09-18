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


@observed("top.serve")
def main() -> None:
    settings = Settings.load()
    settings.prepare()
    db = Database(settings.state_dir / "monitor.sqlite3", settings.sqlite_max_mb * 1024 * 1024)
    db.initialize()
    adapter = DeviceAdapter.from_settings(settings)
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

    @observed("top.inventory.import")
    def import_inventory() -> None:
        existing = {
            (item["host"], int(item["port"]), item["username"]): item
            for item in db.list_servers()
        }
        for item in adapter.discover_servers():
            endpoint = (item["host"], int(item["port"]), item["username"])
            server_record = existing.get(endpoint)
            if server_record is None:
                token = "|".join(map(str, endpoint)).encode()
                server_record = db.upsert_server({**item, "id": hashlib.sha256(token).hexdigest()[:32]})
            else:
                tags = [tag for tag in server_record.get("tags", []) if tag != LOW_PRIORITY_TAG]
                for tag in item.get("tags", []):
                    if tag == LOW_PRIORITY_TAG:
                        continue
                    if tag not in tags:
                        tags.append(tag)
                if not item.get("workspace_enabled", True):
                    tags.append(LOW_PRIORITY_TAG)
                if tags != server_record.get("tags", []):
                    db.update_server(server_record["id"], tags=tags)
                    server_record = {**server_record, "tags": tags}
            with get_recorder("npu-top").operation("top.inventory.bootstrap") as operation:
                auth = adapter.bootstrap_with_passwords(server_record, [])
                if not auth.get("ok"):
                    operation.fail("bootstrap_failed", detail=auth.get("error"))
            if auth.get("ok"):
                scheduler.collect_now(server_record["id"])
            else:
                db.record_failure(server_record["id"], str(auth.get("error") or "监控密钥不可用"),
                                  operation.summary()["duration_ms"])
            existing[endpoint] = server_record

    threading.Thread(target=wrap_context(import_inventory), name="nfm-inventory-import", daemon=True).start()
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
