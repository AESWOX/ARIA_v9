from __future__ import annotations

import asyncio
import json

import httpx

from aria.llm.key_pool import KeyPool, NoAvailableKeys
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse, ToolCallRequest


FORBIDDEN_COOLDOWN_SEC = 15 * 60
DAILY_QUOTA_COOLDOWN_SEC = 60 * 60

_INVALID_KEY_MARKERS = (
    "api_key_invalid",
    "api key not valid",
    "api key expired",
    "invalid api key",
    "incorrect api key",
    "api key was reported as leaked",
    "reported as leaked",
    "api key has been revoked",
)


def _error_text(resp: httpx.Response) -> str:
    try:
        return resp.text.lower()
    except Exception:  # streaming/unread body
        return ""


def _is_invalid_key(body_lower: str) -> bool:
    return any(marker in body_lower for marker in _INVALID_KEY_MARKERS)


def _cooldown_for_429(resp: httpx.Response, body_lower: str, default: float) -> float:
    """Retry-After wins; a per-day quota backs off long; otherwise the default."""
    retry_after = resp.headers.get("retry-after")
    if retry_after:
        try:
            return max(default, float(retry_after))
        except ValueError:
            pass
    if "perday" in body_lower.replace(" ", "").replace("_", "") or "per day" in body_lower:
        return max(default, DAILY_QUOTA_COOLDOWN_SEC)
    return default


class OpenAICompatibleProvider(LlmProvider):
    """Общий клиент для любого /v1/chat/completions-совместимого backend'а:
    DeepSeek, Gemini (openai-compat endpoint), Groq и т.д. —
    отличаются только base_url/model и набором ключей.

    Ключи можно передать двумя способами:
      - api_key=str          -> старое поведение, один статичный ключ (DeepSeek).
      - key_pool=KeyPool(...) -> ротация по кругу с обработкой 429/401/403.
    Если задано и то и другое, используется key_pool.
    """

    def __init__(
        self,
        provider_id: str,
        provider_class: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        key_pool: KeyPool | None = None,
    ):
        self.provider_id = provider_id
        self.provider_class = provider_class
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.key_pool = key_pool

    def _headers(self, key: str | None) -> dict:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _max_attempts(self) -> int:
        return len(self.key_pool) if self.key_pool else 1

    def _current_key(self) -> str | None:
        if self.key_pool:
            return self.key_pool.next_key()
        return self.api_key

    async def check_connectivity(self, timeout_sec: float) -> bool:
        """§12.3: DNS/HTTP check timeout 2 сек (default), без бесконечного перебора.
        С пулом ключей: пробуем текущий ключ пула, не гоняем весь пул на health-check
        (это делает route_chat -> chat() при реальном вызове)."""
        try:
            key = self.api_key
            if self.key_pool:
                try:
                    key = self.key_pool.next_key()
                except NoAvailableKeys:
                    return False
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers(key))
                return resp.status_code < 500
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            return False

    async def list_models(self, timeout_sec: float = 10) -> list[dict]:
        key = self.api_key
        if self.key_pool:
            try:
                key = self.key_pool.next_key()
            except NoAvailableKeys:
                return []

        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            resp = await client.get(f"{self.base_url}/models", headers=self._headers(key))
            resp.raise_for_status()
            data = resp.json()

        raw_items = data.get("data") or data.get("models") or []
        normalized: list[dict] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            pricing = item.get("pricing") or {}
            prompt_price = pricing.get("prompt")
            completion_price = pricing.get("completion")
            is_free_tier = None
            if prompt_price is not None and completion_price is not None:
                is_free_tier = str(prompt_price) == "0" and str(completion_price) == "0"
            normalized.append(
                {
                    "model_id": item.get("id") or item.get("name") or self.model,
                    "context_window": item.get("context_window") or item.get("context_length") or item.get("max_context_length"),
                    "is_free_tier": is_free_tier,
                    "price_prompt_usd": prompt_price,
                    "price_completion_usd": completion_price,
                    "raw": item,
                }
            )
        return normalized

    async def chat(self, messages: list[ChatMessage], tools: list[dict], timeout_sec: float) -> LlmResponse:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if tools:
            payload["tools"] = tools

        last_error: Exception | None = None
        _404_attempts = 0
        max_404_retries = 4

        for _ in range(max(self._max_attempts(), max_404_retries)):
            try:
                key = self._current_key()
            except NoAvailableKeys as exc:
                last_error = exc
                break

            try:
                async with httpx.AsyncClient(timeout=timeout_sec) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions", headers=self._headers(key), json=payload
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if self.key_pool and key is not None:
                    body = _error_text(exc.response)
                    if status == 429:
                        self.key_pool.mark_rate_limited(key, _cooldown_for_429(exc.response, body, self.key_pool.cooldown_sec))
                        last_error = exc
                        continue
                    if status == 401 or (status in (400, 403) and _is_invalid_key(body)):
                        # the key itself is bad/revoked/leaked: never retry it
                        self.key_pool.mark_dead(key)
                        last_error = exc
                        continue
                    if status == 403:
                        # 403 that is NOT about the key (model not enabled for
                        # this project/plan, region, billing): the key is fine
                        # for other models, so back off instead of killing it
                        # for the whole process lifetime.
                        self.key_pool.mark_rate_limited(key, FORBIDDEN_COOLDOWN_SEC)
                        last_error = exc
                        continue
                    if status == 404:
                        # Провайдер отдаёт 404 на ключ без доступа к модели
                        # (напр. Gemini "model no longer available to new users").
                        # Транзиентная ошибка: прокси сам ротирует upstream-ключи,
                        # поэтому не сажаем ключ в cooldown, а делаем короткий retry.
                        _404_attempts += 1
                        last_error = exc
                        if _404_attempts < max_404_retries:
                            await asyncio.sleep(0.7)
                            continue
                        raise
                raise
            else:
                return self._parse_response(data)

        raise last_error or NoAvailableKeys(f"provider '{self.provider_id}': no usable key")

    def _parse_response(self, data: dict) -> LlmResponse:
        choice = data["choices"][0]["message"]
        raw_tool_calls = choice.get("tool_calls") or []
        tool_calls = []
        for call in raw_tool_calls:
            fn = call.get("function", {})
            args_raw = fn.get("arguments", "{}")
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCallRequest(tool_name=fn.get("name", ""), arguments=args))

        return LlmResponse(
            text=choice.get("content"),
            tool_calls=tool_calls,
            raw=data,
            usage=data.get("usage", {}),
        )
