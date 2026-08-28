from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ChatMessage:
    role: str
    content: str


@dataclass(slots=True)
class ToolCallRequest:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LlmResponse:
    text: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class LlmProvider(ABC):
    provider_id: str
    provider_class: str

    @abstractmethod
    async def check_connectivity(self, timeout_sec: float) -> bool: ...

    @abstractmethod
    async def chat(self, messages: list[ChatMessage], tools: list[dict], timeout_sec: float) -> LlmResponse: ...

    async def list_models(self, timeout_sec: float = 10) -> list[dict[str, Any]]:
        return []
