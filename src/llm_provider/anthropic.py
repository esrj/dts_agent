from __future__ import annotations

import os
import json

from .base import LLMProvider, LLMResponse, Message, Role, ToolCall, LLMError


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider using the official anthropic SDK."""

    DEFAULT_MODEL = "claude-opus-4-8"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model_name = (
            model
            or os.getenv("ANTHROPIC_MODEL")
            or os.getenv("DEFAULT_LLM_MODEL")
            or self.DEFAULT_MODEL
        )
        self._client = None

    @property
    def name(self) -> str:
        return "anthropic"

    def is_available(self) -> bool:
        if not self.api_key:
            return False
        try:
            import anthropic  # noqa: F401  (the SDK, not this module)
            return True
        except ImportError:
            return False

    def _get_client(self):
        if self._client is not None:
            return self._client

        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")

        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise LLMError(
                "anthropic is not installed. Install it with: pip install anthropic"
            ) from exc

        self._client = Anthropic(api_key=self.api_key)
        return self._client

    def _split_messages(self, messages: list[Message]) -> tuple[str | None, list[dict]]:
        """
        Anthropic has a separate system parameter.
        User / assistant messages go into messages=[].
        """
        system_parts: list[str] = []
        chat_messages: list[dict] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_parts.append(msg.content)
            elif msg.role == Role.USER:
                chat_messages.append({"role": "user", "content": msg.content})
            elif msg.role == Role.ASSISTANT:
                chat_messages.append({"role": "assistant", "content": msg.content})

        system_prompt = "\n\n".join(system_parts).strip() or None

        if not chat_messages:
            chat_messages.append({"role": "user", "content": system_prompt or ""})

        return system_prompt, chat_messages

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> LLMResponse:
        client = self._get_client()
        selected_model = model or self.model_name

        system_prompt, chat_messages = self._split_messages(messages)

        if json_mode:
            json_instruction = (
                "Return only valid JSON. Do not include markdown fences or explanation."
            )
            if system_prompt:
                system_prompt = f"{system_prompt}\n\n{json_instruction}"
            else:
                system_prompt = json_instruction

        kwargs = {
            "model": selected_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = self._create(client, kwargs)

        text = self._extract_text(response)

        if not text:
            raise LLMError("Anthropic returned empty text.")

        if json_mode:
            text = self._normalize_json_text(text)

        usage = self._extract_usage(response)

        return LLMResponse(
            content=text,
            provider=self.name,
            model=selected_model,
            usage=usage,
        )

    def complete_raw(
        self,
        *,
        system: str | None = None,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
    ) -> LLMResponse:
        """Low-level call that speaks the native Anthropic message format.

        Unlike `complete()`, `messages` is a list of raw Anthropic message dicts
        (so `content` may be a string OR a list of content blocks, including
        tool_use / tool_result). This is what the orchestrator's agentic loop
        needs: the model may emit `tool_use` blocks, we run the tools, append
        `tool_result` blocks, and call again — the whole transcript stays in the
        native shape, never squeezed through Message(str).

        Returns an LLMResponse whose:
          - content      = concatenated text blocks (the model's prose this turn)
          - tool_calls   = [ToolCall(id, name, input)] for each tool_use block
          - stop_reason  = "tool_use" when the model wants a tool run, else "end_turn"
          - raw_blocks   = the assistant content blocks to append verbatim to the
                           history before sending tool_result back.
        """
        client = self._get_client()
        selected_model = model or self.model_name

        kwargs: dict = {
            "model": selected_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        response = self._create(client, kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_blocks: list[dict] = []
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                txt = getattr(block, "text", "") or ""
                text_parts.append(txt)
                raw_blocks.append({"type": "text", "text": txt})
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id, name=block.name, input=dict(block.input or {})))
                raw_blocks.append({
                    "type": "tool_use", "id": block.id,
                    "name": block.name, "input": dict(block.input or {})})

        return LLMResponse(
            content="".join(text_parts).strip(),
            provider=self.name,
            model=selected_model,
            usage=self._extract_usage(response),
            tool_calls=tool_calls,
            stop_reason=getattr(response, "stop_reason", None),
            raw_blocks=raw_blocks,
        )

    # Sampling knobs a model might reject ("X is deprecated/unsupported for this model").
    _STRIPPABLE = ("temperature", "top_p", "top_k")

    def _create(self, client, kwargs: dict):
        """Call the model, progressively stripping any sampling param it rejects.

        We always STREAM (messages.stream + get_final_message) rather than the
        plain messages.create: the SDK refuses a *non-streaming* request whose
        max_tokens is large enough to risk a >10-minute response ("Streaming is
        required for operations that may take longer than 10 minutes"), and the
        knowledge_extract extraction prompts routinely ask for tens of thousands
        of tokens (af/profiles use max_tokens=32768). Streaming accumulates the
        same final Message object, so callers (_extract_text / _extract_usage /
        complete_raw and its tool_use blocks) are unchanged.（自 extractor fork
        porting，llm_provider 統一時併入——EXTRACTOR_MERGE_PLAN D2。）

        Newer models (e.g. claude-opus-4-8) reject `temperature` outright with a
        400 'temperature is deprecated for this model'. Rather than hard-code which
        model accepts which knob, we drop the offending param named in the error
        and retry — looping so that if a second param is then rejected it is
        stripped too. We only ever remove params in `_STRIPPABLE`; any other error
        is raised immediately, so a real failure is never masked.
        """
        attempt = dict(kwargs)
        while True:
            try:
                with client.messages.stream(**attempt) as stream:
                    return stream.get_final_message()
            except Exception as exc:
                msg = str(exc).lower()
                victim = next((p for p in self._STRIPPABLE
                               if p in msg and p in attempt), None)
                if victim is None:
                    raise LLMError(f"Anthropic API call failed: {exc}") from exc
                attempt.pop(victim)

    def _extract_text(self, response) -> str:
        parts: list[str] = []

        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")

        return "".join(parts).strip()

    def _normalize_json_text(self, text: str) -> str:
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            return text

    def _extract_usage(self, response) -> dict[str, int]:
        usage_obj = getattr(response, "usage", None)
        if not usage_obj:
            return {}

        input_tokens = getattr(usage_obj, "input_tokens", 0) or 0
        output_tokens = getattr(usage_obj, "output_tokens", 0) or 0

        return {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }