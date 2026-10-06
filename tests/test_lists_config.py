import pytest

from router_configurator.config import Config
from router_configurator.lists import normalize_hostname, parse_routing_list, router_key


@pytest.mark.parametrize(("value", "expected"), [
    ("192.168.1.1", "http://192.168.1.1"),
    ("192.168.1.1:8080", "http://192.168.1.1:8080"),
    ("Router.Example.com", "https://router.example.com"),
    ("router.example.com:8443", "https://router.example.com:8443"),
    ("https://router.example.com:443/", "https://router.example.com:443"),
    ("http://router.example.com", "http://router.example.com"),
    ("2001:db8::1", "http://[2001:db8::1]"),
    ("[2001:db8::1]:8080", "http://[2001:db8::1]:8080"),
])
def test_hostname(value, expected):
    assert normalize_hostname(value) == expected


@pytest.mark.parametrize("value", [
    "", "http://", "ftp://router.example", "http://user:pass@router.example",
    "http://router.example/path", "http://router.example?", "http://router.example#",
    "http://router.example:0", "http://router.example:65536", "http://router.example:",
    "router example.com", "999.999.999.999", "-bad.example", "https://router.example\\evil",
])
def test_bad_hostname(value):
    with pytest.raises(ValueError):
        normalize_hostname(value)


def test_routing_deduplicates_and_groups():
    content = "\ufeff# comment\nmode: direct\nEXAMPLE.COM\nexample.com # duplicate\n203.0.113.5/24\n203.0.113.0/24\n"
    routing = parse_routing_list(content)
    assert routing.mode == "direct"
    assert routing.entries == ("example.com", "203.0.113.0/24")
    assert routing.groups() == {"geosite_domains_1": ("example.com",), "geoip_ips_1": ("203.0.113.0/24",)}


def test_large_groups():
    routing = parse_routing_list("mode: proxy\n" + "\n".join(f"d{n}.example.com" for n in range(600)))
    assert [len(group) for group in routing.groups().values()] == [299, 299, 2]


@pytest.mark.parametrize("content", ["", "mode: proxy\n# nothing", "example.com", "mode: wrong\nexample.com", "mode: direct\nhttps://example.com", "mode: direct\nmode: proxy", "mode: proxy\n203.0.113.0/99"])
def test_bad_routing(content):
    with pytest.raises(ValueError):
        parse_routing_list(content)


def test_config_relative_paths_and_environment(tmp_path, monkeypatch):
    for name in ("API_KEY", "ROUTER_USERNAME", "ROUTER_PASSWORD", "LIST_PATH", "DATA_DIR", "HOST", "PORT", "MAX_PARALLEL_JOBS"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config" / ".env"
    path.parent.mkdir()
    path.write_text("API_KEY=key\nROUTER_USERNAME=user\nROUTER_PASSWORD='literal${NOT_EXPANDED}'\nLIST_PATH=lists/list.txt\n", encoding="utf-8")
    monkeypatch.setenv("PORT", "9000")
    config = Config.load(path)
    assert config.list_path == path.parent / "lists" / "list.txt"
    assert config.data_dir == path.parent / ".data"
    assert config.port == 9000
    assert config.host == "127.0.0.1"
    assert config.max_parallel_jobs == 4
    assert config.router_password == "literal${NOT_EXPANDED}"
    assert "literal" not in repr(config)


def test_missing_config_does_not_echo_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "a-secret")
    monkeypatch.delenv("ROUTER_USERNAME", raising=False)
    monkeypatch.delenv("ROUTER_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="ROUTER_USERNAME"):
        Config.load(tmp_path / "missing.env")


def test_redaction(config):
    message = f"{config.api_key} {config.router_username} {config.router_password}"
    assert config.redact(message) == "[REDACTED] [REDACTED] [REDACTED]"


@pytest.mark.parametrize("value", ["0", "33", "-1", "four", "1.5"])
def test_invalid_parallel_limit(tmp_path, monkeypatch, value):
    for name in ("API_KEY", "ROUTER_USERNAME", "ROUTER_PASSWORD"):
        monkeypatch.setenv(name, "test-secret")
    monkeypatch.setenv("MAX_PARALLEL_JOBS", value)
    with pytest.raises(ValueError, match="MAX_PARALLEL_JOBS"):
        Config.load(tmp_path / "missing.env")


def test_parallel_limit_from_environment_overrides_config(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("API_KEY=test\nROUTER_USERNAME=test\nROUTER_PASSWORD=test\nMAX_PARALLEL_JOBS=2\n", encoding="utf-8")
    monkeypatch.delenv("MAX_PARALLEL_JOBS", raising=False)
    assert Config.load(path).max_parallel_jobs == 2
    monkeypatch.setenv("MAX_PARALLEL_JOBS", "8")
    assert Config.load(path).max_parallel_jobs == 8


@pytest.mark.parametrize("hostname", ["router.example", "ROUTER.EXAMPLE.", "http://router.example", "https://router.example:8443/"])
def test_router_key_ignores_spelling_scheme_and_port(hostname):
    assert router_key(hostname) == "router.example"
