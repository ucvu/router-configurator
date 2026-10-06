import pytest
import requests

from router_configurator.config import Config


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tests must not contact real routers or download datasets.")
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "router.txt"
    source.write_text("mode: proxy\nexample.com\n203.0.113.0/24\n", encoding="utf-8")
    return Config("test-api-secret", "test-admin", "test-password-secret", source, tmp_path / "data")
