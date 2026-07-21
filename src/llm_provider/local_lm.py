from __future__ import annotations

import os
import json
import re

from .base import LLMProvider, LLMResponse, Message, Role, LLMError


class LocalLMProvider(LLMProvider):
    """
    Local LM server (Ollama / vLLM / llama.cpp) via the OpenAI-compatible
    endpoint. See .claude/skill/LOCAL_LM_API.md for the contract.

    Base URL defaults to http://localhost:11434/v1 and is overridable with
    LOCAL_LM_BASE_URL. Auth is effectively none, but we honour
    LOCAL_LM_API_KEY (any non-empty string works; OpenAI SDK requires one).
    """

    DEFAULT_MODEL = "qwen3.6-8k"
    DEFAULT_BASE_URL = "http://localhost:11434/v1"
    # Matches this repo's own reference server (see LOCAL_LM_API.md); a
    # different local server is very likely configured with a different
    # context window, hence LOCAL_LM_CONTEXT_WINDOW below rather than a
    # bare literal wherever this number is needed.
    DEFAULT_CONTEXT_WINDOW = 8192

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ):
        # Server auth is "none"; key is just a non-empty placeholder the SDK
        # needs. Fall back to "ollama" so the provider still works if the env
        # var is missing.
        self.api_key = api_key or os.getenv("LOCAL_LM_API_KEY", "") or "ollama"
        self.base_url = (
            base_url
            or os.getenv("LOCAL_LM_BASE_URL")
            or self.DEFAULT_BASE_URL
        )
        self.model_name = (
            model
            or os.getenv("LOCAL_LM_MODEL")
            or os.getenv("DEFAULT_LLM_MODEL")
            or self.DEFAULT_MODEL
        )
        self.context_window = int(
            os.getenv("LOCAL_LM_CONTEXT_WINDOW") or self.DEFAULT_CONTEXT_WINDOW
        )
        # Local serving is single-request; first token can lag tens of
        # seconds under contention / cold load (~9.4s). Keep timeout wide.
        self.timeout = float(os.getenv("LOCAL_LM_TIMEOUT", "120"))
        self._client = None

    @property
    def name(self) -> str:
        return "local_lm"

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            from openai import OpenAI  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError(
                "openai is not installed. Install it with: pip install openai"
            ) from exc

        self._client = OpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout,
        )
        return self._client

    def _to_openai_messages(self, messages: list[Message]) -> list[dict]:
        role_map = {
            Role.SYSTEM: "system",
            Role.USER: "user",
            Role.ASSISTANT: "assistant",
        }
        chat = [
            {"role": role_map[m.role], "content": m.content}
            for m in messages
            if m.content
        ]
        if not chat:
            raise LLMError("Local LM received empty messages.")
        return chat

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = False,
        reasoning_effort: str = "none",
    ) -> LLMResponse:
        client = self._get_client()
        selected_model = model or self.model_name
        chat = self._to_openai_messages(messages)
        # Default to the configured server's context window rather than a
        # fixed literal, so the truncation guard below stays accurate for
        # whatever server LOCAL_LM_BASE_URL actually points at.
        if max_tokens is None:
            max_tokens = self.context_window

        if json_mode:
            # response_format alone does not guarantee clean JSON on this
            # model; the prompt must also demand it (see LOCAL_LM_API.md §7).
            json_instruction = (
                "Return ONLY a valid JSON object. "
                "No markdown, no code fences, no explanation."
            )
            if chat[0]["role"] == "system":
                chat[0]["content"] = f"{chat[0]['content']}\n\n{json_instruction}"
            else:
                chat.insert(0, {"role": "system", "content": json_instruction})

        # reasoning_effort is non-standard; the OpenAI SDK passes it through
        # extra_body. "none" disables thinking -> pure output, low latency.
        extra_body: dict = {}
        if reasoning_effort:
            extra_body["reasoning_effort"] = reasoning_effort

        try:
            kwargs: dict = {
                "model": selected_model,
                "messages": chat,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            if extra_body:
                kwargs["extra_body"] = extra_body

            response = client.chat.completions.create(**kwargs)

        except Exception as exc:
            raise LLMError(f"Local LM API call failed: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        if choice is None:
            raise LLMError("Local LM returned no choices.")

        # Truncation: server silently cuts the *front* of over-long input and
        # marks finish_reason="length". Surface it instead of letting a
        # downstream json.loads fail with a cryptic message.
        if getattr(choice, "finish_reason", None) == "length":
            raise LLMError(
                f"模型輸出/輸入被截斷（max_tokens={max_tokens}，"
                f"context 上限 {self.context_window}）。"
                f"請把需求拆成多筆，或縮短輸入。"
            )

        text = (choice.message.content or "").strip()
        if not text:
            raise LLMError("Local LM returned empty text.")

        if json_mode:
            text = self._normalize_json_text(text)

        return LLMResponse(
            content=text,
            provider=self.name,
            model=selected_model,
            usage=self._extract_usage(response),
        )

    def _normalize_json_text(self, text: str) -> str:
        """Defensively strip code fences, then re-dump clean JSON if valid."""
        stripped = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
        try:
            parsed = json.loads(stripped)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            return stripped

    def _extract_usage(self, response) -> dict[str, int]:
        usage_obj = getattr(response, "usage", None)
        if not usage_obj:
            return {}
        return {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage_obj, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
        }
