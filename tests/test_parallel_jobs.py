import sqlite3
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from router_configurator.app import create_app
from router_configurator.jobs import JobWorker, ServiceLock
from router_configurator.lists import parse_routing_list
from router_configurator.router import RouterError
from router_configurator.storage import Store, utc_now


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Concurrent worker did not reach the expected state.")


def test_http_jobs_run_in_parallel_with_a_limit(config):
    config = replace(config, max_parallel_jobs=2)
    release = threading.Event()
    both_entered = threading.Event()
    guard = threading.Lock()
    active = set()
    peak = 0

    class Client:
        def __init__(self, hostname, *args, **kwargs):
            self.hostname = hostname

        def update(self, routing):
            nonlocal peak
            with guard:
                active.add(self.hostname)
                peak = max(peak, len(active))
                if len(active) == 2:
                    both_entered.set()
            try:
                assert release.wait(5)
            finally:
                with guard:
                    active.remove(self.hostname)

        def close(self):
            pass

    with TestClient(create_app(config, client_factory=Client)) as client:
        try:
            headers = {"X-API-Key": config.api_key}
            jobs = []
            for hostname in ("first.example", "second.example", "third.example"):
                response = client.post("/api/v1/router-configurations", json={"hostname": hostname, "action": "update"}, headers=headers)
                assert response.status_code == 202
                jobs.append(response.json()["status_url"])
            assert both_entered.wait(5), "The first two updates must overlap."
            assert [client.get(url, headers=headers).json()["status"] for url in jobs] == ["running", "running", "queued"]
            release.set()
            wait_for(lambda: all(client.get(url, headers=headers).json()["status"] == "succeeded" for url in jobs))
            assert peak == 2
        finally:
            release.set()


def test_busy_router_does_not_block_another_and_alias_is_serialized(config):
    config = replace(config, max_parallel_jobs=2)
    store = Store(config.data_dir / "jobs.sqlite3")
    first_entered, other_entered, release = threading.Event(), threading.Event(), threading.Event()
    seen = []

    class Client:
        def __init__(self, hostname, *args, **kwargs):
            self.hostname = hostname

        def update(self, routing):
            seen.append(self.hostname)
            if self.hostname == "Router.Example":
                first_entered.set()
                assert release.wait(5)
            elif self.hostname == "other.example":
                other_entered.set()

        def close(self):
            pass

    store.initialize()
    routing = parse_routing_list(config.list_path.read_text())
    first = store.enqueue("Router.Example", routing)
    duplicate = store.enqueue("https://router.example.:443/", routing)
    other = store.enqueue("other.example", routing)
    worker = JobWorker(config, store, Client)
    worker.start()
    try:
        assert first_entered.wait(5)
        assert other_entered.wait(5), "The busy host at the head must not block other hosts."
        wait_for(lambda: store.get(other)["status"] == "succeeded")
        assert store.get(first)["status"] == "running"
        assert store.get(duplicate)["status"] == "queued"
        release.set()
        wait_for(lambda: store.get(duplicate)["status"] == "succeeded")
        assert seen.index("Router.Example") < seen.index("https://router.example.:443/")
    finally:
        release.set()
        worker.stop()


def test_failed_update_releases_router_for_next_job(config):
    store = Store(config.data_dir / "jobs.sqlite3")
    store.initialize()
    routing = parse_routing_list(config.list_path.read_text())
    first = store.enqueue("router.example", routing)
    second = store.enqueue("router.example", routing)
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def update(self, routing):
            calls.append(1)
            if len(calls) == 1:
                raise RouterError("First update failed.")

        def close(self):
            pass

    worker = JobWorker(config, store, Client)
    worker.start()
    try:
        wait_for(lambda: store.get(second)["status"] == "succeeded")
        assert store.get(first)["status"] == "failed"
        assert len(calls) == 2
    finally:
        worker.stop()


def test_shutdown_waits_for_all_active_jobs_and_keeps_service_lock(config):
    config = replace(config, max_parallel_jobs=2)
    store = Store(config.data_dir / "jobs.sqlite3")
    store.initialize()
    routing = parse_routing_list(config.list_path.read_text())
    first = store.enqueue("first.example", routing)
    second = store.enqueue("second.example", routing)
    third = store.enqueue("third.example", routing)
    entered = {name: threading.Event() for name in ("first.example", "second.example")}
    release = {name: threading.Event() for name in entered}

    class Client:
        def __init__(self, hostname, *args, **kwargs):
            self.hostname = hostname

        def update(self, routing):
            entered[self.hostname].set()
            assert release[self.hostname].wait(5)

        def close(self):
            pass

    worker = JobWorker(config, store, Client)
    worker.start()
    try:
        assert all(event.wait(5) for event in entered.values())
        worker.stop(timeout=0)
        release["first.example"].set()
        wait_for(lambda: store.get(first)["status"] == "succeeded")
        wait_for(lambda: sum(thread.is_alive() for thread in worker._threads) == 1)
        competitor = ServiceLock(config.data_dir / "worker.lock")
        with pytest.raises(RuntimeError, match="Другой процесс"):
            competitor.acquire()
        assert store.get(second)["status"] == "running"
        assert store.get(third)["status"] == "queued"
        release["second.example"].set()
        worker.stop()
        assert store.get(second)["status"] == "succeeded"
        assert store.get(third)["status"] == "queued"
        competitor.acquire()
        competitor.release()
    finally:
        for event in release.values():
            event.set()
        worker.stop()


def test_shutdown_uses_one_total_timeout(config, monkeypatch):
    worker = JobWorker(config, Store(config.data_dir / "jobs.sqlite3"))
    timeouts = []
    clock = {"now": 0.0}
    monkeypatch.setattr("router_configurator.jobs.time.monotonic", lambda: clock["now"])

    class Thread:
        ident = 1

        def join(self, timeout):
            timeouts.append(timeout)
            clock["now"] += timeout

        def is_alive(self):
            return True

    worker._threads = [Thread(), Thread(), Thread()]
    worker.stop(timeout=0.05)
    assert timeouts == [0.05, 0, 0]
    assert clock["now"] == 0.05


def test_old_database_migration_keeps_history_and_snapshots(config):
    config.data_dir.mkdir()
    path = config.data_dir / "jobs.sqlite3"
    routing = parse_routing_list(config.list_path.read_text())
    created = utc_now()
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, hostname TEXT NOT NULL, action TEXT NOT NULL,
                status TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, message TEXT NOT NULL DEFAULT '', error TEXT
            )
        """)
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id), created_at TEXT NOT NULL, message TEXT NOT NULL)")
        for job_id, hostname, status in (("first", "ROUTER.EXAMPLE.", "running"), ("second", "https://router.example:8443", "queued"), ("history", "other.example", "succeeded")):
            db.execute("INSERT INTO jobs(id,hostname,action,status,snapshot,created_at) VALUES(?,?,'update',?,?,?)", (job_id, hostname, status, routing.serialize(), created))
        db.execute("INSERT INTO events(job_id,created_at,message) VALUES('history',?,'Old event')", (created,))
    store = Store(path)
    store.initialize()
    store.initialize()  # Migration is idempotent.
    assert store.get("history")["events"] == [{"created_at": created, "message": "Old event"}]
    assert store.claim_next() is None  # The migrated busy host is reserved.
    store.interrupt_running()
    claimed = store.claim_next()
    assert claimed["id"] == "second"
    assert claimed["router_key"] == "router.example"
    assert claimed["snapshot"] == routing.serialize()
    assert store.get("first")["status"] == "failed"
    assert store.get("history")["status"] == "succeeded"
