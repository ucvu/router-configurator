import io
import ipaddress

import pytest

from router_configurator import generator


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def datasets():
    site = field(1, b"TEST") + field(2, b"\x08\x02" + field(2, b"example.com")) + field(2, b"\x08\x03" + field(2, b"second.example"))
    ipv4 = field(1, ipaddress.ip_address("203.0.113.0").packed) + b"\x10\x18"
    ipv6 = field(1, ipaddress.ip_address("2001:db8::").packed) + b"\x10\x20"
    geoip = field(1, b"TEST") + field(2, ipv4) + field(2, ipv6)
    return {"geosite.dat": field(1, site), "geoip.dat": field(1, geoip)}


@pytest.mark.parametrize("offline", [True, False])
@pytest.mark.parametrize("mode", ["direct", "proxy"])
def test_generation(tmp_path, monkeypatch, offline, mode):
    data = datasets()
    cache = tmp_path / "cache"
    cache.mkdir()
    for name, content in data.items():
        (cache / name).write_bytes(content)
    downloads = []

    def urlopen(url, timeout):
        downloads.append(url)
        assert timeout == 60
        return io.BytesIO(data[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(generator.urllib.request, "urlopen", urlopen)
    preset = tmp_path / "preset.txt"
    preset.write_text(f"mode: {mode}\ngeosite:test\ngeoip:test\ndomain:example.com\ndomain:198.51.100.2\ndomain:2001:db8::1\n", encoding="utf-8")
    output = tmp_path / "lists" / "router.txt"
    routing = generator.generate(preset, output, cache, offline)
    assert routing.entries == ("example.com", "second.example", "203.0.113.0/24", "198.51.100.2")
    assert output.read_text(encoding="utf-8") == routing.serialize()
    assert len(downloads) == (0 if offline else 2)


def test_manual_preset_needs_no_databases(tmp_path):
    preset = tmp_path / "preset.txt"
    preset.write_text("mode: proxy\ndomain:example.com\n", encoding="utf-8")
    routing = generator.generate(preset, tmp_path / "out.txt", tmp_path / "missing", offline=True)
    assert routing.entries == ("example.com",)


def test_regex_and_keyword_are_not_exported(tmp_path, capsys):
    cache = tmp_path / "cache"
    cache.mkdir()
    site = (field(1, b"TEST") + field(2, b"\x08\x02" + field(2, b"example.com"))
            + field(2, b"\x08\x01" + field(2, rb"^host-\d+\.example\.com$"))
            + field(2, field(2, b"keyword")))
    (cache / "geosite.dat").write_bytes(field(1, site))
    preset = tmp_path / "preset.txt"
    preset.write_text("mode: proxy\ngeosite:test\n", encoding="utf-8")
    result = generator.generate(preset, tmp_path / "out.txt", cache, offline=True)
    assert result.entries == ("example.com",)
    assert "regex/keyword" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["mode: proxy\ngeosite:missing", "mode: proxy\nbad:entry", "mode: proxy", "mode: wrong\ndomain:example.com", "mode: proxy\ndomain:2001:db8::1"])
def test_failed_generation_keeps_old_output(tmp_path, content):
    preset = tmp_path / "preset.txt"
    preset.write_text(content, encoding="utf-8")
    output = tmp_path / "out.txt"
    output.write_text("old-list", encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    for name, data in datasets().items():
        (cache / name).write_bytes(data)
    with pytest.raises(ValueError):
        generator.generate(preset, output, cache, offline=True)
    assert output.read_text() == "old-list"


def test_failed_replace_keeps_old_file(tmp_path, monkeypatch):
    output = tmp_path / "out.txt"
    output.write_bytes(b"old")
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(generator.os, "replace", fail)
    with pytest.raises(OSError):
        generator.atomic_write(output, b"new")
    assert output.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [output]


def test_download_error_keeps_output(tmp_path, monkeypatch):
    preset = tmp_path / "preset.txt"
    preset.write_text("mode: proxy\ngeosite:test", encoding="utf-8")
    output = tmp_path / "out.txt"
    output.write_bytes(b"old")
    def fail(*args, **kwargs):
        raise OSError("download failed")
    monkeypatch.setattr(generator.urllib.request, "urlopen", fail)
    with pytest.raises(OSError):
        generator.generate(preset, output, tmp_path / "cache")
    assert output.read_bytes() == b"old"


@pytest.mark.parametrize("data", [b"\x00", b"\x0a\xff", b"\x0a\x05a", b"\x0e", b"\xff" * 20])
def test_invalid_protobuf(data):
    with pytest.raises(ValueError):
        generator.parse_geosite_list(data)


def test_cli_failure_exit_code(tmp_path):
    assert generator.main(["--preset", str(tmp_path / "missing.txt"), "--output", str(tmp_path / "out.txt")]) == 1
