"""Exercise the real scheduler with SQLite, a controlled clock and probe I/O."""
import threading
import time
from types import SimpleNamespace

import pytest

import npu_top.scheduler as scheduler_module
from npu_top.db import Database
from npu_top.scheduler import AdaptiveScheduler


class ObservableCondition(threading.Condition):
    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()
        self.waiting_threads = set()
        self.two_waiting = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        self.waiting_threads.add(threading.get_ident())
        if len(self.waiting_threads) >= 2:
            self.two_waiting.set()
        return super().wait(timeout)


class Probe:
    def __init__(self):
        self.calls = []
        self.completed = threading.Event()

    def collect(self, server, include_infrastructure):
        self.calls.append(include_infrastructure)
        self.completed.set()
        return {"server_id": server["id"], "collected_at": time.time(),
                "summary": {}, "devices": [], "infrastructure_fresh": include_infrastructure}


@pytest.fixture
def scheduled(tmp_path):
    db = Database(tmp_path / "scheduler.sqlite3")
    db.initialize()
    db.upsert_server({"id": "one", "name": "fixture", "host": "unused.invalid", "port": 22, "username": "test"})
    settings = SimpleNamespace(idle_interval=120, history_interval=30,
                               infrastructure_interval=60, retention_days=90, max_workers=1)
    probe = Probe()
    scheduler = AdaptiveScheduler(settings, db, probe)
    scheduler._condition = ObservableCondition()
    yield scheduler, probe
    scheduler.stop()
    db.close()


@pytest.mark.parametrize("old_interval", [None, 10])
def test_faster_viewer_advances_pending_sample(scheduled, monkeypatch, old_interval):
    scheduler, probe = scheduled
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(
        monotonic=lambda: clock.now, time=time.time, perf_counter=time.perf_counter))
    if old_interval is not None:
        scheduler.heartbeat("viewer", old_interval, True)
    scheduler.start()
    assert scheduler._condition.waiting.wait(2), scheduler.runtime_state()
    assert len(probe.calls) == 1
    probe.completed.clear()
    scheduler.heartbeat("viewer", 1, True)
    with scheduler._condition:
        clock.now += 1.1
        scheduler._condition.notify_all()
    assert probe.completed.wait(1), "new viewer must advance the old idle/10-second deadline"
    assert len(probe.calls) == 2


@pytest.mark.parametrize("transition", ["hidden", "expired"])
def test_inactive_viewer_restores_idle_deadline(scheduled, monkeypatch, transition):
    scheduler, probe = scheduled
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(
        monotonic=lambda: clock.now, time=time.time, perf_counter=time.perf_counter))
    scheduler.heartbeat("viewer", 1, True)
    scheduler.start()
    assert scheduler._condition.waiting.wait(2)
    probe.completed.clear()
    if transition == "hidden":
        scheduler.heartbeat("viewer", 1, False)
    with scheduler._condition:
        clock.now += 2 if transition == "hidden" else 21
        scheduler._condition.notify_all()
    assert not probe.completed.wait(0.1), "inactive viewers must not retain a fast deadline"
    assert scheduler.runtime_state()["mode"] == "idle"
    assert len(probe.calls) == 1
