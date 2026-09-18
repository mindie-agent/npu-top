"""HTTP console routes: status, content type, loopback bind, frontend assets.

No NPU, no npu-smi. The collector is a stub. The server is stdlib
``http.server``; requests go through ``http.client`` against a live
loopback socket.
"""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from npu_top.api import App, AppServer, require_loopback_bind
from npu_top.db import Database
from npu_top.settings import Settings
from npu_top.static_files import STATIC_MISSING, require_static


JSON_TYPE = "application/json; charset=utf-8"


class StubScheduler:
    """In-memory collector stand-in. Never calls npu-smi or SSH."""

    def snapshots(self) -> dict:
        return {}

    def runtime_state(self) -> dict:
        return {
            "mode": "idle",
            "effective_interval": 120,
            "idle_interval": 120,
            "history_interval": 30,
            "active_viewers": 0,
            "collecting": False,
            "last_cycle_at": None,
            "cycle_duration_ms": None,
            "allowed_intervals": [1, 5, 10, 30],
        }

    def collect_now(self, server_id=None, force_infrastructure=False) -> None:
        return None

    def heartbeat(self, client_id, interval, visible) -> dict:
        state = self.runtime_state()
        if visible:
            state = {**state, "mode": "interactive", "active_viewers": 1, "effective_interval": interval}
        return state

    def remove_lease(self, client_id) -> None:
        return None

    def collect_and_wait(self, server_id, *, force_infrastructure=False, timeout=30) -> dict:
        raise TimeoutError("stub collector does not probe")


class StubAdapter:
    def validate_endpoint(self, host, port, username) -> None:
        return None

    def bootstrap_with_passwords(self, server, passwords) -> dict:
        return {"ok": True}


def _settings(state: Path) -> Settings:
    return Settings(state, state, "127.0.0.1", 8789, 120, 30, 60, 90, 4, 12, 8192)


@contextmanager
def console(*, web_root: Path | None = None, seed_server: bool = True):
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        db = Database(state / "test.sqlite3")
        db.initialize()
        if seed_server:
            db.upsert_server({
                "id": "s1",
                "name": "lab",
                "host": "192.0.2.10",
                "port": 22,
                "username": "root",
                "tags": ["A3"],
            })
        app = App(_settings(state), db, StubAdapter(), StubScheduler())  # type: ignore[arg-type]
        if web_root is not None:
            app.web_root = web_root
        else:
            static = (state / "static").resolve()
            static.mkdir()
            (static / "index.html").write_text("<!doctype html><title>npu-top</title>", encoding="utf-8")
            app.web_root = static
        server = AppServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, name="npu-top-test", daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            yield db, host, int(port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            db.close()


def _request(host: str, port: int, method: str, path: str, body: bytes | None = None, content_type: str | None = None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        headers = {}
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
            headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        return response.status, response.getheader("Content-Type"), payload
    finally:
        conn.close()


def _json(host: str, port: int, method: str, path: str, payload: dict | None = None):
    body = None if payload is None else json.dumps(payload).encode()
    status, content_type, raw = _request(host, port, method, path, body)
    parsed = json.loads(raw.decode()) if raw else None
    return status, content_type, parsed


class LoopbackBindTests(unittest.TestCase):
    def test_require_loopback_bind_accepts_only_loopback(self) -> None:
        self.assertEqual(require_loopback_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(require_loopback_bind("localhost"), "localhost")
        self.assertEqual(require_loopback_bind("::1"), "::1")
        with self.assertRaises(ValueError) as caught:
            require_loopback_bind("0.0.0.0")
        self.assertIn("loopback", str(caught.exception))
        with self.assertRaises(ValueError):
            require_loopback_bind("192.0.2.10")

    def test_server_refuses_to_bind_non_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            db = Database(state / "test.sqlite3")
            db.initialize()
            app = App(_settings(state), db, StubAdapter(), StubScheduler())  # type: ignore[arg-type]
            app.web_root = state
            with self.assertRaises(ValueError) as caught:
                AppServer(("0.0.0.0", 0), app)
            self.assertIn("0.0.0.0", str(caught.exception))
            db.close()

    def test_default_settings_bind_is_loopback(self) -> None:
        isolated = {key: value for key, value in os.environ.items() if key != "NFM_BIND"}
        with mock.patch.dict(os.environ, isolated, clear=True):
            self.assertEqual(Settings.load().bind, "127.0.0.1")

    def test_loopback_server_accepts_loopback_clients(self) -> None:
        with console() as (_db, host, port):
            self.assertEqual(host, "127.0.0.1")
            status, content_type, payload = _json(host, port, "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, JSON_TYPE)
            self.assertEqual(payload["status"], "ok")


class FrontendAssetTests(unittest.TestCase):
    def test_require_static_returns_index_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            index = Path(root) / "index.html"
            index.write_text("<!doctype html><title>ok</title>", encoding="utf-8")
            with mock.patch("npu_top.static_files.static_dir", return_value=Path(root)):
                found = require_static()
            self.assertEqual(found, Path(root))
            self.assertTrue((found / "index.html").is_file())

    def test_missing_frontend_is_503_with_package_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-static"
            with console(web_root=missing, seed_server=False) as (_db, host, port):
                status, content_type, payload = _json(host, port, "GET", "/")
        self.assertEqual(status, 503)
        self.assertEqual(content_type, JSON_TYPE)
        self.assertEqual(payload["error"], STATIC_MISSING)

    def test_present_frontend_is_served_as_html(self) -> None:
        with console() as (_db, host, port):
            status, content_type, raw = _request(host, port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIsNotNone(content_type)
        self.assertTrue(content_type.startswith("text/html"), content_type)
        self.assertIn(b"npu-top", raw)


class ConsoleRouteTests(unittest.TestCase):
    def test_get_routes_status_and_content_type(self) -> None:
        with console() as (_db, host, port):
            cases = (
                ("/api/health", 200),
                ("/api/overview", 200),
                ("/api/agent/servers", 200),
                ("/api/agent/npu?host=192.0.2.10", 200),
                ("/api/agent/server?host=192.0.2.10&mode=cache", 200),
                ("/api/agent/capacity", 200),
                ("/api/servers", 200),
                ("/api/history", 200),
                ("/api/history/heatmap?server_id=s1", 200),
            )
            for path, expected in cases:
                with self.subTest(path=path):
                    status, content_type, payload = _json(host, port, "GET", path)
                    self.assertEqual(status, expected, path)
                    self.assertEqual(content_type, JSON_TYPE, path)
                    self.assertIsInstance(payload, dict, path)

    def test_heatmap_without_server_id_is_400(self) -> None:
        with console() as (_db, host, port):
            status, content_type, payload = _json(host, port, "GET", "/api/history/heatmap")
        self.assertEqual(status, 400)
        self.assertEqual(content_type, JSON_TYPE)
        self.assertIn("server_id", payload["error"])

    def test_agent_npu_unknown_host_is_404(self) -> None:
        with console() as (_db, host, port):
            status, content_type, payload = _json(host, port, "GET", "/api/agent/npu?host=missing")
        self.assertEqual(status, 404)
        self.assertEqual(content_type, JSON_TYPE)
        self.assertIn("not found", payload["error"])

    def test_head_health_has_json_type_and_no_body(self) -> None:
        with console() as (_db, host, port):
            status, content_type, raw = _request(host, port, "HEAD", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, JSON_TYPE)
        self.assertEqual(raw, b"")

    def test_options_is_204(self) -> None:
        with console() as (_db, host, port):
            status, _content_type, raw = _request(host, port, "OPTIONS", "/api/health")
        self.assertEqual(status, 204)
        self.assertEqual(raw, b"")

    def test_post_collect_routes(self) -> None:
        with console() as (_db, host, port):
            status, content_type, payload = _json(host, port, "POST", "/api/collect")
            self.assertEqual(status, 202)
            self.assertEqual(content_type, JSON_TYPE)
            self.assertTrue(payload["accepted"])
            status, content_type, payload = _json(host, port, "POST", "/api/servers/s1/collect")
            self.assertEqual(status, 202)
            self.assertEqual(content_type, JSON_TYPE)
            self.assertTrue(payload["accepted"])

    def test_post_batch_servers(self) -> None:
        with console() as (_db, host, port):
            status, content_type, payload = _json(host, port, "POST", "/api/servers/batch", {
                "servers": [{"name": "other", "host": "192.0.2.11", "port": 22, "username": "root"}],
                "passwords": [],
            })
        self.assertEqual(status, 207)
        self.assertEqual(content_type, JSON_TYPE)
        self.assertEqual(len(payload["results"]), 1)
        self.assertTrue(payload["results"][0]["auth"]["ok"])

    def test_post_unknown_is_404(self) -> None:
        with console() as (_db, host, port):
            status, content_type, payload = _json(host, port, "POST", "/api/no-such")
        self.assertEqual(status, 404)
        self.assertEqual(content_type, JSON_TYPE)
        self.assertEqual(payload["error"], "not found")

    def test_put_and_delete_viewer_and_server(self) -> None:
        with console() as (_db, host, port):
            status, content_type, payload = _json(
                host, port, "PUT", "/api/viewers/viewer_01",
                {"interval": 10, "visible": True},
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, JSON_TYPE)
            self.assertEqual(payload["mode"], "interactive")

            status, content_type, payload = _json(
                host, port, "PUT", "/api/servers/s1",
                {"enabled": False, "tags": ["A3", "lab"]},
            )
            self.assertEqual(status, 200)
            self.assertEqual(content_type, JSON_TYPE)
            self.assertFalse(payload["server"]["enabled"])

            status, content_type, payload = _json(host, port, "DELETE", "/api/viewers/viewer_01")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, JSON_TYPE)
            self.assertTrue(payload["ok"])

            status, content_type, payload = _json(host, port, "DELETE", "/api/servers/s1")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, JSON_TYPE)
            self.assertTrue(payload["ok"])

            status, content_type, payload = _json(host, port, "DELETE", "/api/servers/s1")
            self.assertEqual(status, 404)
            self.assertEqual(content_type, JSON_TYPE)

    def test_put_unknown_is_404(self) -> None:
        with console() as (_db, host, port):
            status, content_type, payload = _json(host, port, "PUT", "/api/no-such", {"x": 1})
        self.assertEqual(status, 404)
        self.assertEqual(content_type, JSON_TYPE)
        self.assertEqual(payload["error"], "not found")


if __name__ == "__main__":
    unittest.main()
