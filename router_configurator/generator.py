from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .lists import RoutingList, meaningful_lines, normalize_entry, parse_mode, parse_routing_list

GEOSITE_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"
GEOIP_URL = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("Обрезанные protobuf-данные.")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 128:
            return result, pos
    raise ValueError("Некорректный protobuf varint.")


def fields(data: bytes):
    """Read the protobuf wire format used by v2ray-rules-dat (no protobuf dependency)."""
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Некорректное protobuf-поле.")
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire in {1, 2, 5}:
            if wire == 2:
                length, pos = read_varint(data, pos)
            else:
                length = 8 if wire == 1 else 4
            if pos + length > len(data):
                raise ValueError("Обрезанные protobuf-данные.")
            value = data[pos:pos + length]
            pos += length
        else:
            raise ValueError("Неподдерживаемый protobuf wire type.")
        yield number, wire, value


def parse_geosite_list(data: bytes, skipped: dict[str, int] | None = None) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    for number, wire, site in fields(data):
        if number != 1 or wire != 2:
            continue
        code = None
        domains = []
        omitted = 0
        for field, kind, value in fields(site):
            if field == 1 and kind == 2:
                code = value.decode("utf-8").upper()
            elif field == 2 and kind == 2:
                # GeoSite types: Plain=0, Regex=1, RootDomain=2, Full=3.
                # RCI address lists contain domain names, not V2Ray match expressions.
                domain_type, domain_name = 0, None
                for domain_field, domain_wire, domain_value in fields(value):
                    if domain_field == 1 and domain_wire == 0:
                        domain_type = domain_value
                    elif domain_field == 2 and domain_wire == 2:
                        domain_name = domain_value.decode("utf-8")
                if domain_name is None:
                    raise ValueError("Запись geosite без значения.")
                if domain_type in (2, 3):
                    domains.append(domain_name)
                elif domain_type in (0, 1):
                    omitted += 1
                else:
                    raise ValueError("Неизвестный тип записи geosite.")
        if not code:
            raise ValueError("Категория geosite без имени.")
        entries[code] = domains
        if skipped is not None and omitted:
            skipped[code] = omitted
    return entries


def parse_geoip_list(data: bytes) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    for number, wire, geoip in fields(data):
        if number != 1 or wire != 2:
            continue
        code = None
        networks = []
        for field, kind, value in fields(geoip):
            if field == 1 and kind == 2:
                code = value.decode("utf-8").upper()
            elif field == 2 and kind == 2:
                address = prefix = None
                for cidr_field, cidr_wire, cidr_value in fields(value):
                    if cidr_field == 1 and cidr_wire == 2:
                        address = ipaddress.ip_address(cidr_value)
                    elif cidr_field == 2 and cidr_wire == 0:
                        prefix = cidr_value
                if address is None or prefix is None:
                    raise ValueError("Неполная запись CIDR в geoip.")
                networks.append(str(ipaddress.ip_network(f"{address}/{prefix}", strict=False)))
        if not code:
            raise ValueError("Категория geoip без имени.")
        entries[code] = networks
    return entries


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # New files remain readable by a separate service user; retain existing permissions.
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_file(url: str, destination: Path) -> None:
    print(f"Скачиваю {destination.name}")
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    parser = parse_geosite_list if destination.name == "geosite.dat" else parse_geoip_list
    if not parser(data):
        raise ValueError("Скачанная база пуста.")
    atomic_write(destination, data)


def generate(preset: Path, output: Path, cache_dir: Path, offline: bool = False) -> RoutingList:
    lines = meaningful_lines(preset.read_text(encoding="utf-8"))
    if not lines:
        raise ValueError("Пресет пуст.")
    mode = parse_mode(lines[0])
    sources = []
    for line in lines[1:]:
        source, separator, name = line.partition(":")
        source, name = source.strip().lower(), name.strip()
        if not separator or source not in {"geosite", "geoip", "domain"} or not name:
            raise ValueError("Ожидаются непустые записи geosite:name, geoip:name или domain:value.")
        sources.append((source, name))
    if not sources:
        raise ValueError("В пресете нет записей.")
    databases = {}
    skipped: dict[str, int] = {}
    for source, filename, url, parser in (
        ("geosite", "geosite.dat", GEOSITE_URL, parse_geosite_list),
        ("geoip", "geoip.dat", GEOIP_URL, parse_geoip_list),
    ):
        if not any(kind == source for kind, _ in sources):
            continue
        path = cache_dir / filename
        if not offline:
            download_file(url, path)
        elif not path.is_file():
            raise ValueError(f"Для --offline отсутствует {filename} в каталоге кеша.")
        data = path.read_bytes()
        databases[source] = parse_geosite_list(data, skipped) if source == "geosite" else parser(data)
    entries = []
    for source, name in sources:
        if source == "domain":
            values = [name]
        else:
            values = databases[source].get(name.upper())
            if values is None:
                raise ValueError(f"Категория {source}:{name} не найдена.")
            if source == "geosite" and name.upper() in skipped:
                print(f"geosite:{name}: пропущено regex/keyword-правил: {skipped[name.upper()]}; TXT содержит только домены/IP.")
        for value in values:
            entry = normalize_entry(value)
            try:
                if ipaddress.ip_network(entry, strict=False).version == 6:
                    continue
            except ValueError:
                pass
            entries.append(entry)
    version = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    routing = parse_routing_list(RoutingList(mode, tuple(entries), version).serialize())
    atomic_write(output, routing.serialize().encode("utf-8"))
    return routing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Создать готовый TXT-лист для router-configurator.")
    parser.add_argument("--preset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        routing = generate(args.preset, args.output, args.cache_dir, args.offline)
    except (OSError, ValueError) as exc:
        print(f"Ошибка генерации: {exc}", file=sys.stderr)
        return 1
    print(f"Создан {args.output}: версия {routing.version}, режим {routing.mode}, записей {len(routing.entries)}.")
    return 0
