import io
import logging
import time

import pytest
from fastapi.testclient import TestClient

from router_configurator.app import create_app
from router_configurator.logging import configure_logging
from router_configurator.router import RouterError


def test_application_logging_goes_to_stdout_and_redacts_formatted_values(config, monkeypatch):
    logger = logging.getLogger("router_configurator")
    for attribute in ("handlers", "level", "propagate"):
        monkeypatch.setattr(logger, attribute, getattr(logger, attribute))
    stream = io.StringIO()
    monkeypatch.setattr("sys.stdout", stream)
    configure_logging(config)
    child = logging.getLogger("router_configurator.jobs")
    child.info("job_id=example hostname=router.example | %s %s %s",
               config.api_key, config.router_username, config.router_password)
    child.error("Ошибка выполнения задачи.")
    output = stream.getvalue()
    assert "INFO router_configurator.jobs job_id=example hostname=router.example" in output
    assert "ERROR router_configurator.jobs Ошибка выполнения задачи." in output
    assert output.count("[REDACTED]") == 3
    assert all(secret not in output for secret in (config.api_key, config.router_username, config.router_password))


@pytest.mark.parametrize("failure", [None, "rci", "unexpected"])
def test_worker_logs_context_progress_and_final_status_without_secrets(config, caplog, failure):
    class Client:
        def __init__(self, hostname, username, password, emit):
            self.emit = emit

        def update(self, routing):
            self.emit("Подключение к RCI API роутера.")
            self.emit("Этап " + config.api_key + " " + config.router_username + " " + config.router_password)
            if failure == "rci":
                raise RouterError("Ошибка авторизации " + config.router_password)
            if failure == "unexpected":
                raise RuntimeError("private-unexpected-detail " + config.router_password)

        def close(self):
            pass

    caplog.set_level(logging.INFO, logger="router_configurator")
    app = create_app(config, client_factory=Client)
    # Let lifespan shutdown finish the accepted job before inspecting its logs.
    with TestClient(app) as client:
        response = client.post("/api/v1/router-configurations",
                               json={"hostname": "router.example", "action": "update"},
                               headers={"X-API-Key": config.api_key})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 5
        while app.state.store.get(job_id)["status"] in {"queued", "running"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    status = "failed" if failure else "succeeded"
    job_messages = [record.getMessage() for record in caplog.records if f"job_id={job_id}" in record.getMessage()]
    assert job_messages
    assert all("hostname=router.example action=update" in message for message in job_messages)
    assert any("status=queued" in message for message in job_messages)
    assert any("status=running" in message and "Подключение к RCI" in message for message in job_messages)
    assert any(f"status={status}" in message for message in job_messages)
    assert app.state.store.get(job_id)["status"] == status
    assert "private-unexpected-detail" not in caplog.text
    assert all(secret not in caplog.text for secret in (config.api_key, config.router_username, config.router_password))
    assert all(record.exc_info is None for record in caplog.records)


def test_http_logs_statuses_without_headers_bodies_query_or_raw_paths(config, caplog):
    caplog.set_level(logging.INFO, logger="router_configurator")
    submitted_secret = "unrecognized-submitted-secret"
    with TestClient(create_app(config, start_worker=False)) as client:
        headers = {"X-API-Key": config.api_key}
        body = {"hostname": "router.example", "action": "update"}
        assert client.post("/api/v1/router-configurations", json=body,
                           headers={"X-API-Key": submitted_secret}).status_code == 401
        assert client.post(f"/api/v1/router-configurations?token={submitted_secret}",
                           json=body, headers=headers).status_code == 202
        assert client.get(f"/api/v1/jobs/{submitted_secret}", headers=headers).status_code == 404
        assert client.get(f"/{submitted_secret}").status_code == 404
        assert client.post("/api/v1/router-configurations",
                           json={**body, "password": submitted_secret}, headers=headers).status_code == 422
        config.list_path.write_text("mode: invalid\n", encoding="utf-8")
        assert client.post("/api/v1/router-configurations", json=body, headers=headers).status_code == 503
    messages = [record.getMessage() for record in caplog.records if record.name == "router_configurator.app"]
    for status in (202, 401, 404, 422, 503):
        assert any(f"status={status} " in message for message in messages)
    assert any("HTTP GET /api/v1/jobs/{job_id}" in message for message in messages)
    assert any("HTTP GET <unknown>" in message for message in messages)
    assert "duration_ms=" in caplog.text
    assert "client=testclient" in caplog.text
    assert submitted_secret not in caplog.text
    assert config.api_key not in caplog.text
