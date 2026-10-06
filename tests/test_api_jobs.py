import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from router_configurator.app import create_app
from router_configurator.jobs import JobWorker
from router_configurator.lists import parse_routing_list
from router_configurator.router import RouterError
from router_configurator.storage import Store


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Worker did not reach the expected state.")


def test_auth_and_missing_job(config):
    with TestClient(create_app(config, start_worker=False)) as client:
        for headers in ({}, {"X-API-Key": "wrong"}):
            assert client.post("/api/v1/router-configurations", json={"hostname": "192.168.1.1", "action": "update"}, headers=headers).status_code == 401
            assert client.get("/api/v1/jobs/missing", headers=headers).status_code == 401
        assert client.get("/api/v1/jobs/missing", headers={"X-API-Key": config.api_key}).status_code == 404


@pytest.mark.parametrize("body", [
    {"hostname": "192.168.1.1", "action": "install"},
    {"hostname": "192.168.1.1"},
    {"hostname": "", "action": "update"},
    {"hostname": 123, "action": "update"},
    {"hostname": "http://user:password@host.example/path", "action": "update"},
    {"hostname": "host.example", "action": "update", "list_path": "other.txt"},
])
def test_validation(config, body):
    with TestClient(create_app(config, start_worker=False)) as client:
        assert client.post("/api/v1/router-configurations", json=body, headers={"X-API-Key": config.api_key}).status_code == 422


@pytest.mark.parametrize("content", [None, "mode: proxy\n", "mode: invalid\nexample.com", "mode: proxy\nbad/value", b"\xff"])
def test_bad_list_never_enqueues(config, content):
    if content is None:
        config.list_path.unlink()
    elif isinstance(content, bytes):
        config.list_path.write_bytes(content)
    else:
        config.list_path.write_text(content, encoding="utf-8")
    app = create_app(config, start_worker=False)
    with TestClient(app) as client:
        result = client.post("/api/v1/router-configurations", json={"hostname": "router.example", "action": "update"}, headers={"X-API-Key": config.api_key})
        assert result.status_code == 503
        assert app.state.store.claim_next() is None


def test_accepted_job_and_snapshot(config):
    app = create_app(config, start_worker=False)
    with TestClient(app) as client:
        headers = {"X-API-Key": config.api_key}
        result = client.post("/api/v1/router-configurations", json={"hostname": "router.example", "action": "update"}, headers=headers)
        assert result.status_code == 202
        accepted = result.json()
        assert accepted["status"] == "queued"
        status = client.get(accepted["status_url"], headers=headers).json()
        assert status["hostname"] == "router.example"
        assert status["started_at"] is None
        assert "snapshot" not in status
        config.list_path.write_text("mode: direct\nchanged.example\n", encoding="utf-8")
        snapshot = parse_routing_list(app.state.store.claim_next()["snapshot"])
        assert snapshot.mode == "proxy"
        assert snapshot.entries == ("example.com", "203.0.113.0/24")


def test_api_worker_success_and_secret_redaction(config):
    class Client:
        def __init__(self, hostname, username, password, emit):
            self.emit = emit
            assert (username, password) == (config.router_username, config.router_password)
        def update(self, routing):
            self.emit(config.api_key + " " + config.router_password)
        def close(self):
            pass
    with TestClient(create_app(config, client_factory=Client)) as client:
        headers = {"X-API-Key": config.api_key}
        response = client.post("/api/v1/router-configurations", json={"hostname": "router.example", "action": "update"}, headers=headers)
        url = response.json()["status_url"]
        def completed():
            data = client.get(url, headers=headers).json()
            return data if data["status"] == "succeeded" else None
        status = wait_for(completed)
        assert status["created_at"] <= status["started_at"] <= status["finished_at"]
        assert status["error"] is None
        assert config.api_key not in str(status)
        assert config.router_password not in str(status)


def test_worker_failure_is_persisted(config):
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        def update(self, routing):
            raise RouterError("Ошибка " + config.router_password)
        def close(self):
            pass
    store = Store(config.data_dir / "jobs.sqlite3")
    worker = JobWorker(config, store, Client)
    worker.start()
    try:
        job_id = store.enqueue("router.example", parse_routing_list(config.list_path.read_text()))
        worker.notify()
        wait_for(lambda: store.get(job_id)["status"] == "failed")
        assert store.get(job_id)["error"] == "Ошибка [REDACTED]"
    finally:
        worker.stop()


def test_restart_recovers_queued_and_interrupts_running(config):
    store = Store(config.data_dir / "jobs.sqlite3")
    store.initialize()
    routing = parse_routing_list(config.list_path.read_text())
    interrupted = store.enqueue("first.example", routing)
    queued = store.enqueue("second.example", routing)
    assert store.claim_next()["id"] == interrupted
    config.list_path.write_text("mode: direct\nnew.example\n", encoding="utf-8")
    seen = []
    class Client:
        def __init__(self, hostname, *args, **kwargs):
            self.hostname = hostname
        def update(self, snapshot):
            seen.append((self.hostname, snapshot))
        def close(self):
            pass
    restarted_store = Store(store.path)
    worker = JobWorker(config, restarted_store, Client)
    worker.start()
    try:
        wait_for(lambda: restarted_store.get(queued)["status"] == "succeeded")
        assert restarted_store.get(interrupted)["status"] == "failed"
        assert "прервано" in restarted_store.get(interrupted)["error"]
        assert seen == [("second.example", routing)]
    finally:
        worker.stop()


def test_worker_is_sequential_and_stops_before_next_job(config):
    config = replace(config, max_parallel_jobs=1)
    store = Store(config.data_dir / "jobs.sqlite3")
    entered, release = threading.Event(), threading.Event()
    seen = []
    class Client:
        def __init__(self, hostname, *args, **kwargs):
            self.hostname = hostname
        def update(self, routing):
            seen.append(self.hostname)
            entered.set()
            assert release.wait(5)
        def close(self):
            pass
    worker = JobWorker(config, store, Client)
    worker.start()
    try:
        routing = parse_routing_list(config.list_path.read_text())
        first = store.enqueue("first.example", routing)
        second = store.enqueue("second.example", routing)
        worker.notify()
        assert entered.wait(5)
        assert store.get(first)["status"] == "running"
        assert store.get(second)["status"] == "queued"
        worker.stop(timeout=0)
        release.set()
        worker.stop()
        assert store.get(first)["status"] == "succeeded"
        assert store.get(second)["status"] == "queued"
        assert seen == ["first.example"]
    finally:
        release.set()
        worker.stop()


def test_second_worker_cannot_interrupt_first(config):
    store = Store(config.data_dir / "jobs.sqlite3")
    first = JobWorker(config, store)
    second = JobWorker(config, store)
    first.start()
    try:
        with pytest.raises(RuntimeError, match="Другой процесс"):
            second.start()
    finally:
        first.stop()
    second.start()
    second.stop()
