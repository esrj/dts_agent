from __future__ import annotations

import os
import re
from configparser import ConfigParser

from llm_provider import LLMProvider

from .jsonutil import complete_json
from .paths import BOARDS_INI, LIVE_DATA_DIR

_IDENTIFY_SYSTEM = """\
You identify which SoC / board a hardware reference manual (datasheet) describes.

Reply with ONLY a JSON object:
{
  "vendor": "<chip vendor, e.g. STMicroelectronics / Texas Instruments / Nuvoton>",
  "soc": "<the most specific SoC part name the manual covers, e.g. STM32MP257F / AM6548 / MA35D1>",
  "board_slug": "<lowercase kebab-case directory name for the knowledge base>",
  "confidence": "high" | "medium" | "low"
}

board_slug rules:
- If one of the EXISTING BOARD DIRECTORIES clearly corresponds to this manual's
  SoC family, reuse that directory name exactly.
- Otherwise derive a slug from the SoC name: lowercase, alphanumeric and dashes
  only (e.g. "am6548", "ma35d1").
"""

_HEADER = (
    "# input/<pdf> -> data/<board>/ 對應表([boards]),board -> SoC 名快取([soc]),\n"
    "# board -> baseline 板 .dts 檔名([baseline])。\n"
    "# 由 knowledge_extract 自動辨識後寫入;人工修改後以此檔為準。\n"
)


def _read_ini() -> ConfigParser:
    parser = ConfigParser()
    parser.optionxform = str  # PDF 檔名大小寫有意義,不轉小寫
    if os.path.exists(BOARDS_INI):
        parser.read(BOARDS_INI, encoding="utf-8")
    return parser


def _write_ini(parser: ConfigParser) -> None:
    with open(BOARDS_INI, "w", encoding="utf-8") as f:
        f.write(_HEADER)
        parser.write(f)


def save_board_entry(pdf_name: str, board: str, soc: str | None = None) -> None:
    parser = _read_ini()
    for section in ("boards", "soc"):
        if not parser.has_section(section):
            parser.add_section(section)
    parser.set("boards", pdf_name, board)
    if soc:
        parser.set("soc", board, soc)
    _write_ini(parser)


def get_soc(board: str) -> str | None:
    parser = _read_ini()
    return (parser.get("soc", board, fallback=None)
            if parser.has_section("soc") else None)


def get_baseline(board: str) -> str | None:
    parser = _read_ini()
    return (parser.get("baseline", board, fallback=None)
            if parser.has_section("baseline") else None)


def save_baseline(board: str, dts_name: str) -> None:
    parser = _read_ini()
    if not parser.has_section("baseline"):
        parser.add_section("baseline")
    parser.set("baseline", board, dts_name)
    _write_ini(parser)


def resolve_board(
    pdf_name: str, pages: list[str], provider: LLMProvider
) -> tuple[str, str]:
    """
    回傳 (board_slug, soc)。boards.ini 有登錄就直接用(零 LLM 呼叫);沒有就用
    手冊前幾頁讓 LLM 辨識,並回寫 boards.ini 供之後的執行與人工修正。
    """
    parser = _read_ini()
    board = (
        parser.get("boards", pdf_name, fallback=None)
        if parser.has_section("boards")
        else None
    )
    if board:
        soc = (
            parser.get("soc", board, fallback=None)
            if parser.has_section("soc")
            else None
        )
        if soc:
            return board, soc

    existing = sorted(
        d
        for d in (os.listdir(LIVE_DATA_DIR) if os.path.isdir(LIVE_DATA_DIR) else [])
        if os.path.isdir(os.path.join(LIVE_DATA_DIR, d))
    )
    head = "\n\n--- PAGE BREAK ---\n\n".join(pages[:5])[:15000]
    result = complete_json(
        provider,
        _IDENTIFY_SYSTEM,
        f"EXISTING BOARD DIRECTORIES: {existing}\n\n"
        f"MANUAL FILE NAME: {pdf_name}\n\n"
        f"FIRST PAGES OF THE MANUAL:\n{head}",
        max_tokens=1024,
    )
    soc = str(result.get("soc") or "").strip() or pdf_name

    if not board:
        board = re.sub(
            r"[^a-z0-9-]+", "-", str(result.get("board_slug") or "").lower()
        ).strip("-")
        if not board:
            board = re.sub(r"[^a-z0-9-]+", "-", soc.lower()).strip("-") or "unknown"

    save_board_entry(pdf_name, board, soc)
    return board, soc
