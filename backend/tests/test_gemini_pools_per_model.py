"""Квота Gemini — на проект и модель: пул ключей общий у провайдеров с одной моделью и раздельный у разных.

Живой случай 12.10: суточная квота gemini-3.8-flash исчерпана (retryDelay 64600s), а соседние модели
с теми же ключами отвечали 200. Каждый провайдер перебирал все ключи заново (свой пул), cooldown не делился.
"""
import httpx

from aria import config
from aria.llm.providers.openai_compatible import _cooldown_for_429


def _router(monkeypatch, flash, pro):
    from aria.llm.router import build_default_router

    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2,k3")
    monkeypatch.setenv("GEMINI_FLASH_MODEL", flash)
    monkeypatch.setenv("GEMINI_PRO_MODEL", pro)
    config.get_settings.cache_clear()
    try:
        r = build_default_router()
    finally:
        config.get_settings.cache_clear()
    return {p.provider_id: p for cls in r.providers_by_class.values() for p in cls if p.provider_id.startswith("gemini")}


def test_same_model_providers_share_one_pool_and_cooldown(monkeypatch):
    prov = _router(monkeypatch, "gemini-3.8-flash", "gemini-3.8-flash")
    assert prov["gemini-flash"].key_pool is prov["gemini-pro"].key_pool is prov["gemini-pro-premium-fallback"].key_pool
    prov["gemini-flash"].key_pool.mark_rate_limited("k1", 3600)
    assert not prov["gemini-pro"].key_pool._is_usable("k1"), "429 у Flash известен запасному провайдеру на той же модели"


def test_different_models_do_not_block_each_other(monkeypatch):
    prov = _router(monkeypatch, "gemini-3.8-flash", "gemini-3.7-flash")
    assert prov["gemini-flash"].key_pool is not prov["gemini-pro"].key_pool
    assert prov["gemini-pro"].key_pool is prov["gemini-pro-premium-fallback"].key_pool
    for k in ("k1", "k2", "k3"):
        prov["gemini-flash"].key_pool.mark_rate_limited(k, 3600)
    assert prov["gemini-pro"].key_pool.next_key() in ("k1", "k2", "k3"), "исчерпанная модель A не блокирует модель B"


def _resp(headers=None):
    return httpx.Response(429, headers=headers or {}, request=httpx.Request("POST", "http://x"))


def test_retry_delay_from_google_body_beats_the_one_hour_guess():
    body = '{"error":{"details":[{"retryDelay":"64600s"}]}} generaterequestsperdayperprojectpermodel-freetier'
    assert _cooldown_for_429(_resp(), body.lower(), 60.0) == 64600.0


def test_retry_delay_is_capped_and_missing_delay_keeps_old_behaviour():
    assert _cooldown_for_429(_resp(), '"retrydelay": "999999s"', 60.0) == 24 * 3600
    assert _cooldown_for_429(_resp(), "quota per day exceeded", 60.0) == 3600
    assert _cooldown_for_429(_resp(), "slow down", 60.0) == 60.0
    assert _cooldown_for_429(_resp({"retry-after": "120"}), '"retrydelay": "5s"', 60.0) == 120.0
