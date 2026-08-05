from __future__ import annotations

from collections import deque

from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse, ToolCallRequest


class StubProvider(LlmProvider):
    """Детерминированный провайдер для §21.1/§21.2 unit/integration тестов и для
    прогона loop без реального LLM-ключа. НЕ используется в production —
    router.py подключает его только если явно передан в конструктор для тестов."""

    def __init__(self, provider_id: str = "stub", scripted_responses: list[LlmResponse] | None = None):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self._queue: deque[LlmResponse] = deque(scripted_responses or [])

    async def check_connectivity(self, timeout_sec: float) -> bool:
        return True

    async def chat(self, messages: list[ChatMessage], tools: list[dict], timeout_sec: float) -> LlmResponse:
        if self._queue:
            return self._queue.popleft()
        return LlmResponse(text="stub: no scripted response left, returning final answer.", tool_calls=[])

    def push(self, response: LlmResponse) -> None:
        self._queue.append(response)


def final_answer(text: str) -> LlmResponse:
    return LlmResponse(text=text, tool_calls=[])


def tool_call(tool_name: str, arguments: dict) -> LlmResponse:
    return LlmResponse(text=None, tool_calls=[ToolCallRequest(tool_name=tool_name, arguments=arguments)])
