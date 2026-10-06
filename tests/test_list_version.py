from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from router_configurator import generator
from router_configurator.app import create_app
from router_configurator.lists import parse_routing_list
from router_configurator.storage import Store


@pytest.mark.parametrize("version", [
    "2026-10-06T10:30:15.123456Z", "2026-10-06T10:30:15Z",
    "2026-10-06T13:30:15.123456+03:00", "2024-02-29T00:00:00-05:00",
])
def test_version_header_round_trip_and_router_groups(version):
    content = f"\ufeff# comment\nversion: {version} # generation time\n\nmode: proxy\nEXAMPLE.COM\n203.0.113.0/24\nexample.com\n"
    routing = parse_routing_list(content)
    assert routing.version == version
    assert routing.serialize().splitlines()[:2] == [f"version: {version}", "mode: proxy"]
    assert parse_routing_list(routing.serialize()) == routing
    assert routing.groups() == {"geosite_domains_1": ("example.com",), "geoip_ips_1": ("203.0.113.0/24",)}


@pytest.mark.parametrize("version", [
    "", "latest", "2026-10-06", "2026-10-06T10:30:15", "2026-02-29T10:30:15Z",
    "2026-13-06T10:30:15Z", "2026-10-06T24:30:15Z", "2026-10-06T10:30:15+25:00",
])
def test_invalid_version_header(version):
    with pytest.raises(ValueError):
        parse_routing_list(f"version: {version}\nmode: proxy\nexample.com\n")


@pytest.mark.parametrize("content", [
    "version: 2026-10-06T10:30:15Z",
    "version: 2026-10-06T10:30:15Z\nmode: proxy",
    "version: 2026-10-06T10:30:15Z\nversion: 2026-10-06T10:30:15Z\nmode: proxy\nexample.com",
    "mode: proxy\nversion: 2026-10-06T10:30:15Z\nexample.com",
])
def test_incomplete_duplicate_or_misplaced_header(content):
    with pytest.raises(ValueError):
        parse_routing_list(content)


def test_legacy_format_has_no_version():
    content = "mode: direct\nexample.com\n"
    routing = parse_routing_list(content)
    assert routing.version is None
    assert routing.serialize() == content


def test_generated_version_is_current_utc_time_in_first_line(tmp_path):
    preset = tmp_path / "preset.txt"
    preset.write_text("mode: proxy\ndomain:example.com\n", encoding="utf-8")
    output = tmp_path / "router.txt"
    before = datetime.now(timezone.utc)
    routing = generator.generate(preset, output, tmp_path / "cache", offline=True)
    after = datetime.now(timezone.utc)
    assert before <= datetime.fromisoformat(routing.version.replace("Z", "+00:00")) <= after
    assert len(routing.version) == 27  # UTC timestamp with six fractional digits and Z.
    assert output.read_text(encoding="utf-8").splitlines()[0] == f"version: {routing.version}"


def test_regeneration_updates_api_but_preserves_queued_snapshot_and_old_version_on_failure(config, tmp_path, monkeypatch):
    instants = iter([datetime(2026, 10, 6, 10, 30, 15, 123456, tzinfo=timezone.utc),
                     datetime(2026, 10, 6, 10, 30, 15, 123457, tzinfo=timezone.utc)])

    class Clock:
        @staticmethod
        def now(tz):
            assert tz == timezone.utc
            return next(instants)

    monkeypatch.setattr(generator, "datetime", Clock)
    preset = tmp_path / "preset.txt"
    preset.write_text("mode: proxy\ndomain:example.com\n", encoding="utf-8")
    first = generator.generate(preset, config.list_path, tmp_path / "cache", offline=True)
    app = create_app(config, start_worker=False)
    with TestClient(app) as client:
        headers = {"X-API-Key": config.api_key}
        response = client.get("/api/v1/list/version", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"version": first.version}
        assert response.headers["Cache-Control"] == "no-store"
        assert app.state.store.claim_next() is None
        accepted = client.post("/api/v1/router-configurations",
                               json={"hostname": "router.example", "action": "update"}, headers=headers)
        assert accepted.status_code == 202
        second = generator.generate(preset, config.list_path, tmp_path / "cache", offline=True)
        assert second.entries == first.entries
        assert second.version != first.version
        assert client.get("/api/v1/list/version", headers=headers).json() == {"version": second.version}
        preset.write_text("mode: invalid\ndomain:example.com\n", encoding="utf-8")
        with pytest.raises(ValueError):
            generator.generate(preset, config.list_path, tmp_path / "cache", offline=True)
        assert client.get("/api/v1/list/version", headers=headers).json() == {"version": second.version}
    reopened = Store(app.state.store.path)
    reopened.initialize()
    job = reopened.claim_next()
    assert job["id"] == accepted.json()["job_id"]
    assert parse_routing_list(job["snapshot"]) == first


def test_version_endpoint_legacy_and_auth_before_file_access(config):
    with TestClient(create_app(config, start_worker=False)) as client:
        response = client.get("/api/v1/list/version", headers={"X-API-Key": config.api_key})
        assert response.status_code == 200
        assert response.json() == {"version": None}
        config.list_path.unlink()
        for headers in ({}, {"X-API-Key": "wrong"}):
            assert client.get("/api/v1/list/version", headers=headers).status_code == 401


@pytest.mark.parametrize("content", [
    None, b"\xff", b"", b"mode: proxy\n", b"mode: invalid\nexample.com\n",
    b"version: invalid\nmode: proxy\nexample.com\n",
    b"version: 2026-10-06T10:30:15Z\nmode: proxy\nbad/value\n",
])
def test_version_endpoint_rejects_unavailable_or_invalid_list(config, content):
    if content is None:
        config.list_path.unlink()
    else:
        config.list_path.write_bytes(content)
    app = create_app(config, start_worker=False)
    with TestClient(app) as client:
        headers = {"X-API-Key": config.api_key}
        assert client.get("/api/v1/list/version", headers=headers).status_code == 503
        assert client.post("/api/v1/router-configurations",
                           json={"hostname": "router.example", "action": "update"}, headers=headers).status_code == 503
        assert app.state.store.claim_next() is None
