"""Проверка связи с провайдером не должна бить в /models перед каждым вызовом модели (лог владельца: ~18 000 GET /models за 2 дня)."""
import asyncio

import httpx
import pytest

from aria.llm.providers import openai_compatible as oc


class _Resp:
    def __init__(self, code):
        self.status_code = code


def _install_client(monkeypatch, codes):
    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls["n"] += 1
            code = codes[min(calls["n"] - 1, len(codes) - 1)]
            if isinstance(code, Exception):
                raise code
            return _Resp(code)

    monkeypatch.setattr(oc.httpx, "AsyncClient", _Client)
    return calls


@pytest.fixture(autouse=True)
def _clean():
    oc.reset_connectivity_cache()
    yield
    oc.reset_connectivity_cache()


def _prov(model, base="https://example.test/v1"):
    return oc.OpenAICompatibleProvider("p-" + model, "standard_reasoning", base, model, api_key="k")


def test_success_is_cached_per_host_across_chain(monkeypatch):
    calls = _install_client(monkeypatch, [200])
    chain = [_prov(f"m{i}") for i in range(8)]
    for _round in range(3):
        for p in chain:
            assert asyncio.run(p.check_connectivity(2)) is True
    assert calls["n"] == 1, "один хост = одна проверка за TTL, а не 24"


def test_different_hosts_are_checked_separately(monkeypatch):
    calls = _install_client(monkeypatch, [200])
    assert asyncio.run(_prov("a", "https://a.test/v1").check_connectivity(2))
    assert asyncio.run(_prov("b", "https://b.test/v1").check_connectivity(2))
    assert calls["n"] == 2


def test_failure_is_not_cached(monkeypatch):
    calls = _install_client(monkeypatch, [httpx.ConnectError("x"), 200])
    p = _prov("m")
    assert asyncio.run(p.check_connectivity(2)) is False
    assert asyncio.run(p.check_connectivity(2)) is True
    assert calls["n"] == 2


def test_5xx_is_not_cached(monkeypatch):
    calls = _install_client(monkeypatch, [503, 200])
    p = _prov("m")
    assert asyncio.run(p.check_connectivity(2)) is False
    assert asyncio.run(p.check_connectivity(2)) is True
    assert calls["n"] == 2


def test_cache_expires(monkeypatch):
    calls = _install_client(monkeypatch, [200])
    p = _prov("m")
    assert asyncio.run(p.check_connectivity(2))
    key = p.base_url
    oc._connectivity_ok_until[key] = 0.0
    assert asyncio.run(p.check_connectivity(2))
    assert calls["n"] == 2
