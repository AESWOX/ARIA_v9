"""llm/groq_discovery.py — автоподбор цепочек моделей Groq по тому, что реально отвечает вашему пулу ключей.

Та же схема, что у ``gemini_discovery`` (0027/0028), вместо зашитого ID модели:

  1. список моделей берётся из API (``GET <base>/models``), ключи пробуются по очереди (401/сеть = следующий);
  2. оставляются только текстовые чат-модели (без whisper / guard / tts / embedding / compound ...);
  3. «сила» модели оценивается по размеру в имени (``70b``, ``8b``, ``120b``); без размера = средняя;
  4. опционально (``python -m aria.llm.groq_discovery --probe``) каждая модель проверяется крошечным запросом:
     404 / 403 / «снята с обслуживания» / квота 0 исключаются, перегрузка и 429 уходят в конец цепочки;
  5. результат лежит в ``<data_dir>/groq_models.json``; ``config.Settings`` берёт цепочки оттуда, если в .env
     нет ``GROQ_MODELS`` (явная переменная всегда главнее).

Цепочки:
  main = сильные -> слабые   (standard_reasoning, premium-fallback)
  fast = слабые -> сильные   (free_tier_reasoning, subagent_execution)

Важно: ранжирование по размеру — эвристика, а не знание о качестве. Порядок можно переопределить через GROQ_MODELS.
"""
from __future__ import annotations

import argparse
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
from aria.llm.gemini_discovery import BUSY_STATUSES, DROP_STATUSES, classify_response

logger = logging.getLogger("local_agent.groq_discovery")

CACHE_NAME = "groq_models.json"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
STALE_AFTER_SEC = 12 * 3600
MAX_CHAIN = 6
MAX_KEYS_TRIED = 3
UNKNOWN_SIZE = 50.0  # модель без размера в имени (kimi, ...) считается средней

# Не чат-модели: речь, модерация, эмбеддинги, агентные «compound» (сами вызывают тулы).
# allam — узкоспециализированная арабская модель: по размеру (7b) она встаёт первой в fast, но для общего чата не годится.
_EXCLUDE = re.compile(r"whisper|guard|safeguard|moderation|tts|playai|orpheus|embed|compound|transcri|speech|allam")
_SIZE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)b(?![a-z])")


@dataclass(frozen=True)
class Candidate:
    model: str
    size: float

    @property
    def rank(self) -> tuple:
        return (-self.size, self.model)


def classify(model_id: str) -> Candidate | None:
    """ID модели -> кандидат или None (не чат-модель)."""
    mid = (model_id or "").strip()
    if not mid or _EXCLUDE.search(mid.lower()):
        return None
    match = _SIZE.search(mid.lower())
    return Candidate(model=mid, size=float(match.group(1)) if match else UNKNOWN_SIZE)


def select_candidates(models: list[str]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for mid in models:
        cand = classify(mid)
        if cand and cand.model not in seen:
            seen.add(cand.model)
            out.append(cand)
    return sorted(out, key=lambda c: c.rank)  # сильные первыми


def classify_probe(status_code: int, body: str) -> str:
    """Как у Gemini, плюс особенности Groq: снятая модель отвечает 400, а не 404."""
    low = (body or "").lower()
    if status_code == 400 and ("decommission" in low or "model_not_found" in low or "does not exist" in low):
        return "not_found"
    if status_code == 429 and re.search(r"limit[^0-9]{0,12}0\b", low) and "per" not in low:
        return "quota_zero"
    return classify_response(status_code, body)


def build_chains(models: list[str], status: dict[str, str] | None = None, *, max_chain: int = MAX_CHAIN) -> dict[str, list[str]]:
    status = status or {}
    ranked = select_candidates(models)
    alive = [c for c in ranked if status.get(c.model, "ok") not in DROP_STATUSES]
    ready = [c for c in alive if status.get(c.model, "ok") not in BUSY_STATUSES]
    busy = [c for c in alive if status.get(c.model, "ok") in BUSY_STATUSES]
    main = [c.model for c in ready + busy][:max_chain]  # сильные первыми, занятые в конец
    fast_ready = sorted(ready, key=lambda c: (c.size, c.model))
    fast_busy = sorted(busy, key=lambda c: (c.size, c.model))
    fast = [c.model for c in fast_ready + fast_busy][:max_chain]
    chains = {"main": main, "fast": fast}
    return {k: v for k, v in chains.items() if v}


def list_models(base_url: str, key: str, *, http: Any = None, timeout: float = 8.0) -> list[str]:
    http = http or httpx
    resp = http.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    resp.raise_for_status()
    return [str(item["id"]) for item in resp.json().get("data", []) if item.get("id")]


def probe_model(base_url: str, key: str, model: str, *, http: Any = None, timeout: float = 25.0) -> tuple[str, str]:
    """Крошечный запрос. Возврат: (статус, пояснение)."""
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
    return classify_probe(resp.status_code, resp.text), f"HTTP {resp.status_code}"


def key_fingerprint(keys: list[str]) -> str:
    return hashlib.sha256((keys[0] if keys else "").encode()).hexdigest()[:12]


def discover(
    keys: list[str], base_url: str = DEFAULT_BASE_URL, *, probe: bool = False,
    max_chain: int = MAX_CHAIN, http: Any = None, progress: Any = None,
) -> dict[str, Any]:
    """Список моделей (+ проверка при probe=True) -> результат для кэша. Ошибки сети поднимаются наружу."""
    last_exc: Exception | None = None
    models: list[str] = []
    good_key = ""
    for key in keys[:MAX_KEYS_TRIED]:
        try:
            models = list_models(base_url, key, http=http)
            good_key = key
            break
        except Exception as exc:  # noqa: BLE001 — следующий ключ
            last_exc = exc
    if not good_key:
        raise RuntimeError(f"model list unavailable with {min(len(keys), MAX_KEYS_TRIED)} key(s): "
                           f"{type(last_exc).__name__ if last_exc else 'no keys'}")

    status: dict[str, str] = {}
    detail: dict[str, str] = {}
    candidates = select_candidates(models)
    if probe:
        for cand in candidates:
            st, info = probe_model(base_url, good_key, cand.model, http=http)
            status[cand.model], detail[cand.model] = st, info
            if progress:
                progress(cand.model, st, info)
    return {
        "fetched_at": time.time(),
        "probed": probe,
        "key_id": key_fingerprint(keys),
        "status": status,
        "detail": detail,
        "models": [c.model for c in candidates],
        "chains": build_chains(models, status, max_chain=max_chain),
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
    """Цепочка из кэша (kind: main | fast) или [] — тогда работает одна модель из GROQ_MODEL."""
    cache = load_cache(data_dir)
    chain = (cache or {}).get("chains", {}).get(kind)
    return [str(m) for m in chain] if isinstance(chain, list) else []


def refresh_if_stale(
    keys: list[str], base_url: str = DEFAULT_BASE_URL, *, max_age_sec: float = STALE_AFTER_SEC,
    data_dir: Path | None = None, http: Any = None,
) -> bool:
    """Обновляет кэш (только список, без проб), если он старше max_age_sec или ключи сменились. Не бросает."""
    if not keys:
        return False
    cache = load_cache(data_dir)
    if (
        cache is not None
        and cache.get("key_id") == key_fingerprint(keys)
        and time.time() - float(cache.get("fetched_at", 0)) < max_age_sec
    ):
        return False
    try:
        result = discover(keys, base_url, probe=False, http=http)
        # не затирать результат ручной проверки тем же набором без проверки, пока ключи те же
        if cache and cache.get("probed") and cache.get("key_id") == result["key_id"]:
            result["status"], result["detail"], result["probed"] = cache.get("status", {}), cache.get("detail", {}), True
            result["chains"] = build_chains(result["models"], result["status"]) or result["chains"]
        save_cache(result, data_dir)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("groq model discovery failed (%s); keeping previous chains", type(exc).__name__)
        return False


def main(argv: list[str] | None = None) -> int:
    """python -m aria.llm.groq_discovery [--probe]: показать список и обновить кэш."""
    try:  # как scripts/live_smoke.py: ключи из backend/.env, если они не в окружении
        from dotenv import load_dotenv

        load_dotenv(paths._BACKEND_ROOT / ".env")
    except ImportError:
        pass
    from aria.config import get_settings

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true", help="проверить каждую модель запросом на 16 токенов")
    args = ap.parse_args(argv)
    settings = get_settings()
    keys = settings.groq_api_keys_list
    if not keys:
        print("GROQ_API_KEYS не заданы")
        return 1
    result = discover(
        keys, settings.groq_base_url, probe=args.probe,
        progress=lambda m, st, info: print(f"  {m:55s} {st:12s} {info}"),
    )
    path = save_cache(result)
    print(f"моделей-кандидатов: {len(result['models'])}; кэш: {path}")
    for kind, chain in result["chains"].items():
        print(f"  {kind}: {' -> '.join(chain)}")
    return 0 if result["chains"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
