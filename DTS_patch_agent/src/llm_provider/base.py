from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class ToolCall:
    """One tool_use block the model emitted (Anthropic function calling)."""
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    usage: dict[str, int]
    # --- tool use (optional; populated by complete_raw on providers that support it) ---
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None             # "tool_use" | "end_turn" | ...
    raw_blocks: list | None = None             # assistant content blocks, to append
                                               # verbatim to the message history


class LLMError(Exception):
    """Raised when an LLM provider fails."""


class LLMProvider:
    """Base interface for all LLM providers."""

    @property
    def name(self) -> str:
        raise NotImplementedError

    def is_available(self) -> bool:
        raise NotImplementedError

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> LLMResponse:
        raise NotImplementedError