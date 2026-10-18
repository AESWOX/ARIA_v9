"""B4 (часть): каждый безпараметровый GET из /openapi.json вызывается С токеном и отвечает 2xx с валидным JSON.

Список берётся из openapi динамически: новый GET-маршрут без параметров автоматически попадает под проверку.
Исключения — только явные, с причиной (INTENTIONAL). Сеть закрыта, БД и vault — временные из conftest.
"""
from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app

# маршрут -> (ожидаемый статус, почему он законно не 2xx в изолированном тесте)
INTENTIONAL: dict[str, tuple[int, str]] = {
    "/mcp/oauth/callback": (400, "браузерный колбэк: без валидного одноразового state отказывает"),
    "/storage/b2/buckets": (503, "B2 не настроен в тестовом окружении"),
    "/storage/b2/objects": (503, "B2 не настроен в тестовом окружении"),
    "/tasks/runner/status": (503, "TaskRunner стартует в lifespan, TestClient без lifespan его не поднимает"),
}


def _simple_gets() -> list[str]:
    spec = app.openapi()["paths"]
    out = []
    for path, ops in spec.items():
        get = ops.get("get")
        if get is None or "{" in path:
            continue
        if any(p.get("required") for p in get.get("parameters", [])):
            continue  # обязательные query-параметры — отдельные тесты с телом
        out.append(path)
    return sorted(out)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*a, **k):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def _headers():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


@pytest.mark.parametrize("path", _simple_gets())
def test_get_route_answers_2xx_with_json(client, path):
    r = client.get(path, headers=_headers())
    if path in INTENTIONAL:
        expected, why = INTENTIONAL[path]
        assert r.status_code in _accepted(expected, why), f"{path}: ожидали {expected} ({why}), получили {r.status_code}"
        return
    assert 200 <= r.status_code < 300, f"{path}: {r.status_code} {r.text[:200]}"
    if r.headers.get("content-type", "").startswith("application/json"):
        r.json()


def test_guard_the_walk_is_not_empty():
    assert len(_simple_gets()) > 40


# --- GET с path-параметрами: несуществующий id -> 404 (не 500) -----------------------------------------
MISSING_ID = "00000000-0000-0000-0000-000000000000"

# маршрут -> (ожидаемый статус, причина). Всё остальное обязано быть 404.
def _accepted(expected: int, why: str) -> set[int]:
    """B2 не настроен -> 503; если в .env владельца ключи B2 есть, а сеть в тестах закрыта -> 502. Оба ответа осознанные."""
    return {expected, 502} if "B2" in why else {expected}


NOT_404: dict[str, tuple[int, str]] = {
    "/actions/{name}/status": (200, "опрос состояния: действие, которое не запускали, = running:false"),
    "/providers/oauth/{provider_id}/poll/{session_id}": (200, "опрос: неизвестная сессия = status:expired"),
    "/storage/b2/objects/{key}/meta": (503, "B2 не настроен в тестовом окружении"),
    "/messaging/telegram/onboarding/{pairing_id}": (200, "ЗАГЛУШКА (routers/stubs.py, B6): всегда 'waiting' с выдуманным expires_at; убрать вместе со stubs.py"),
}


def _param_gets() -> list[str]:
    return sorted(p for p, ops in app.openapi()["paths"].items() if "get" in ops and "{" in p)


@pytest.mark.parametrize("path", _param_gets())
def test_get_with_unknown_id_is_404_not_500(client, path):
    import re

    r = client.get(re.sub(r"\{[^}]+\}", MISSING_ID, path), headers=_headers())
    expected, why = NOT_404.get(path, (404, "несуществующий ресурс"))
    assert r.status_code in _accepted(expected, why), f"{path}: ожидали {expected} ({why}), получили {r.status_code}: {r.text[:150]}"
