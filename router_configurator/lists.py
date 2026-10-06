from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

Mode = Literal["proxy", "direct"]
MAX_LIST_ENTRIES = 299
_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\Z")
_VERSION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")


def normalize_domain(value: str) -> str:
    try:
        domain = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Некорректное доменное имя.") from None
    if not domain or len(domain) > 253 or not all(_LABEL.fullmatch(label) for label in domain.split(".")):
        raise ValueError("Некорректное доменное имя.")
    # A malformed numeric IP must not silently become a domain.
    if re.fullmatch(r"[0-9.]+", domain):
        raise ValueError("Некорректный IP-адрес.")
    return domain


def normalize_entry(value: str) -> str:
    try:
        if "/" in value:
            return str(ipaddress.ip_network(value, strict=False))
        return str(ipaddress.ip_address(value))
    except ValueError:
        if "/" in value or ":" in value:
            raise ValueError("Некорректный IP/CIDR.") from None
        return normalize_domain(value)


def normalize_hostname(value: str) -> str:
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Укажите IP, домен или базовый HTTP(S) URL роутера.")
    if "://" in value:
        try:
            parsed = urlsplit(value)
            port = parsed.port
            host = parsed.hostname
        except ValueError:
            raise ValueError("Некорректный URL роутера.") from None
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.path not in {"", "/"} or parsed.username is not None
                or parsed.password is not None or "?" in value or "#" in value):
            raise ValueError("Разрешён только базовый URL http(s)://host[:port].")
        if parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
            raise ValueError("Некорректный порт роутера.")
        scheme = parsed.scheme
    else:
        # Bare IPv6 is an address, not host:port. Ports on IPv6 require brackets.
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return normalize_hostname(f"https://{value}") if ":" not in value else _hostname_with_port(value)
        host, port, scheme = str(address), None, "http"
    try:
        address = ipaddress.ip_address(host)
        if "%" in host:
            raise ValueError("IPv6 zone identifiers are not supported.")
        normalized = f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        normalized = normalize_domain(host)
    return f"{scheme}://{normalized}" + (f":{port}" if port is not None else "")


def _hostname_with_port(value: str) -> str:
    try:
        host = urlsplit(f"//{value}").hostname
        if not host:
            raise ValueError("Некорректный адрес роутера.")
        try:
            ipaddress.ip_address(host)
            scheme = "http"
        except ValueError:
            scheme = "https"
        return normalize_hostname(f"{scheme}://{value}")
    except ValueError:
        raise ValueError("Некорректный адрес роутера.") from None


def router_key(hostname: str) -> str:
    """Serialize updates to the same normalized host, even across schemes/ports."""
    return urlsplit(normalize_hostname(hostname)).hostname


@dataclass(frozen=True)
class RoutingList:
    mode: Mode
    entries: tuple[str, ...]
    version: str | None = None

    def serialize(self) -> str:
        header = f"version: {self.version}\n" if self.version is not None else ""
        return header + f"mode: {self.mode}\n" + "\n".join(self.entries) + "\n"

    def groups(self) -> dict[str, tuple[str, ...]]:
        domains: list[str] = []
        ips: list[str] = []
        for entry in self.entries:
            try:
                ipaddress.ip_network(entry, strict=False)
                ips.append(entry)
            except ValueError:
                domains.append(entry)
        groups = {}
        for prefix, entries in (("geosite_domains", domains), ("geoip_ips", ips)):
            for start in range(0, len(entries), MAX_LIST_ENTRIES):
                groups[f"{prefix}_{start // MAX_LIST_ENTRIES + 1}"] = tuple(entries[start:start + MAX_LIST_ENTRIES])
        return groups


def meaningful_lines(content: str) -> list[str]:
    return [line for raw in content.lstrip("\ufeff").splitlines() if (line := raw.split("#", 1)[0].strip())]


def parse_mode(line: str) -> Mode:
    key, separator, value = line.partition(":")
    if key.strip().lower() != "mode" or not separator or value.strip().lower() not in {"proxy", "direct"}:
        raise ValueError("Ожидается строка mode: proxy или mode: direct.")
    return value.strip().lower()


def parse_routing_list(content: str) -> RoutingList:
    lines = meaningful_lines(content)
    if not lines:
        raise ValueError("Лист пуст.")
    version = None
    key, separator, value = lines[0].partition(":")
    if key.strip().lower() == "version":
        version = value.strip()
        if not separator or not _VERSION.fullmatch(version):
            raise ValueError("Версия должна содержать дату и время ISO 8601 с часовым поясом.")
        try:
            datetime.fromisoformat(version.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Некорректная дата или время версии листа.") from None
        lines = lines[1:]
        if not lines:
            raise ValueError("В листе отсутствует режим.")
    mode = parse_mode(lines[0])
    entries = tuple(dict.fromkeys(normalize_entry(line) for line in lines[1:]))
    if not entries:
        raise ValueError("В листе нет ни одной записи.")
    return RoutingList(mode, entries, version)
