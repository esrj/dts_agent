from __future__ import annotations

import os
from configparser import ConfigParser

# this file: <root>/src/llm_provider/config.py  ->  root is 3 levels up
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_CONFIG_PATH = os.path.join(_ROOT, "llm_modules.ini")


def _config_path() -> str:
    """Location of the module->LLM config file (override via LLM_CONFIG_FILE)."""
    return os.getenv("LLM_CONFIG_FILE") or _DEFAULT_CONFIG_PATH


def module_config(module: str) -> dict[str, str | None]:
    """
    Resolve the provider/model for a module from llm_modules.ini.

    Returns {"provider": ..., "model": ...}. Values fall back to the
    [default] section, then to None (which lets the factory/provider pick
    their own defaults). A missing config file yields all-None, so callers
    keep working without it.
    """
    result: dict[str, str | None] = {"provider": None, "model": None}

    path = _config_path()
    if not os.path.exists(path):
        return result

    parser = ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except OSError:
        return result

    for section in ("default", module):
        if parser.has_section(section):
            for key in ("provider", "model"):
                value = parser.get(section, key, fallback="").strip()
                if value:
                    result[key] = value

    return result
