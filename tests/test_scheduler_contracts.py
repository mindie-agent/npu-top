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


class GatedProbe(Probe):
    def __init__(self):
        super().__init__()
        self.ordinary_started = threading.Event()
        self.ordinary_release = threading.Event()
        self.forced_started = threading.Event()
        self.forced_release = threading.Event()

    def collect(self, server, include_infrastructure):
        if len(self.calls) == 1:
            assert not include_infrastructure
            self.ordinary_started.set()
            assert self.ordinary_release.wait(5)
        elif len(self.calls) == 2:
            assert include_infrastructure
            self.forced_started.set()
            assert self.forced_release.wait(5)
        return super().collect(server, include_infrastructure)


def test_live_request_does_not_accept_an_older_inflight_ordinary_probe(scheduled):
    scheduler, _ = scheduled
    probe = GatedProbe()
    scheduler.probe = probe
    scheduler.start()
    assert scheduler._condition.waiting.wait(2)
    scheduler.collect_now("one")
    assert probe.ordinary_started.wait(2)
    returned = threading.Event()
    result = {}

    def request():
        try:
            result["snapshot"] = scheduler.collect_and_wait(
                "one", force_infrastructure=True, timeout=3)
        except Exception as exc:
            result["error"] = exc
        finally:
            returned.set()

    caller = threading.Thread(target=request)
    caller.start()
    try:
        with scheduler._condition:
            assert scheduler._condition.wait_for(
                lambda: "one" in scheduler._force_infrastructure, timeout=2)
        probe.ordinary_release.set()
        assert probe.forced_started.wait(2)
        assert not returned.wait(0.1), "ordinary completion cannot satisfy forced infrastructure"
        probe.forced_release.set()
        assert returned.wait(2)
        assert "error" not in result
        assert result["snapshot"]["infrastructure_fresh"] is True
        assert result["snapshot"]["status"] == "online"
        assert probe.calls == [True, False, True]
    finally:
        probe.ordinary_release.set()
        probe.forced_release.set()
        caller.join(4)


def test_concurrent_live_requests_share_a_satisfying_completed_probe(scheduled):
    scheduler, probe = scheduled
    results, errors = [], []

    def request():
        try:
            results.append(scheduler.collect_and_wait("one", force_infrastructure=True, timeout=2))
        except Exception as exc:
            errors.append(exc)

    callers = [threading.Thread(target=request) for _ in range(2)]
    for caller in callers:
        caller.start()
    try:
        assert scheduler._condition.two_waiting.wait(2)
        scheduler._collect_cycle({"one"}, {"one"})
        for caller in callers:
            caller.join(2)
        assert not errors
        assert len(results) == 2
        assert results[0] == results[1]
        assert results[0]["infrastructure_fresh"] is True
        assert probe.calls == [True]
    finally:
        scheduler.stop()
        for caller in callers:
            caller.join(2)


def test_live_timeout_does_not_return_cached_success(scheduled):
    scheduler, _ = scheduled
    scheduler._collect_cycle({"one"}, {"one"})
    with pytest.raises(TimeoutError, match="live collection timed out"):
        scheduler.collect_and_wait("one", force_infrastructure=True, timeout=1)


def test_stopping_collector_wakes_live_waiter_with_failure(scheduled):
    scheduler, _ = scheduled
    errors = []

    def request():
        try:
            scheduler.collect_and_wait("one", force_infrastructure=True, timeout=2)
        except Exception as exc:
            errors.append(exc)

    caller = threading.Thread(target=request)
    caller.start()
    assert scheduler._condition.waiting.wait(2)
    scheduler.stop()
    caller.join(2)
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "collector is stopped"


def test_later_ordinary_cycle_cannot_erase_completed_infrastructure_request(scheduled):
    scheduler, probe = scheduled
    result = {}

    def request():
        try:
            result["snapshot"] = scheduler.collect_and_wait(
                "one", force_infrastructure=True, timeout=1)
        except Exception as exc:
            result["error"] = exc

    caller = threading.Thread(target=request)
    caller.start()
    assert scheduler._condition.waiting.wait(2)
    try:
        # Prevent the waiter from reacquiring its condition between two real
        # collection/publication cycles, as under scheduling contention.
        with scheduler._condition:
            scheduler._collect_cycle({"one"}, {"one"})
            scheduler._collect_cycle({"one"})
        caller.join(2)
        assert "error" not in result
        assert result["snapshot"]["infrastructure_fresh"] is True
        assert probe.calls == [True, False]
    finally:
        scheduler.stop()
        caller.join(2)
