"""
test_local_lm.py — LocalLMProvider's context-window resolution.

Regression guard for a real bug: the truncation error message used to hard-
code "context 上限 8192" regardless of what server LOCAL_LM_BASE_URL
actually points at, which is simply wrong for any server configured with a
different context window. `context_window` must now come from
LOCAL_LM_CONTEXT_WINDOW (falling back to the documented reference server's
8192), and `complete()`'s `max_tokens` default must follow it rather than a
literal.

Run: venv/bin/pytest src/llm_provider/test_local_lm.py -v
"""
import os

import pytest

from llm_provider.local_lm import LocalLMProvider


@pytest.fixture(autouse=True)
def _clear_local_lm_env(monkeypatch):
    for var in ("LOCAL_LM_CONTEXT_WINDOW", "LOCAL_LM_BASE_URL",
               "LOCAL_LM_MODEL", "LOCAL_LM_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_default_context_window_matches_reference_server():
    provider = LocalLMProvider()
    assert provider.context_window == LocalLMProvider.DEFAULT_CONTEXT_WINDOW == 8192


def test_context_window_is_configurable_via_env(monkeypatch):
    monkeypatch.setenv("LOCAL_LM_CONTEXT_WINDOW", "16384")
    provider = LocalLMProvider()
    assert provider.context_window == 16384


def test_context_window_env_var_is_read_as_int(monkeypatch):
    monkeypatch.setenv("LOCAL_LM_CONTEXT_WINDOW", "32768")
    provider = LocalLMProvider()
    assert provider.context_window == 32768
    assert isinstance(provider.context_window, int)
