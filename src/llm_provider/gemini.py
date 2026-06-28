from __future__ import annotations

import os
import json
from typing import Any

from .base import LLMProvider, LLMResponse, Message, Role, LLMError


class GeminiProvider(LLMProvider):
    """Google Gemini provider using the official google-genai SDK."""

    DEFAULT_MODEL = "gemini-2.5-pro"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.model_name = (
            model
            or os.getenv("GEMINI_MODEL")
            or os.getenv("DEFAULT_LLM_MODEL")
            or self.DEFAULT_MODEL
        )
        self._client = None

    @property
    def name(self) -> str:
        return "gemini"

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            from google import genai  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_client(self):
        if self._client is not None:
            return self._client

        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set.")

        try:
            from google import genai
        except ImportError as exc:
            raise LLMError(
                "google-genai is not installed. Install it with: pip install google-genai"
            ) from exc

        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _build_prompt(self, messages: list[Message]) -> str:
        """
        Convert internal messages into a single text prompt.

        This is simple and stable for your current agent use case.
        System messages are placed at the top.
        """
        parts: list[str] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                parts.append(f"[System]\n{msg.content}")
            elif msg.role == Role.USER:
                parts.append(f"[User]\n{msg.content}")
            elif msg.role == Role.ASSISTANT:
                parts.append(f"[Assistant]\n{msg.content}")

        return "\n\n".join(parts).strip()

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
        thinking_budget: int = 512,
    ) -> LLMResponse:
        client = self._get_client()
        selected_model = model or self.model_name
        prompt = self._build_prompt(messages)

        if not prompt:
            raise LLMError("Gemini received empty messages.")

        try:
            from google.genai import types

            config_kwargs: dict[str, Any] = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            }

            if json_mode:
                config_kwargs["response_mime_type"] = "application/json"

            # Gemini 2.5 是 thinking 模型：預設的動態 thinking 會吃掉 output
            # 預算，碰到大型請求（例如一次列出幾十個 signal）就把 JSON 截斷成
            # 無效字串。把 thinking 限在小預算 -> 保留 output 空間，且已驗證仍
            # 能正確解析（含 inline pin）。0 在 2.5-pro 不合法，故用正值。
            if thinking_budget and "2.5" in selected_model:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=thinking_budget
                )

            response = client.models.generate_content(
                model=selected_model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )

        except Exception as exc:
            raise LLMError(f"Gemini API call failed: {exc}") from exc

        # 截斷偵測：給明確、可行動的錯誤，而不是讓下游 json.loads 噴
        # 「Expecting property name…」這種難懂訊息。
        if self._finish_reason(response).upper().endswith("MAX_TOKENS"):
            raise LLMError(
                f"模型輸出被截斷（max_output_tokens={max_tokens} 不足）。"
                f"請把需求拆成多筆，或調高 max_tokens。"
            )

        text = getattr(response, "text", None)
        if not text:
            raise LLMError("Gemini returned empty text.")

        if json_mode:
            text = self._normalize_json_text(text)

        usage = self._extract_usage(response)

        return LLMResponse(
            content=text,
            provider=self.name,
            model=selected_model,
            usage=usage,
        )

    def _normalize_json_text(self, text: str) -> str:
        """
        Optional safety check for JSON mode.
        If Gemini returns valid JSON, re-dump it into clean JSON text.
        If not valid, keep original text.
        """
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            return text

    def _finish_reason(self, response) -> str:
        """Best-effort finish_reason of the first candidate (e.g. 'STOP',
        'MAX_TOKENS'); '' if unavailable."""
        try:
            return str(response.candidates[0].finish_reason or "")
        except Exception:
            return ""

    def _extract_usage(self, response) -> dict[str, int]:
        usage = {}

        metadata = getattr(response, "usage_metadata", None)
        if not metadata:
            return usage

        usage["prompt_tokens"] = getattr(metadata, "prompt_token_count", 0) or 0
        usage["completion_tokens"] = getattr(metadata, "candidates_token_count", 0) or 0
        usage["total_tokens"] = getattr(metadata, "total_token_count", 0) or 0

        return usage