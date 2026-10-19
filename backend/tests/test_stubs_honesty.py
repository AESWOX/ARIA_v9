"""Заглушки не имеют права отвечать успехом, которого не было.

До этого PR кнопки «Save» в CHANNELS, «Add» в пуле ключей, enable/remove плагина и т.д. получали
`{"ok": true}` и UI рисовал зелёный тост, хотя ничего не менялось (пул ключей ещё и выбрасывал ключ).
Теперь такой маршрут отвечает 501 not_implemented, а fetchJSON бросает ошибку и UI показывает её.
"""
from __future__ import annotations

import ast
import re
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app

STUBS_PATH = Path(__file__).resolve().parents[1] / "aria" / "routers" / "stubs.py"

# (метод, путь, тело) — все эти маршруты обязаны отвечать 501.
NOT_IMPLEMENTED = [
    ("DELETE", "/providers/oauth/anthropic", None),
    ("POST", "/providers/oauth/anthropic/start", None),
    ("POST", "/ops/config-migrate", None),
    ("POST", "/ops/debug-share", {"redact": True}),
    ("POST", "/dashboard/plugins/rescan", None),
    ("POST", "/dashboard/agent-plugins/demo/enable", None),
    ("POST", "/dashboard/agent-plugins/demo/disable", None),
    ("DELETE", "/dashboard/agent-plugins/demo", None),
    ("PUT", "/dashboard/plugin-providers", {"memory_provider": "local"}),
    ("POST", "/dashboard/plugins/demo/visibility", {"hidden": True}),
    ("PUT", "/messaging/platforms/telegram", {"enabled": True, "env": {"TELEGRAM_BOT_TOKEN": "x"}}),
    ("POST", "/messaging/telegram/onboarding/start", {"bot_name": "aria_bot"}),
    ("POST", "/pairing/approve", {"platform": "telegram", "code": "ABC123"}),
    ("POST", "/pairing/revoke", {"platform": "telegram", "user_id": "1"}),
    ("POST", "/pairing/clear-pending", None),
    ("POST", "/credentials/pool", {"provider": "openai", "api_key": "sk-test-1234567890"}),
    ("DELETE", "/credentials/pool/openai/0", None),
    ("PUT", "/curator/paused", {"paused": True}),
]

# Мутирующие обработчики, которым разрешено НЕ бросать 501: они сами честно отвечают ok:false
# с причиной либо безвредны (отмена несуществующей сессии). Новый обработчик сюда добавляют осознанно.
HONEST_MUTATORS = {
    "oauth_submit",
    "oauth_cancel",
    "gateway_start",
    "gateway_stop",
    "gateway_restart",
    "agent_plugins_install",
    "agent_plugins_update",
    "messaging_platform_test",
    "telegram_onboarding_apply",
    "telegram_onboarding_cancel",
    "curator_run",
    "aria_update",
}


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


@pytest.mark.parametrize("method,path,body", NOT_IMPLEMENTED, ids=[f"{m} {p}" for m, p, _ in NOT_IMPLEMENTED])
def test_fake_stub_answers_501(client, method, path, body):
    r = client.request(method, path, json=body, headers=_headers())
    assert r.status_code == 501, f"{method} {path}: {r.status_code} {r.text[:200]}"
    detail = r.json()["detail"]
    assert detail["code"] == "not_implemented"
    assert detail["feature"] and detail["message"]


def test_credential_pool_never_stores_anything(client):
    """Раньше POST отвечал ok:true и писал в json только превью ключа (сам ключ терялся)."""
    r = client.post(
        "/credentials/pool",
        json={"provider": "openai", "api_key": "sk-live-SECRET-1234567890"},
        headers=_headers(),
    )
    assert r.status_code == 501
    assert "SECRET" not in r.text


def test_stub_501_requires_token(client):
    """501 не должен выдаваться без токена: auth проверяется раньше."""
    r = client.post("/pairing/clear-pending")
    assert r.status_code in (401, 403)


def _mutating_handlers():
    tree = ast.parse(STUBS_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in {"post", "put", "delete", "patch"}
            ):
                yield node
                break


def _is_bare_not_implemented(node) -> bool:
    body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Call)
        and getattr(body[0].value.func, "id", "") == "_not_implemented"
    )


def test_every_mutating_stub_is_501_or_explicitly_honest():
    offenders = [
        n.name for n in _mutating_handlers() if not _is_bare_not_implemented(n) and n.name not in HONEST_MUTATORS
    ]
    assert not offenders, (
        "Мутирующая заглушка без реального эффекта обязана вызывать _not_implemented(): "
        f"{offenders}. Если она честно отвечает ok:false — добавьте в HONEST_MUTATORS."
    )


def test_honest_mutators_really_do_not_claim_success():
    """«Честные» обработчики не возвращают ok:true (исключение — безвредная отмена сессии)."""
    allowed_true = {"oauth_cancel", "telegram_onboarding_cancel"}
    src = STUBS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    checked = 0
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in HONEST_MUTATORS - allowed_true:
            seg = ast.get_source_segment(src, node) or ""
            assert '"ok": True' not in seg, f"{node.name} возвращает ok:true"
            assert not re.search(r'_action\("[^"]+",\s*True', seg), f"{node.name} возвращает _action(..., True)"
            checked += 1
    assert checked == len(HONEST_MUTATORS - allowed_true), "HONEST_MUTATORS ссылается на несуществующий обработчик"
