"""mcp/oauth.py — OAuth 2.1 для удалённых серверов MCP (патч 0009).

Что реализовано (по спецификации MCP authorization, 2025-06-18):
  * поиск сервера авторизации: ``resource_metadata`` из ``WWW-Authenticate`` или
    ``/.well-known/oauth-protected-resource`` (RFC 9728), затем метаданные сервера
    авторизации (RFC 8414 / OpenID discovery);
  * динамическая регистрация клиента (RFC 7591), публичный клиент без секрета;
  * Authorization Code + PKCE (S256), параметр ``resource`` (RFC 8707);
  * обновление токена (refresh_token, с учётом ротации) и отзыв (RFC 7009).

Хранение: ``<data_dir>/mcp_oauth.json`` с правами 0600 (токены и client_id по серверам).
Токены никогда не попадают в ``mcp_servers.json``, логи и ответы API.

Не реализовано / не проверено вживую: PRM-ответы нестандартного вида, ``client_secret_basic``,
redirect на порт установленной сборки у конкретного сервера (см. BOOST_PLAN §5).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from aria import paths

logger = logging.getLogger("local_agent.mcp.oauth")

CLIENT_NAME = "ARIA"
STATE_TTL_SEC = 600
EXPIRY_SKEW_SEC = 30
HTTP_TIMEOUT = 20.0
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_RESOURCE_META_RE = re.compile(r'resource_metadata="([^"]+)"')


class OAuthError(Exception):
    """Ошибка авторизации (сообщение безопасно показывать владельцу)."""


# Подмена в тестах: фабрика httpx-клиента (например, с MockTransport).
_client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(  # noqa: E731
    timeout=HTTP_TIMEOUT, follow_redirects=False
)

_file_lock = threading.Lock()
_pending: dict[str, dict] = {}
_refresh_locks: dict[str, asyncio.Lock] = {}


# ── хранилище ────────────────────────────────────────────────────────────

def store_path():
    return paths.data_dir() / "mcp_oauth.json"


def _load() -> dict[str, dict]:
    path = store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("mcp_oauth.json is unreadable; treating as empty")
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # Windows: права ACL, chmod почти ничего не делает
        pass
    os.replace(tmp, path)


def get_record(server: str) -> dict:
    with _file_lock:
        rec = _load().get(server)
    return dict(rec) if isinstance(rec, dict) else {}


def _update_record(server: str, **changes: Any) -> dict:
    with _file_lock:
        data = _load()
        rec = dict(data.get(server) or {})
        rec.update(changes)
        data[server] = rec
        _save(data)
    return rec


def drop_record(server: str) -> None:
    with _file_lock:
        data = _load()
        if data.pop(server, None) is not None:
            _save(data)


def access_token(server: str) -> str | None:
    """Действующий access-токен или None (синхронно: нужен ``HttpTransport.token_provider``)."""
    tokens = get_record(server).get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        return None
    exp = tokens.get("expires_at")
    if exp is not None and float(exp) <= time.time():
        return None
    return str(token)


def status(server: str) -> str:
    """``none`` — не авторизован; ``authorized`` — токен есть (или обновится); ``expired`` — истёк без refresh."""
    tokens = get_record(server).get("tokens") or {}
    if not tokens.get("access_token"):
        return "none"
    exp = tokens.get("expires_at")
    if exp is None or float(exp) > time.time():
        return "authorized"
    return "authorized" if tokens.get("refresh_token") else "expired"


# ── проверка URL ─────────────────────────────────────────────────────────

def _check_url(url: str, what: str = "endpoint") -> str:
    parts = urlsplit(str(url or ""))
    if parts.username or parts.password or not parts.hostname:
        raise OAuthError(f"invalid {what} URL")
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK):
        return str(url)
    raise OAuthError(f"{what} must use https")


async def _get_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        _check_url(url, "metadata")
        resp = await client.get(url, headers={"Accept": "application/json"})
    except (OAuthError, httpx.HTTPError):
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ── обнаружение ──────────────────────────────────────────────────────────

async def discover(server_url: str, www_authenticate: str = "", client: httpx.AsyncClient | None = None) -> dict:
    """Метаданные сервера авторизации и канонический ресурс для ``server_url``."""
    own = client is None
    client = client or _client_factory()
    try:
        parts = urlsplit(server_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path.rstrip("/")
        candidates: list[str] = []
        match = _RESOURCE_META_RE.search(www_authenticate or "")
        if match:
            candidates.append(match.group(1))
        if path:
            candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
        candidates.append(f"{origin}/.well-known/oauth-protected-resource")

        prm: dict | None = None
        for url in candidates:
            prm = await _get_json(client, url)
            if prm:
                break

        servers = (prm or {}).get("authorization_servers")
        as_url = str(servers[0]) if isinstance(servers, list) and servers else origin
        resource = server_url
        if prm and isinstance(prm.get("resource"), str) and urlsplit(prm["resource"]).netloc == parts.netloc:
            resource = prm["resource"]

        ap = urlsplit(_check_url(as_url, "authorization server"))
        as_origin, as_path = f"{ap.scheme}://{ap.netloc}", ap.path.rstrip("/")
        meta_urls = [f"{as_origin}/.well-known/oauth-authorization-server{as_path}"]
        meta_urls.append(f"{as_origin}/.well-known/openid-configuration{as_path}")
        if as_path:
            meta_urls.append(f"{as_url.rstrip('/')}/.well-known/openid-configuration")
        meta: dict | None = None
        for url in meta_urls:
            meta = await _get_json(client, url)
            if meta:
                break
        if not meta:
            raise OAuthError("authorization server metadata not found")
        issuer = meta.get("issuer")
        if isinstance(issuer, str) and issuer.rstrip("/") != as_url.rstrip("/"):
            raise OAuthError("authorization server issuer mismatch")
        for key in ("authorization_endpoint", "token_endpoint"):
            if not isinstance(meta.get(key), str):
                raise OAuthError(f"authorization server has no {key}")
            _check_url(meta[key], key)
        methods = meta.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise OAuthError("authorization server does not support PKCE S256")
        keep = ("issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint")
        return {"resource": resource, "meta": {k: meta[k] for k in keep if isinstance(meta.get(k), str)}}
    finally:
        if own:
            await client.aclose()


# ── регистрация клиента ─────────────────────────────────────────────────

async def _ensure_client_registration(server: str, meta: dict, redirect_uri: str, client: httpx.AsyncClient) -> dict:
    rec = get_record(server)
    if rec.get("client_id") and redirect_uri in (rec.get("redirect_uris") or []) and rec.get("issuer") == meta.get("issuer"):
        return rec
    endpoint = meta.get("registration_endpoint")
    if not endpoint:
        raise OAuthError("this server does not support dynamic client registration")
    _check_url(endpoint, "registration endpoint")
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        resp = await client.post(endpoint, json=body, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise OAuthError(f"client registration failed: {exc.__class__.__name__}") from exc
    if resp.status_code not in (200, 201):
        raise OAuthError(f"client registration rejected (HTTP {resp.status_code})")
    try:
        data = resp.json()
    except ValueError as exc:
        raise OAuthError("client registration returned invalid JSON") from exc
    if not isinstance(data, dict) or not data.get("client_id"):
        raise OAuthError("client registration returned no client_id")
    return _update_record(
        server,
        client_id=str(data["client_id"]),
        client_secret=str(data["client_secret"]) if data.get("client_secret") else None,
        redirect_uris=[redirect_uri],
        issuer=meta.get("issuer"),
        tokens={},
    )


# ── поток авторизации ────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _gc_pending() -> None:
    now = time.time()
    for key in [k for k, v in _pending.items() if now - v["created"] > STATE_TTL_SEC]:
        _pending.pop(key, None)


async def start_flow(server: str, server_url: str, redirect_uri: str, www_authenticate: str = "") -> str:
    """Подготовить авторизацию и вернуть URL, который владелец откроет в браузере."""
    _check_url(redirect_uri, "redirect")
    client = _client_factory()
    try:
        info = await discover(server_url, www_authenticate, client)
        meta = info["meta"]
        rec = await _ensure_client_registration(server, meta, redirect_uri, client)
    finally:
        await client.aclose()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    _gc_pending()
    _pending[state] = {
        "server": server, "verifier": verifier, "redirect_uri": redirect_uri,
        "meta": meta, "resource": info["resource"], "client_id": rec["client_id"], "created": time.time(),
    }
    _update_record(server, meta=meta, resource=info["resource"])
    query = urlencode({
        "response_type": "code", "client_id": rec["client_id"], "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": info["resource"],
    })
    sep = "&" if "?" in meta["authorization_endpoint"] else "?"
    return f"{meta['authorization_endpoint']}{sep}{query}"


def _parse_token_response(data: Any, previous_refresh: str | None = None) -> dict:
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError("token endpoint returned no access_token")
    if str(data.get("token_type", "bearer")).lower() != "bearer":
        raise OAuthError("unsupported token type")
    tokens: dict[str, Any] = {"access_token": str(data["access_token"])}
    try:
        expires_in = float(data["expires_in"]) if data.get("expires_in") is not None else None
    except (TypeError, ValueError):
        expires_in = None
    tokens["expires_at"] = time.time() + expires_in if expires_in else None
    refresh = data.get("refresh_token") or previous_refresh
    if refresh:
        tokens["refresh_token"] = str(refresh)
    if data.get("scope"):
        tokens["scope"] = str(data["scope"])
    return tokens


async def _token_request(meta: dict, form: dict[str, str], client: httpx.AsyncClient) -> Any:
    try:
        resp = await client.post(
            meta["token_endpoint"], data=form,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"token request failed: {exc.__class__.__name__}") from exc
    if resp.status_code != 200:
        code = ""
        try:
            body = resp.json()
            code = str(body.get("error", "")) if isinstance(body, dict) else ""
        except ValueError:
            pass
        raise OAuthError(f"token endpoint rejected the request (HTTP {resp.status_code}{': ' + code if code else ''})")
    try:
        return resp.json()
    except ValueError as exc:
        raise OAuthError("token endpoint returned invalid JSON") from exc


def _with_secret(form: dict[str, str], rec: dict) -> dict[str, str]:
    if rec.get("client_secret"):
        form["client_secret"] = str(rec["client_secret"])
    return form


async def complete_flow(state: str, code: str) -> str:
    """Обменять ``code`` на токены. Возвращает имя сервера. ``state`` одноразовый."""
    _gc_pending()
    flow = _pending.pop(state, None)
    if flow is None:
        raise OAuthError("unknown or expired authorization request")
    if not code:
        raise OAuthError("authorization response has no code")
    server = flow["server"]
    rec = get_record(server)
    form = _with_secret({
        "grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"],
        "client_id": flow["client_id"], "code_verifier": flow["verifier"], "resource": flow["resource"],
    }, rec)
    client = _client_factory()
    try:
        data = await _token_request(flow["meta"], form, client)
    finally:
        await client.aclose()
    _update_record(server, tokens=_parse_token_response(data))
    return server


def reject_flow(state: str) -> str | None:
    """Владелец отказал (или сервер вернул error): забыть запрос; вернуть имя сервера, если был."""
    flow = _pending.pop(state, None)
    return flow["server"] if flow else None


# ── обновление и отзыв ───────────────────────────────────────────────────

async def refresh(server: str) -> bool:
    """Обновить токен по refresh_token. False — обновлять нечем или сервер отказал (токены стёрты при invalid_grant)."""
    lock = _refresh_locks.setdefault(server, asyncio.Lock())
    async with lock:
        rec = get_record(server)
        tokens = rec.get("tokens") or {}
        refresh_token = tokens.get("refresh_token")
        meta = rec.get("meta") or {}
        if not refresh_token or not meta.get("token_endpoint") or not rec.get("client_id"):
            return False
        form = _with_secret({
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": rec["client_id"], "resource": rec.get("resource") or "",
        }, rec)
        if not form["resource"]:
            form.pop("resource")
        client = _client_factory()
        try:
            data = await _token_request(meta, form, client)
        except OAuthError as exc:
            if "invalid_grant" in str(exc):
                _update_record(server, tokens={})
            logger.warning("mcp oauth refresh failed for '%s': %s", server, exc)
            return False
        finally:
            await client.aclose()
        try:
            _update_record(server, tokens=_parse_token_response(data, previous_refresh=str(refresh_token)))
        except OAuthError as exc:
            logger.warning("mcp oauth refresh for '%s' gave a bad response: %s", server, exc)
            return False
        return True


async def ensure_fresh(server: str) -> None:
    """Перед подключением: если токен истёк или вот-вот истечёт — обновить."""
    tokens = get_record(server).get("tokens") or {}
    exp = tokens.get("expires_at")
    if tokens.get("access_token") and exp is not None and float(exp) - EXPIRY_SKEW_SEC <= time.time():
        await refresh(server)


async def revoke(server: str) -> dict:
    """Отозвать токены на сервере авторизации (best effort) и стереть всё локально."""
    rec = get_record(server)
    tokens = rec.get("tokens") or {}
    meta = rec.get("meta") or {}
    revoked = False
    endpoint = meta.get("revocation_endpoint")
    token = tokens.get("refresh_token") or tokens.get("access_token")
    if endpoint and token and rec.get("client_id"):
        client = _client_factory()
        try:
            _check_url(endpoint, "revocation endpoint")
            resp = await client.post(
                endpoint, data=_with_secret({"token": token, "client_id": rec["client_id"]}, rec),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            revoked = resp.status_code == 200
        except (OAuthError, httpx.HTTPError):
            revoked = False
        finally:
            await client.aclose()
    drop_record(server)
    return {"revoked_remotely": revoked, "erased_locally": True}
