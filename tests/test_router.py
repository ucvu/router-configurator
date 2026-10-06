import copy
import hashlib

import pytest
import requests

from router_configurator.lists import parse_routing_list
from router_configurator.router import HTTP_TIMEOUT, RouterClient, RouterConnectionError, RouterError


class Response:
    def __init__(self, data=None, status=200, headers=None):
        self.data = data
        self.status_code = status
        self.headers = headers or {}
    def json(self):
        return copy.deepcopy(self.data)


class RciSession:
    """Stateful fake: executes actual RCI command shapes and can silently drop a route."""
    def __init__(self, *, wireguard=True, wan=True, lose_route=False, disconnect=False, login_status=200):
        self.interface_data = {}
        if wan:
            self.interface_data["GigabitEthernet0"] = {"interface-name": "ISP", "state": "up"}
        if wireguard:
            self.interface_data["Wireguard0"] = {"state": "up"}
            self.interface_data["Wireguard1"] = {"state": "up"}
        self.global_settings = {
            "GigabitEthernet0": {"ip": {"global": {"enabled": True, "order": 1}}},
            "Wireguard0": {"ip": {"global": {"enabled": True, "order": 0}}},
        }
        self.groups = {"old-list": {"description": "user-rule", "include": [{"address": "old.example"}]}}
        self.routes = [{"index": 1, "group": "old-list", "interface": "Wireguard0"}]
        self.commands = []
        self.lose_route = lose_route
        self.disconnect = disconnect
        self.login_status = login_status
        self.closed = False
        self.login_body = None

    def close(self):
        self.closed = True

    def request(self, method, url, timeout, allow_redirects, json=None):
        assert timeout == HTTP_TIMEOUT
        assert allow_redirects is False
        if url.endswith("/auth"):
            if method == "GET":
                return Response(status=401, headers={"X-NDM-Realm": "realm", "X-NDM-Challenge": "challenge"})
            self.login_body = json
            return Response(status=self.login_status)
        assert url.endswith("/rci/") and method == "POST"
        results = []
        for command in json:
            self.commands.append(copy.deepcopy(command))
            if "show" in command:
                show = command["show"]
                if "interface" in show:
                    results.append({"show": {"interface": self.interface_data}})
                elif "object-group" in show["sc"]:
                    results.append({"show": {"sc": {"object-group": {"fqdn": self.groups}}}})
                elif "dns-proxy" in show["sc"]:
                    results.append({"show": {"sc": {"dns-proxy": {"route": self.routes}}}})
                else:
                    results.append({"show": {"sc": {"interface": self.global_settings}}})
            elif "interface" in command:
                interface = command["interface"]
                if "up" in interface:
                    self.interface_data[interface["name"]]["state"] = "up" if interface["up"] else "down"
                    if self.disconnect:
                        self.disconnect = False
                        raise requests.ConnectionError("secret error should not leak")
                else:
                    self.global_settings[interface["name"]] = {"ip": interface["ip"]}
                results.append({})
            elif "object-group" in command:
                for name, info in command["object-group"]["fqdn"].items():
                    if info.get("no"):
                        self.groups.pop(name, None)
                    else:
                        self.groups[name] = copy.deepcopy(info)
                results.append({})
            elif "dns-proxy" in command:
                route = command["dns-proxy"]["route"]
                if isinstance(route, list):
                    indexes = {item["index"] for item in route if item.get("no")}
                    self.routes = [item for item in self.routes if item["index"] not in indexes]
                elif self.lose_route:
                    self.lose_route = False
                else:
                    self.routes = [item for item in self.routes if item["group"] != route["group"]]
                    self.routes.append({**route, "index": len(self.routes) + 1})
                results.append({})
            else:
                assert "system" in command
                results.append({})
        return Response(results)


@pytest.mark.parametrize("mode", ["proxy", "direct"])
def test_update_applies_real_command_shapes(mode):
    session = RciSession(lose_route=True, disconnect=True)
    routing = parse_routing_list(f"mode: {mode}\nexample.com\n203.0.113.0/24\n")
    client = RouterClient("router.example", "admin", "password", session=session, sleep=lambda seconds: None)
    client.update(routing)
    digest = hashlib.md5(b"admin:realm:password").hexdigest()
    assert session.login_body == {"login": "admin", "password": hashlib.sha256(("challenge" + digest).encode()).hexdigest()}
    assert "old-list" not in session.groups
    assert {group["description"] for group in session.groups.values()} == {"geosite_domains_1", "geoip_ips_1"}
    assert {route["interface"] for route in session.routes} == {"Wireguard0" if mode == "proxy" else "GigabitEthernet0"}
    assert session.interface_data["Wireguard0"]["state"] == "up"
    assert session.interface_data["Wireguard1"]["state"] == "down"
    commands = session.commands
    vpn_down = next(i for i, command in enumerate(commands) if command.get("interface", {}).get("up") is False)
    deletion = next(i for i, command in enumerate(commands) if isinstance(command.get("dns-proxy", {}).get("route"), list))
    vpn_up = next(i for i, command in enumerate(commands) if command.get("interface", {}).get("up") is True)
    assert vpn_down < deletion < vpn_up
    assert session.global_settings["GigabitEthernet0"]["ip"]["global"]["order"] == (0 if mode == "proxy" else 1)
    client.close()
    assert session.closed


@pytest.mark.parametrize(("wireguard", "wan", "mode"), [(False, True, "proxy"), (True, False, "direct")])
def test_missing_interfaces_never_mutate(wireguard, wan, mode):
    session = RciSession(wireguard=wireguard, wan=wan)
    client = RouterClient("router.example", "admin", "password", session=session, sleep=lambda seconds: None)
    with pytest.raises(RouterError):
        client.update(parse_routing_list(f"mode: {mode}\nexample.com\n"))
    assert all("show" in command for command in session.commands)
    assert "old-list" in session.groups


def test_auth_failure_never_mutates():
    session = RciSession(login_status=403)
    client = RouterClient("router.example", "admin", "secret", session=session)
    with pytest.raises(RouterError, match="403"):
        client.update(parse_routing_list("mode: proxy\nexample.com\n"))
    assert session.commands == []


def test_lists_are_chunked_on_router():
    session = RciSession()
    client = RouterClient("router.example", "admin", "password", session=session, sleep=lambda seconds: None)
    routing = parse_routing_list("mode: proxy\n" + "\n".join(f"d{n}.example.com" for n in range(600)))
    client.update(routing)
    assert sorted(len(info["include"]) for info in session.groups.values()) == [2, 299, 299]


@pytest.mark.parametrize("data", [[{"error": [{"code": "secret"}]}], [{"status": "error"}], {"unexpected": True}, []])
def test_bad_rci_responses_are_errors(data):
    session = RciSession()
    session.request = lambda *args, **kwargs: Response(data)
    client = RouterClient("router.example", "admin", "password", session=session)
    with pytest.raises(RouterError) as error:
        client.rci([{"test": {}}])
    assert "secret" not in str(error.value)


def test_connection_exception_is_sanitized():
    session = RciSession()
    def fail(*args, **kwargs):
        raise requests.ConnectionError("password and proxy secret")
    session.request = fail
    client = RouterClient("router.example", "admin", "password", session=session)
    with pytest.raises(RouterConnectionError) as error:
        client.login()
    assert "password" not in str(error.value)


def test_silent_route_failures_are_bounded():
    session = RciSession()
    client = RouterClient("router.example", "admin", "password", session=session, sleep=lambda seconds: None)
    client.create_route = lambda *args: None
    with pytest.raises(RouterError, match="подтвердить"):
        client.update(parse_routing_list("mode: proxy\nexample.com\n"))
    assert session.interface_data["Wireguard0"]["state"] == "down"


def test_disconnect_when_enabling_is_verified():
    session = RciSession()
    session.interface_data["Wireguard0"]["state"] = "down"
    session.disconnect = True
    client = RouterClient("router.example", "admin", "password", session=session, sleep=lambda seconds: None)
    client.enable_vpn("Wireguard0")
    assert session.interface_data["Wireguard0"]["state"] == "up"
    assert session.login_body is not None


def test_missing_interface_cannot_be_reported_as_enabled():
    session = RciSession(wireguard=False)
    client = RouterClient("router.example", "admin", "password", session=session)
    client.set_interface_up = lambda *args: None
    with pytest.raises(RouterError, match="включение"):
        client.enable_vpn("Wireguard0")


def test_temporary_http_failure_is_retriable():
    session = RciSession()
    session.request = lambda *args, **kwargs: Response(status=503)
    client = RouterClient("router.example", "admin", "password", session=session)
    with pytest.raises(RouterConnectionError, match="503"):
        client.rci([{"show": {"interface": {}}}])
