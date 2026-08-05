from __future__ import annotations

import json

import httpx

from aria.llm.key_pool import KeyPool, NoAvailableKeys
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse, ToolCallRequest


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

        for _ in range(self._max_attempts()):
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
                    if status == 429:
                        self.key_pool.mark_rate_limited(key)
                        last_error = exc
                        continue
                    if status in (401, 403):
                        self.key_pool.mark_dead(key)
                        last_error = exc
                        continue
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
