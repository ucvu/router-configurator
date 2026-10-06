from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

import requests

from .lists import RoutingList, normalize_hostname

HTTP_TIMEOUT = (15, 60)
VPN_DOWN_DELAY = 10
VPN_DOWN_TIMEOUT = 300
RECONNECT_TIMEOUT = 180
ACTION_DELAY = 1
VERIFY_ATTEMPTS = 3


class RouterError(RuntimeError):
    """A router error with a safe message suitable for the job log."""


class RouterConnectionError(RouterError):
    pass


class RouterClient:
    def __init__(
        self, hostname: str, username: str, password: str,
        emit: Callable[[str], None] = lambda message: None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = normalize_hostname(hostname)
        self._username = username
        self._password = password
        self.session = session if session is not None else requests.Session()
        self.emit = emit
        self.sleep = sleep

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, **kwargs):
        try:
            # Never follow redirects with router credentials/RCI commands to another host.
            response = self.session.request(
                method, self.base_url + path, timeout=HTTP_TIMEOUT, allow_redirects=False, **kwargs,
            )
        except requests.RequestException:
            raise RouterConnectionError("Роутер недоступен или истекло время ожидания ответа.") from None
        return response

    @staticmethod
    def _check_response(response) -> None:
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise RouterConnectionError(f"RCI API временно недоступен: HTTP {response.status_code}.")
        if not 200 <= response.status_code < 300:
            raise RouterError(f"RCI API вернул HTTP {response.status_code}.")

    def login(self) -> None:
        self.emit("Подключение к RCI API роутера.")
        response = self._request("GET", "/auth")
        if response.status_code == 200:
            self.emit("Авторизация на роутере успешна.")
            return
        if response.status_code != 401:
            self._check_response(response)
            raise RouterError(f"Авторизация роутера: HTTP {response.status_code}.")
        realm = response.headers.get("X-NDM-Realm")
        challenge = response.headers.get("X-NDM-Challenge")
        if realm is None or challenge is None:
            raise RouterError("Роутер не вернул параметры RCI-авторизации.")
        digest = hashlib.md5(f"{self._username}:{realm}:{self._password}".encode()).hexdigest()
        password_hash = hashlib.sha256((challenge + digest).encode()).hexdigest()
        response = self._request("POST", "/auth", json={"login": self._username, "password": password_hash})
        if not 200 <= response.status_code < 300:
            raise RouterError(f"Не удалось авторизоваться на роутере: HTTP {response.status_code}.")
        self.emit("Авторизация на роутере успешна.")

    def rci(self, commands: list[dict]) -> list[dict]:
        response = self._request("POST", "/rci/", json=commands)
        self._check_response(response)
        try:
            result = response.json()
        except ValueError:
            raise RouterError("RCI API вернул некорректный JSON.") from None
        if not isinstance(result, list) or not result or not all(isinstance(item, dict) for item in result):
            raise RouterError("RCI API вернул неожиданный формат ответа.")

        def has_error(item) -> bool:
            if isinstance(item, dict):
                return bool(item.get("error")) or item.get("status") == "error" or any(
                    has_error(value) for value in item.values() if isinstance(value, (dict, list))
                )
            if isinstance(item, list):
                return any(has_error(value) for value in item)
            return False

        if has_error(result):
            # Response bodies can contain configuration secrets; do not persist them.
            raise RouterError("Роутер отклонил команду RCI.")
        return result

    def _show(self, command: dict, *keys: str):
        result = self.rci([command])[0]
        try:
            for key in keys:
                result = result[key]
        except (KeyError, TypeError):
            raise RouterError("В ответе RCI отсутствуют ожидаемые данные.") from None
        return result

    def interfaces(self) -> dict:
        result = self._show({"show": {"interface": {}}}, "show", "interface")
        if not isinstance(result, dict):
            raise RouterError("Некорректный список интерфейсов RCI.")
        return result

    def domain_lists(self) -> dict[str, str]:
        data = self._show(
            {"show": {"sc": {"object-group": {"fqdn": {}}}}},
            "show", "sc", "object-group", "fqdn",
        ) or {}
        if not isinstance(data, dict):
            raise RouterError("Некорректный список групп RCI.")
        return {group: info.get("description", "") for group, info in data.items()}

    def routes(self) -> list[dict]:
        data = self._show({"show": {"sc": {"dns-proxy": {"route": {}}}}}, "show", "sc", "dns-proxy", "route")
        if data is None or data == {}:
            return []
        if not isinstance(data, list):
            raise RouterError("Некорректный список DNS-маршрутов RCI.")
        return data

    def reconnect(self, timeout: int = RECONNECT_TIMEOUT) -> None:
        self.emit(f"Восстановление связи с роутером; timeout={timeout}s.")
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.login()
                self.interfaces()
                self.emit("Связь с роутером восстановлена.")
                return
            except RouterConnectionError:
                if time.monotonic() >= deadline:
                    raise RouterConnectionError("Роутер не восстановил связь за отведённое время.") from None
                self.sleep(3)

    def set_interface_up(self, name: str, up: bool) -> None:
        self.rci([
            {"interface": {"name": name, "up": up}},
            {"system": {"configuration": {"save": {}}}},
        ])

    def disable_vpn_if_up(self, name: str) -> None:
        for attempt in range(4):
            info = self.interfaces().get(name)
            if info is None or info.get("state") != "up":
                return
            if attempt == 3:
                raise RouterError("Не удалось выключить WireGuard-подключение.")
            self.emit(f"Отключаю {name}; ожидаю восстановления связи.")
            try:
                self.set_interface_up(name, False)
            except RouterConnectionError:
                self.emit("Связь оборвалась при отключении VPN; проверяю состояние повторно.")
            self.sleep(VPN_DOWN_DELAY)
            self.reconnect(VPN_DOWN_TIMEOUT)

    def enable_vpn(self, name: str) -> None:
        self.emit(f"Включаю {name}.")
        try:
            self.set_interface_up(name, True)
        except RouterConnectionError:
            # A disconnect alone is not sufficient evidence of success.
            self.emit("Связь оборвалась при включении VPN; проверяю применение команды.")
            self.reconnect(VPN_DOWN_TIMEOUT)
        deadline = time.monotonic() + RECONNECT_TIMEOUT
        while True:
            try:
                info = self.interfaces().get(name)
            except RouterConnectionError:
                self.reconnect(VPN_DOWN_TIMEOUT)
                info = self.interfaces().get(name)
            if info is not None and info.get("state") == "up":
                return
            if info is None or time.monotonic() >= deadline:
                raise RouterError("Не удалось подтвердить включение WireGuard-подключения.")
            self.sleep(3)

    def move_wan(self, name: str, bottom: bool) -> None:
        interfaces = self._show({"show": {"sc": {"interface": {}}}}, "show", "sc", "interface")
        settings = {
            name: info["ip"]["global"] for name, info in interfaces.items()
            if isinstance(info.get("ip", {}).get("global"), dict) and "order" in info["ip"]["global"]
        }
        if name not in settings:
            return
        others = sorted((item for item in settings if item != name), key=lambda item: settings[item]["order"])
        new_order = others + [name] if bottom else [name] + others
        if all(settings[item]["order"] == position for position, item in enumerate(new_order)):
            return
        self.rci([
            {"interface": {"name": item, "ip": {"global": {
                "enabled": settings[item].get("enabled", True), "order": position,
            }}}} for position, item in enumerate(new_order)
        ] + [{"system": {"configuration": {"save": {}}}}])

    def delete_all_lists_and_routes(self) -> None:
        for attempt in range(3):
            try:
                indexes = [route["index"] for route in self.routes() if "index" in route]
                if indexes:
                    self.rci([
                        {"dns-proxy": {"route": [{"index": index, "no": True} for index in indexes]}},
                        {"system": {"configuration": {"save": {}}}},
                    ])
                groups = self.domain_lists()
                if groups:
                    self.rci([
                        {"object-group": {"fqdn": {group: {"no": True} for group in groups}}},
                        {"system": {"configuration": {"save": {}}}},
                    ])
                return
            except RouterConnectionError:
                if attempt == 2:
                    raise
                self.emit("Повторяю удаление после восстановления связи.")
                self.reconnect()

    def create_list(self, name: str, entries: tuple[str, ...]) -> str:
        existing = self.domain_lists()
        number = 0
        while f"domain-list{number}" in existing:
            number += 1
        group = f"domain-list{number}"
        self.rci([{"object-group": {"fqdn": {group: {
            "description": name, "include": [{"address": entry} for entry in entries],
        }}}}])
        return group

    def create_route(self, group: str, interface: str) -> None:
        self.rci([
            {"dns-proxy": {"route": {
                "group": group, "gateway": "", "auto": True, "reject": False,
                "interface": interface, "disable": False,
            }}},
            {"system": {"configuration": {"save": {}}}},
        ])

    def ensure_lists_and_routes(self, groups: dict[str, tuple[str, ...]], interface: str) -> None:
        for attempt in range(VERIFY_ATTEMPTS + 1):
            try:
                names = {name: group for group, name in self.domain_lists().items()}
                routes = {route.get("group"): route for route in self.routes()}
                pending = [name for name in groups if name not in names
                           or routes.get(names[name], {}).get("interface") != interface
                           or routes.get(names[name], {}).get("disable", False)]
                if not pending:
                    return
                if attempt == VERIFY_ATTEMPTS:
                    break
                self.emit(f"Создание списков и маршрутов: попытка {attempt + 1}/{VERIFY_ATTEMPTS}.")
                for name in pending:
                    group = names.get(name)
                    if group is None:
                        self.emit(f"Создаю список {name}: entries={len(groups[name])}.")
                        group = self.create_list(name, groups[name])
                        self.sleep(ACTION_DELAY)
                    self.emit(f"Создаю DNS-маршрут для {name}: interface={interface}.")
                    self.create_route(group, interface)
                    self.sleep(ACTION_DELAY)
            except RouterConnectionError:
                if attempt == VERIFY_ATTEMPTS:
                    raise
                self.reconnect()
        raise RouterError("Не удалось подтвердить создание всех списков и DNS-маршрутов.")

    def update(self, routing: RoutingList) -> None:
        groups = routing.groups()
        if not groups or routing.mode not in {"proxy", "direct"}:
            raise RouterError("Готовый лист пуст или имеет неверный режим.")
        self.emit("Авторизация и проверка интерфейсов.")
        self.login()
        interfaces = self.interfaces()
        wireguards = [name for name in interfaces if name.startswith("Wireguard")]
        if not wireguards:
            raise RouterError("На роутере нет WireGuard-подключения.")
        selected = wireguards[0]
        wan = next((name for name, info in interfaces.items() if info.get("interface-name") == "ISP"), None)
        if routing.mode == "direct" and wan is None:
            raise RouterError("Для режима direct не найден WAN-интерфейс ISP.")
        route_interface = wan if routing.mode == "direct" else selected
        self.emit(f"Интерфейсы проверены: WireGuard={selected}, route_interface={route_interface}.")
        for name in wireguards:
            self.disable_vpn_if_up(name)
        if wan is not None:
            self.emit(f"Настройка приоритета WAN-интерфейса {wan}.")
            self.move_wan(wan, bottom=routing.mode == "direct")
        self.emit("Удаление всех существующих списков и DNS-маршрутов.")
        self.delete_all_lists_and_routes()
        self.ensure_lists_and_routes(groups, route_interface)
        self.emit(f"Создание списков и DNS-маршрутов подтверждено: lists={len(groups)}.")
        self.enable_vpn(selected)
        self.emit("Списки и маршруты обновлены.")
