"""llm/gemini_discovery.py — автоподбор цепочек моделей Gemini по тому, что реально отвечает вашему ключу.

Раньше цепочки (GEMINI_PRO_MODELS / GEMINI_FLASH_MODELS / COMPRESSION_MODELS) приходилось собирать руками
из списка API, и при пустых переменных все классы смотрели в одну модель: когда она перегружена, переключаться
некуда. Теперь:

  1. список моделей берётся из API (``GET <base>/models``);
  2. оставляются только текстовые чат-модели Gemini (без tts / image / live / embedding / veo / ... / gemma);
  3. по имени определяется ярус (pro / flash / lite) и версия; внутри яруса новые версии идут первыми;
  4. опционально (скрипт ``scripts/gemini_autoconfig.py``) каждая модель проверяется крошечным запросом:
     404/403 и «квота 0» исключаются, перегрузка и 429 остаются (модель живая, просто занята);
  5. результат лежит в ``<data_dir>/gemini_models.json``; ``config.Settings`` берёт цепочки оттуда, если
     в .env они не заданы явно (явная переменная всегда главнее).

0028: модель, не ответившая на проверку (перегрузка / 429 с квотой), не выбрасывается, а уходит в конец цепочки.
Переключение при отказе во время работы остаётся прежним (0020-0022): исчерпанная модель уходит в cooldown
по retryDelay Google, запрос идёт на следующую в цепочке. Цепочка идёт «вниз» по качеству:
  pro         = pro -> flash -> lite
  flash       = flash -> lite
  compression = lite -> flash   (сжатие дешёвое, не тратит квоту сильных моделей)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from aria import paths

logger = logging.getLogger("local_agent.gemini_discovery")

CACHE_NAME = "gemini_models.json"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
STALE_AFTER_SEC = 12 * 3600
MAX_CHAIN = 12
MAX_PER_TIER = 4
# Нижняя граница версии: старее в цепочки не попадают (1.5, 2.0 и т.п.).
MIN_VERSION = 2.5
# Версия алгоритма сборки цепочек. Кэш без неё/старой пересобирается сразу, а не через 12 часов.
ALGO = 2

# Не чат-модели и алиасы «latest» (их цель меняется, а квота и поведение непредсказуемы).
_EXCLUDE = re.compile(
    r"tts|image|live|native-audio|embedding|veo|lyria|robotics|computer-use|antigravity|deep-research|"
    r"aqa|transcribe|nano-banana|omni|customtools|translate|gemma|latest"
)
_VERSION = re.compile(r"^gemini-(\d+(?:\.\d+)?)-")

# Статусы проверки. Эти модели в цепочку не попадают.
DROP_STATUSES = frozenset({"not_found", "forbidden", "quota_zero"})
# 0028: живые, но сейчас занятые. Остаются в цепочке, но в самом конце: иначе каждый запрос сначала
# ждёт таймаут перегруженной модели и только потом уходит на рабочую (так упал смоук 12.10.2026).
BUSY_STATUSES = frozenset({"overloaded", "rate_limited", "unknown"})


@dataclass(frozen=True)
class Candidate:
    model: str
    tier: str  # pro | flash | lite
    version: float
    preview: bool

    @property
    def rank(self) -> tuple:
        return (-self.version, self.preview, self.model)


def strip_prefix(model_id: str) -> str:
    return model_id[len("models/"):] if model_id.startswith("models/") else model_id


def classify(model_id: str) -> Candidate | None:
    """Чат-модель Gemini -> Candidate, остальное (tts, image, live, embedding, ...) -> None."""
    name = strip_prefix(model_id).strip().lower()
    if not name.startswith("gemini-") or _EXCLUDE.search(name):
        return None
    if "flash-lite" in name:
        tier = "lite"
    elif "-flash" in name:
        tier = "flash"
    elif "-pro" in name:
        tier = "pro"
    else:
        return None
    match = _VERSION.match(name)
    if not match:
        return None
    return Candidate(model=name, tier=tier, version=float(match.group(1)), preview="preview" in name)


def select_candidates(
    models: list[str], max_per_tier: int = MAX_PER_TIER, min_version: float = MIN_VERSION
) -> dict[str, list[Candidate]]:
    """Лучшие ``max_per_tier`` моделей каждого яруса (новые версии первыми, без дублей, не старее ``min_version``)
    плюс «якорь» яруса: самая старая стабильная (не preview) модель не старее ``min_version``.

    Якорь нужен потому, что новые версии на бесплатном уровне чаще упираются в квоту, а проверенная старая
    (например, 2.5-flash) почти не даёт ошибок: без якоря срез «4 новейших» отрезал её ещё до проверки.
    """
    seen: set[str] = set()
    by_tier: dict[str, list[Candidate]] = {"pro": [], "flash": [], "lite": []}
    for cand in sorted(filter(None, (classify(m) for m in models)), key=lambda c: c.rank):
        if cand.model in seen or cand.version < min_version:
            continue
        seen.add(cand.model)
        by_tier[cand.tier].append(cand)
    out: dict[str, list[Candidate]] = {}
    for tier, items in by_tier.items():
        top = items[:max_per_tier]
        stable = [c for c in items if not c.preview]
        anchor = stable[-1] if stable else None  # самая старая стабильная (список идёт от новых к старым)
        if anchor is not None and anchor not in top:
            top = [*top, anchor]
        out[tier] = top
    return out


def anchors(models: list[str], min_version: float = MIN_VERSION) -> set[str]:
    """Якорные модели всех ярусов (их цепочка не обрезает)."""
    result: set[str] = set()
    for items in select_candidates(models, min_version=min_version).values():
        stable = [c for c in items if not c.preview]
        if stable:
            result.add(sorted(stable, key=lambda c: c.rank)[-1].model)
    return result


def build_chains(
    models: list[str], status: dict[str, str] | None = None, *, max_per_tier: int = MAX_PER_TIER, max_chain: int = MAX_CHAIN
) -> dict[str, list[str]]:
    """Цепочки по ярусам: pro -> flash -> lite; flash -> lite; compression -> lite -> flash."""
    status = status or {}
    tiers = select_candidates(models, max_per_tier)
    keep = anchors(models)
    alive = {
        tier: [c.model for c in items if status.get(c.model, "unprobed") not in DROP_STATUSES]
        for tier, items in tiers.items()
    }

    def _join(*parts: list[str]) -> list[str]:
        out: list[str] = []
        for part in parts:
            for model in part:
                if model not in out:
                    out.append(model)
        # 0028: рабочие вперёд (порядок качества внутри группы сохраняется), занятые в хвост, и только потом обрезка:
        # занятые модели не вытесняют рабочие из-за лимита длины.
        out.sort(key=lambda m: status.get(m, "unprobed") in BUSY_STATUSES)
        # при обрезке сначала выпадают обычные модели с хвоста, якоря остаются (иначе проверенная 2.5 отрезается)
        for model in reversed(list(out)):
            if len(out) <= max_chain:
                break
            if model not in keep:
                out.remove(model)
        return out[:max_chain]

    chains = {
        "pro": _join(alive["pro"], alive["flash"], alive["lite"]),
        "flash": _join(alive["flash"], alive["lite"]),
        "compression": _join(alive["lite"], alive["flash"]),
    }
    return {kind: chain for kind, chain in chains.items() if chain}


def classify_response(status_code: int, body: str) -> str:
    """Код ответа на пробный запрос -> статус модели."""
    if status_code == 200:
        return "ok"
    if status_code == 404:
        return "not_found"
    if status_code in (401, 403):
        return "forbidden"
    if status_code == 429:
        # «limit: 0» / quotaValue 0 = у этой модели на вашем уровне доступа нет квоты вообще (часто Pro на free tier)
        if re.search(r'limit:\s*0\b|"quotaValue":\s*"0"', body or ""):
            return "quota_zero"
        return "rate_limited"
    if status_code >= 500:
        return "overloaded"
    return "unknown"


def list_models(base_url: str, key: str, *, http: Any = None, timeout: float = 8.0) -> list[str]:
    http = http or httpx
    resp = http.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    resp.raise_for_status()
    return [strip_prefix(str(item.get("id", ""))) for item in resp.json().get("data", []) if item.get("id")]


def probe_model(base_url: str, key: str, model: str, *, http: Any = None, timeout: float = 25.0) -> tuple[str, str]:
    """Крошечный запрос (1 токен ответа). Возврат: (статус, пояснение)."""
    http = http or httpx
    try:
        resp = http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16},
            timeout=timeout,
        )
    except httpx.TimeoutException:
        return "overloaded", "timeout"
    except httpx.TransportError as exc:
        return "overloaded", type(exc).__name__
    return classify_response(resp.status_code, resp.text), f"HTTP {resp.status_code}"


def key_fingerprint(keys: list[str]) -> str:
    return hashlib.sha256((keys[0] if keys else "").encode()).hexdigest()[:12]


def discover(
    keys: list[str],
    base_url: str = DEFAULT_BASE_URL,
    *,
    probe: bool = False,
    max_per_tier: int = MAX_PER_TIER,
    http: Any = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Список моделей (+ проверка каждой при probe=True) -> результат для кэша. Ошибки сети поднимаются наружу."""
    last_exc: Exception | None = None
    models: list[str] = []
    good_key = ""
    for key in keys:
        try:
            models = list_models(base_url, key, http=http)
            good_key = key
            break
        except Exception as exc:  # noqa: BLE001 — пробуем следующий ключ
            last_exc = exc
    if not good_key:
        raise RuntimeError(f"model list unavailable with any key: {type(last_exc).__name__ if last_exc else 'no keys'}")

    status: dict[str, str] = {}
    detail: dict[str, str] = {}
    if probe:
        for tier, items in select_candidates(models, max_per_tier).items():
            for cand in items:
                st, info = probe_model(base_url, good_key, cand.model, http=http)
                status[cand.model], detail[cand.model] = st, info
                if progress:
                    progress(tier, cand.model, st, info)
    return {
        "fetched_at": time.time(),
        "algo": ALGO,
        "probed": probe,
        "key_id": key_fingerprint(keys),
        "status": status,
        "detail": detail,
        "chains": build_chains(models, status, max_per_tier=max_per_tier),
        "tiers": {t: [c.model for c in items] for t, items in select_candidates(models, max_per_tier).items()},
    }


# ── кэш ───────────────────────────────────────────────────────────────

def cache_path(data_dir: Path | None = None) -> Path:
    return (data_dir or paths.data_dir()) / CACHE_NAME


def save_cache(result: dict[str, Any], data_dir: Path | None = None) -> Path:
    path = cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_cache(data_dir: Path | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(cache_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cached_chain(kind: str, data_dir: Path | None = None) -> list[str]:
    """Цепочка из кэша (kind: pro | flash | compression) или [] — тогда работает прежняя одна модель."""
    cache = load_cache(data_dir)
    chain = (cache or {}).get("chains", {}).get(kind) if cache else None
    return [str(m) for m in chain] if isinstance(chain, list) else []


def refresh_if_stale(
    keys: list[str], base_url: str = DEFAULT_BASE_URL, *, max_age_sec: float = STALE_AFTER_SEC,
    data_dir: Path | None = None, http: Any = None,
) -> bool:
    """Обновляет кэш (только список моделей, без пробных запросов), если он старше max_age_sec или ключи сменились.

    Никогда не бросает: нет сети — остаётся прежний кэш или прежняя одна модель. True = кэш обновлён.
    """
    if not keys:
        return False
    cache = load_cache(data_dir)
    fresh = (
        cache is not None
        and cache.get("algo") == ALGO
        and cache.get("key_id") == key_fingerprint(keys)
        and time.time() - float(cache.get("fetched_at", 0)) < max_age_sec
    )
    if fresh:
        return False
    try:
        result = discover(keys, base_url, probe=False, http=http)
        # не затирать результат ручной проверки (probe) тем же набором без проверки, пока ключи те же
        if cache and cache.get("probed") and cache.get("key_id") == result["key_id"]:
            result["status"], result["detail"], result["probed"] = cache.get("status", {}), cache.get("detail", {}), True
            result["chains"] = build_chains(
                [m for ms in result["tiers"].values() for m in ms], result["status"]
            ) or result["chains"]
        save_cache(result, data_dir)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("gemini model discovery failed (%s); keeping previous chains", type(exc).__name__)
        return False
