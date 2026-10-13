import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

import web.app as app_mod
from modules.module_status import ERROR, OK, RATE_LIMITED, classify
from web.security import require_api_key

MAC = "00-00-5e-00-53-af"
NORMALIZED = "00:00:5E:00:53:AF"
# A prefix that is in neither the bundled OUI table nor a real registry, so the API path is taken.
UNREGISTERED = "FA:CE:B0:0C:00:01"


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Drive /api/mac-lookup with no network, no auth and an empty cache."""
    monkeypatch.setattr(app_mod, "_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(app_mod, "_lookup_local_oui", lambda mac: None)
    app_mod.app.dependency_overrides[require_api_key] = lambda: None
    app_mod.limiter.enabled = False
    calls = []

    def answer(response):
        def fake_get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(app_mod._requests, "get", fake_get)
        return calls

    yield TestClient(app_mod.app), answer, calls
    app_mod.app.dependency_overrides.clear()
    app_mod.limiter.enabled = True


def lookup(client, mac=NORMALIZED):
    return client.post("/api/mac-lookup", json={"mac": mac})


def test_registered_vendor_is_ok(api):
    client, answer, _ = api
    answer(FakeResponse(200, "  Ubiquiti Inc  "))

    result = lookup(client).json()

    assert result["vendor"] == "Ubiquiti Inc"
    assert result["source"] == "api"
    assert not result.get("error")


def test_unregistered_mac_is_a_valid_answer_not_an_error(api):
    client, answer, _ = api
    answer(FakeResponse(404, '{"errors":{"detail":"Not Found"}}'))

    result = lookup(client, UNREGISTERED).json()

    assert result["vendor"] is None
    assert result["status"] == OK
    assert result["status_reason"] == "Vendor not registered"
    assert not result.get("error")
    assert classify(result) == OK


def test_rate_limit_is_reported_as_rate_limited(api):
    client, answer, _ = api
    answer(FakeResponse(429, "Too Many Requests"))

    result = lookup(client).json()

    assert result["vendor"] is None
    assert result["status"] == RATE_LIMITED
    assert "rate limit" in result["status_reason"]
    assert not result.get("error")


def test_other_server_status_is_an_error_with_the_status(api):
    client, answer, _ = api
    answer(FakeResponse(500, "boom"))

    result = lookup(client).json()

    assert result["vendor"] is None
    assert result["status"] == ERROR
    assert "500" in result["error"]


def test_connection_error_is_an_error_not_a_crash(api):
    client, answer, _ = api
    answer(requests.ConnectionError("dns failure"))

    response = lookup(client)

    assert response.status_code == 200
    result = response.json()
    assert result["vendor"] is None
    assert result["status"] == ERROR
    assert result["error"]


def test_request_uses_the_normalized_mac_and_the_module_proxy(api, monkeypatch):
    client, answer, calls = api
    proxies = {"https": "socks5h://127.0.0.1:9050"}
    monkeypatch.setattr(app_mod, "get_proxies", lambda: proxies)
    answer(FakeResponse(200, "Ubiquiti Inc"))

    lookup(client, MAC)

    assert calls[0]["url"] == f"https://api.macvendors.com/{NORMALIZED}"
    assert calls[0]["proxies"] == proxies


def test_only_successful_lookups_are_cached(api):
    client, answer, calls = api
    answer(FakeResponse(404))
    lookup(client)
    lookup(client)
    assert len(calls) == 2  # a 404 is not cached, so a vendor registered later is picked up

    answer(FakeResponse(200, "Ubiquiti Inc"))
    lookup(client)
    lookup(client)
    assert len(calls) == 3  # the success was cached: the second call never reached the API


def test_local_oui_hit_never_calls_the_api(api, monkeypatch):
    client, answer, calls = api
    monkeypatch.setattr(app_mod, "_lookup_local_oui", lambda mac: "Locally Known Vendor")
    answer(FakeResponse(500))

    result = lookup(client).json()

    assert result == {"mac": NORMALIZED, "vendor": "Locally Known Vendor", "source": "local"}
    assert calls == []
