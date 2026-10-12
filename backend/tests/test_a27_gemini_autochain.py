"""A27 (патч 0027): автоподбор цепочек Gemini по списку моделей ключа и их проверке."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from aria import paths
from aria.config import Settings
from aria.llm import gemini_discovery as gd

# Реальный список моделей ключа владельца (12.10.2026), как его отдаёт API.
RAW = """models/gemini-2.5-flash models/gemini-2.5-pro models/gemini-2.5-flash-preview-tts models/gemini-2.5-pro-preview-tts
models/gemma-4-26b-a4b-it models/gemma-4-31b-it models/gemini-flash-latest models/gemini-flash-lite-latest models/gemini-pro-latest
models/gemini-2.5-flash-lite models/gemini-2.5-flash-image models/gemini-3-flash-preview models/gemini-3.1-pro-preview
models/gemini-3.1-pro-preview-customtools models/gemini-3.1-flash-lite-preview models/gemini-3.1-flash-lite
models/gemini-3-pro-image-preview models/gemini-3.1-flash-image models/gemini-nano-banana-2.1 models/gemini-3.5-flash
models/gemini-3.5-flash-lite models/gemini-omni-flash-preview models/gemini-3.5-transcribe models/gemini-3.6-flash
models/gemini-3.7-flash models/gemini-3.8-flash models/lyria-3.5 models/gemini-3.8-flash-tts models/gemini-3.8-live
models/gemini-robotics-er-2-preview models/gemini-2.5-computer-use-preview-10-2025 models/antigravity-preview-latest
models/deep-research-max-preview-04-2026 models/gemini-embedding-001 models/aqa models/veo-3.1-generate-preview
models/gemini-3.1-flash-live-preview models/gemini-3.5-live-translate-preview"""
MODELS = RAW.split()


def test_only_text_chat_models_are_classified():
    kept = {c.model for c in map(gd.classify, MODELS) if c}
    assert "gemini-3.8-flash" in kept and "gemini-3.1-pro-preview" in kept and "gemini-3.5-flash-lite" in kept
    for bad in ("tts", "image", "live", "embedding", "veo", "lyria", "robotics", "computer-use", "antigravity",
                "deep-research", "aqa", "transcribe", "nano-banana", "omni", "customtools", "gemma", "latest"):
        assert not any(bad in m for m in kept), f"{bad} не должно попасть в цепочки"


def test_tiers_and_order_newest_first():
    tiers = gd.select_candidates(MODELS, max_per_tier=4)
    assert [c.model for c in tiers["pro"]] == ["gemini-3.1-pro-preview", "gemini-2.5-pro"]
    assert [c.model for c in tiers["flash"]] == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
    # при равной версии (3.1) стабильная модель раньше preview
    assert [c.model for c in tiers["lite"]] == [
        "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite"]


def test_chains_go_down_in_quality():
    chains = gd.build_chains(MODELS)
    assert chains["pro"][:3] == ["gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-3.8-flash"]
    assert chains["flash"] == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                               "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert chains["compression"][0] == "gemini-3.5-flash-lite"
    assert all(len(v) <= gd.MAX_CHAIN for v in chains.values())


def test_dead_models_are_dropped_but_busy_ones_stay():
    status = {"gemini-3.1-pro-preview": "quota_zero", "gemini-2.5-pro": "not_found", "gemini-3.8-flash": "overloaded",
              "gemini-3.7-flash": "rate_limited"}
    chains = gd.build_chains(MODELS, status)
    assert "gemini-3.1-pro-preview" not in chains["pro"] and "gemini-2.5-pro" not in chains["pro"]
    assert chains["pro"][0] == "gemini-3.8-flash", "перегруженная модель остаётся первой: она живая, цепочка уйдёт ниже сама"
    assert "gemini-3.7-flash" in chains["pro"]


@pytest.mark.parametrize("code,body,expected", [
    (200, "", "ok"), (404, "", "not_found"), (403, "", "forbidden"), (401, "", "forbidden"),
    (503, "high demand", "overloaded"), (429, "quota exceeded, limit: 0, model: x", "quota_zero"),
    (429, '{"quotaValue": "0"}', "quota_zero"), (429, "retryDelay 30s limit: 20", "rate_limited"), (400, "bad", "unknown"),
])
def test_classify_response(code, body, expected):
    assert gd.classify_response(code, body) == expected


class FakeHttp:
    """Подмена httpx: список моделей + пробные запросы по таблице статусов."""

    def __init__(self, codes: dict[str, int] | None = None, fail_list: bool = False):
        self.codes = codes or {}
        self.fail_list = fail_list
        self.posts: list[str] = []

    def get(self, url, headers=None, timeout=None):
        if self.fail_list:
            raise httpx.ConnectError("no network")
        return httpx.Response(200, json={"data": [{"id": m} for m in MODELS]}, request=httpx.Request("GET", url))

    def post(self, url, headers=None, json=None, timeout=None):
        model = json["model"]
        self.posts.append(model)
        code = self.codes.get(model, 200)
        if code == -1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(code, text="limit: 0" if code == 429 else "", request=httpx.Request("POST", url))


def test_discover_probes_and_builds_chains():
    http = FakeHttp({"gemini-3.1-pro-preview": 429, "gemini-2.5-pro": 404, "gemini-3.8-flash": -1})
    result = gd.discover(["k1"], probe=True, http=http)
    assert result["status"]["gemini-3.1-pro-preview"] == "quota_zero"
    assert result["status"]["gemini-2.5-pro"] == "not_found"
    assert result["status"]["gemini-3.8-flash"] == "overloaded"
    assert "gemini-3.1-pro-preview" not in result["chains"]["pro"]
    assert len(http.posts) <= 3 * gd.MAX_PER_TIER


def test_discover_without_probe_makes_no_chat_requests():
    http = FakeHttp()
    result = gd.discover(["k1"], probe=False, http=http)
    assert http.posts == [] and result["chains"]["flash"][0] == "gemini-3.8-flash"


def test_discover_raises_when_list_unavailable_and_refresh_swallows(tmp_path):
    with pytest.raises(RuntimeError):
        gd.discover(["k1"], http=FakeHttp(fail_list=True))
    assert gd.refresh_if_stale(["k1"], data_dir=tmp_path, http=FakeHttp(fail_list=True)) is False
    assert gd.load_cache(tmp_path) is None


def test_refresh_is_cached_and_invalidated_by_key_change(tmp_path):
    http = FakeHttp()
    assert gd.refresh_if_stale(["k1"], data_dir=tmp_path, http=http) is True
    assert gd.refresh_if_stale(["k1"], data_dir=tmp_path, http=http) is False, "свежий кэш не обновляется"
    assert gd.refresh_if_stale(["other"], data_dir=tmp_path, http=http) is True, "смена ключа = новый подбор"
    cache = gd.load_cache(tmp_path)
    cache["fetched_at"] = time.time() - 13 * 3600
    gd.save_cache(cache, tmp_path)
    assert gd.refresh_if_stale(["other"], data_dir=tmp_path, http=http) is True, "кэш старше 12 ч обновляется"


def test_refresh_keeps_manual_probe_result(tmp_path):
    gd.save_cache(gd.discover(["k1"], probe=True, http=FakeHttp({"gemini-3.8-flash": 404})), tmp_path)
    cache = gd.load_cache(tmp_path)
    cache["fetched_at"] = 0
    gd.save_cache(cache, tmp_path)
    gd.refresh_if_stale(["k1"], data_dir=tmp_path, http=FakeHttp())
    assert "gemini-3.8-flash" not in gd.cached_chain("flash", tmp_path), "ручная проверка не должна теряться при автообновлении"


@pytest.fixture()
def cache_in_data_dir():
    path = gd.cache_path()
    yield path
    path.unlink(missing_ok=True)


def test_settings_use_cached_chains_only_when_auto_and_not_explicit(cache_in_data_dir):
    gd.save_cache(gd.discover(["k1"], http=FakeHttp()))
    auto = Settings(gemini_models_auto=True, gemini_flash_models="", gemini_pro_models="", compression_models="")
    assert auto.gemini_flash_chain[0] == "gemini-3.8-flash" and len(auto.gemini_flash_chain) > 1
    assert auto.gemini_pro_chain[0] == "gemini-3.1-pro-preview"
    assert auto.compression_chain[0] == "gemini-3.5-flash-lite"
    explicit = Settings(gemini_models_auto=True, gemini_flash_models="my-a,my-b", gemini_pro_models="my-c", compression_models="")
    assert explicit.gemini_flash_chain == ["my-a", "my-b"] and explicit.gemini_pro_chain == ["my-c"], "явная цепочка главнее"
    off = Settings(gemini_models_auto=False, gemini_flash_models="", gemini_pro_models="", compression_models="")
    assert off.gemini_flash_chain == [off.gemini_flash_model], "GEMINI_MODELS_AUTO=0 возвращает прежнее поведение"


def test_no_cache_means_old_single_model_behaviour():
    assert not gd.cache_path().exists()
    s = Settings(gemini_models_auto=True, gemini_flash_models="", gemini_pro_models="", compression_models="")
    assert s.gemini_flash_chain == [s.gemini_flash_model] and s.gemini_pro_chain == [s.gemini_pro_model]


def test_autoconfig_script_writes_env_without_touching_other_lines(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("gemini_autoconfig", paths._BACKEND_ROOT / "scripts" / "gemini_autoconfig.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEYS=secret-key-1,secret-key-2\nOTHER=1\nGEMINI_PRO_MODELS=old\n", encoding="utf-8")
    fake = FakeHttp({"gemini-3.1-pro-preview": 429})
    monkeypatch.setattr(gd, "httpx", type("H", (), {"get": staticmethod(fake.get), "post": staticmethod(fake.post),
                                                    "TimeoutException": httpx.TimeoutException, "TransportError": httpx.TransportError}))
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    rc = mod.main(["--env", str(env), "--write-env"])
    out = capsys.readouterr().out
    assert rc == 0 and "secret-key" not in out, "ключи не должны печататься"
    text = env.read_text(encoding="utf-8")
    assert "OTHER=1" in text and "GEMINI_API_KEYS=secret-key-1,secret-key-2" in text
    assert "GEMINI_PRO_MODELS=old" not in text and "GEMINI_FLASH_MODELS=gemini-3.8-flash" in text
    pro_line = next(l for l in text.splitlines() if l.startswith("GEMINI_PRO_MODELS="))
    assert "gemini-3.1-pro-preview" not in pro_line, "модель без квоты (429, limit: 0) не должна попасть в цепочку"
    assert (tmp_path / ".env.bak").exists() and json.loads((tmp_path / "gemini_models.json").read_text())["chains"]
