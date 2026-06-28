from __future__ import annotations

import os

from .base import LLMProvider, LLMResponse, Message
from .gemini import GeminiProvider
from .anthropic import AnthropicProvider
from .local_lm import LocalLMProvider
from .config import module_config


def _load_env_file() -> None:
    """
    Load KEY=VALUE pairs from the project-root .env into os.environ.

    Existing environment variables are never overwritten. This keeps the
    package usable without requiring python-dotenv to be installed.
    """
    # this file: <root>/src/llm_provider/factory.py  ->  root is 3 levels up
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    env_path = os.path.join(root, ".env")
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


class MockProvider(LLMProvider):
    """Simple offline provider for testing the agent pipeline without API keys."""

    @property
    def name(self) -> str:
        return "mock"

    def is_available(self) -> bool:
        return True

    def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> LLMResponse:
        if json_mode:
            content = '{"ok": true, "provider": "mock"}'
        else:
            content = "Mock response: LLM provider pipeline is working."

        return LLMResponse(
            content=content,
            provider=self.name,
            model=model or "mock",
            usage={},
        )


def get_provider(
    name: str | None = None,
    *,
    model: str | None = None,
    module: str | None = None,
    allow_mock: bool = True,
) -> LLMProvider:
    """
    Create an LLM provider.

    name:
      - "gemini"
      - "anthropic"
      - "auto"
      - "mock"

    module:
      A module name looked up in llm_modules.ini (e.g. "parse",
      "dts_patch"). Its provider/model become the defaults, so different
      modules can run on different LLM tiers. Explicit `name`/`model`
      arguments always win over the config file.

    Resolution order for both provider and model:
      explicit arg  ->  llm_modules.ini[module]  ->  env var  ->  "auto"/provider default
    """

    _load_env_file()

    if module is not None:
        cfg = module_config(module)
        if name is None:
            name = cfg["provider"]
        if model is None:
            model = cfg["model"]

    provider_name = (name or os.getenv("LLM_PROVIDER") or "auto").lower().strip()

    if provider_name == "gemini":
        provider = GeminiProvider(model=model)
        if not provider.is_available():
            raise RuntimeError(
                "Gemini provider is not available. "
                "Check GEMINI_API_KEY and install: pip install google-genai"
            )
        return provider

    if provider_name == "anthropic":
        provider = AnthropicProvider(model=model)
        if not provider.is_available():
            raise RuntimeError(
                "Anthropic provider is not available. "
                "Check ANTHROPIC_API_KEY and install: pip install anthropic"
            )
        return provider

    if provider_name == "local_lm":
        provider = LocalLMProvider(model=model)
        if not provider.is_available():
            raise RuntimeError(
                "Local LM provider is not available. "
                "Check the local server / LOCAL_LM_API_KEY and install: pip install openai"
            )
        return provider

    if provider_name == "mock":
        return MockProvider()

    if provider_name != "auto":
        raise ValueError(
            f"Unknown LLM provider: {provider_name}. "
            "Use one of: auto, gemini, anthropic, local_lm, mock."
        )

    providers: list[LLMProvider] = [
        GeminiProvider(model=model),
        AnthropicProvider(model=model),
        LocalLMProvider(model=model),
    ]

    for provider in providers:
        if provider.is_available():
            return provider

    if allow_mock:
        return MockProvider()

    raise RuntimeError(
        "No LLM provider is available. "
        "Set GEMINI_API_KEY or ANTHROPIC_API_KEY, and install google-genai or anthropic."
    )