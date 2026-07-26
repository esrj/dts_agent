from __future__ import annotations

import json
import re
import time

from llm_provider import LLMError, LLMProvider, Message, Role

# 供應商暫時性錯誤(過載/限流)的訊息特徵:批次跑一半碰到就退避重試,不放棄
_TRANSIENT_MARKERS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "rate limit")


def _complete_with_backoff(provider: LLMProvider, messages, *, max_tokens: int):
    delay = 10.0
    for attempt in range(5):
        try:
            return provider.complete(
                messages, temperature=0.0, max_tokens=max_tokens, json_mode=True
            )
        except LLMError as exc:
            text = str(exc)
            if attempt == 4 or not any(m in text for m in _TRANSIENT_MARKERS):
                raise
            time.sleep(delay)
            delay *= 2


def parse_json_object(text: str):
    """
    從 LLM 回覆中挖出第一個 JSON 物件(容忍 markdown fence / 前後贅文)。
    失敗丟 ValueError。
    """
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in LLM reply")
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in LLM reply: {exc}") from exc


def complete_json(
    provider: LLMProvider,
    system: str,
    user: str,
    *,
    max_tokens: int = 16384,
    retries: int = 1,
):
    """呼叫 provider 並解析 JSON 物件;解析失敗時帶錯誤訊息重試 retries 次。"""
    messages = [
        Message(role=Role.SYSTEM, content=system),
        Message(role=Role.USER, content=user),
    ]
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        resp = _complete_with_backoff(provider, messages, max_tokens=max_tokens)
        try:
            return parse_json_object(resp.content)
        except ValueError as exc:
            last_err = exc
            messages = [
                Message(role=Role.SYSTEM, content=system),
                Message(role=Role.USER, content=user),
                Message(role=Role.ASSISTANT, content=resp.content[:4000]),
                Message(
                    role=Role.USER,
                    content=(
                        f"Your previous reply was not valid JSON ({exc}). "
                        "Reply again with ONLY the JSON object, nothing else."
                    ),
                ),
            ]
    raise ValueError(f"LLM JSON parse failed after {retries + 1} attempts: {last_err}")
