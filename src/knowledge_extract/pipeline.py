from __future__ import annotations

import json
import os

from llm_provider import LLMProvider

from . import derive, dts_extract, extract_af, identify, locate, pdf_text, profiles
from .paths import INPUT_DIR, base_dir


def _write_json(path: str, obj, force: bool) -> str:
    """回傳 'written' | 'skipped'(已存在且未 --force 時不動手工維護檔)。"""
    if os.path.exists(path) and not force:
        return "skipped"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return "written"


def output_files(board: str, steps: set[str], out_dir: str | None = None) -> list[str]:
    """選定步驟對應的輸出檔路徑(用來判斷該手冊是否已處理完)。"""
    names: list[str] = []
    if "af" in steps:
        names += ["af_table.json", "all_peripheral.json"]
    if "profiles" in steps:
        names.append("peripheral_profiles.json")
    if "require" in steps:
        names.append("require.json")
    base = base_dir(board, out_dir)
    paths = [os.path.join(base, n) for n in names]
    if "dts" in steps:
        paths += dts_extract.output_files(board, out_dir)
    return paths


def is_complete(
    pdf_name: str,
    provider: LLMProvider,
    *,
    steps: set[str],
    out_dir: str | None = None,
) -> bool:
    """該手冊選定步驟的產出是否已齊全(板名經 boards.ini 快取,重跑零 LLM 呼叫)。"""
    pages = pdf_text.extract_pages(os.path.join(INPUT_DIR, pdf_name))
    board, _ = identify.resolve_board(pdf_name, pages, provider)
    return all(os.path.exists(p) for p in output_files(board, steps, out_dir))


def list_manuals() -> list[str]:
    if not os.path.isdir(INPUT_DIR):
        return []
    return sorted(
        f for f in os.listdir(INPUT_DIR) if f.lower().endswith(".pdf")
    )


def has_dts_input() -> bool:
    return dts_extract.find_dts_dir(INPUT_DIR) is not None


def run_manual(
    pdf_name: str,
    provider: LLMProvider,
    *,
    force: bool = False,
    steps: set[str] = frozenset({"af", "profiles", "require"}),
    out_dir: str | None = None,
    pages_override: tuple[int, int] | None = None,
    board_override: str | None = None,
    input_dir: str | None = None,
    log=print,
) -> dict:
    """
    處理一塊板:辨識板名 -> 手冊抽取(af/profiles/require) -> DTS 抽取(dts)。
    已存在的輸出檔一律跳過(除非 force),因此可重複執行、增量處理。
    input_dir:材料來源(預設全域 input/;web 上傳流程傳 per-job staging 目錄)。
    """
    pdf_path = os.path.join(input_dir or INPUT_DIR, pdf_name)
    report: dict = {"manual": pdf_name, "files": {}, "warnings": []}

    log(f"[{pdf_name}] 讀取 PDF 文字 ...")
    pages = pdf_text.extract_pages(pdf_path)

    if board_override:
        board, soc = board_override, board_override
        identify.save_board_entry(pdf_name, board)
    else:
        board, soc = identify.resolve_board(pdf_name, pages, provider)
    report["board"], report["soc"] = board, soc
    log(f"[{pdf_name}] 板名={board} soc={soc}")

    out_base = base_dir(board, out_dir)
    af_path = os.path.join(out_base, "af_table.json")
    allp_path = os.path.join(out_base, "all_peripheral.json")
    prof_path = os.path.join(out_base, "peripheral_profiles.json")
    req_path = os.path.join(out_base, "require.json")

    af_table: dict | None = None

    if "af" in steps:
        if os.path.exists(af_path) and not force:
            report["files"]["af_table.json"] = "skipped"
        else:
            runs = (
                [(pages_override[0] - 1, pages_override[1] - 1)]
                if pages_override
                else locate.pinmux_page_runs(pages)
            )
            if not runs:
                report["warnings"].append(
                    "找不到 pinmux 表候選頁;可用 --pages START-END 手動指定"
                )
            else:
                log(
                    f"[{pdf_name}] pinmux 候選頁: "
                    + ", ".join(f"{a + 1}-{b + 1}" for a, b in runs)
                )
                af_table, warns = extract_af.extract_af_table(
                    pages, runs, provider, log=log
                )
                report["warnings"] += warns
                if not af_table:
                    report["warnings"].append("AF 抽取結果為空,未寫檔")
                else:
                    report["files"]["af_table.json"] = _write_json(
                        af_path, af_table, force
                    )
                    report["af_pins"] = len(af_table)

    # 衍生檔:af_table 有重生就一併重生;否則僅在缺檔時從磁碟上的 af_table 補生
    if "af" in steps:
        if af_table is None and os.path.exists(af_path):
            with open(af_path, "r", encoding="utf-8") as f:
                af_table = json.load(f)
        if af_table:
            allp = derive.derive_all_peripheral(af_table, soc)
            report["files"]["all_peripheral.json"] = _write_json(
                allp_path, allp, force or report["files"].get("af_table.json") == "written"
            )

    if "profiles" in steps:
        if os.path.exists(prof_path) and not force:
            report["files"]["peripheral_profiles.json"] = "skipped"
        elif not os.path.exists(allp_path):
            report["warnings"].append("缺 all_peripheral.json,跳過 profiles 草擬")
        else:
            with open(allp_path, "r", encoding="utf-8") as f:
                allp = json.load(f)
            log(f"[{pdf_name}] 草擬 peripheral_profiles.json ...")
            excerpt = "\n\n".join(pages[i] for i in locate.boot_pages(pages, limit=4))
            draft = profiles.draft_peripheral_profiles(
                provider, soc=soc, board=board, all_peripheral=allp, excerpt=excerpt
            )
            report["files"]["peripheral_profiles.json"] = _write_json(
                prof_path, draft, force
            )

    if "require" in steps:
        if os.path.exists(req_path) and not force:
            report["files"]["require.json"] = "skipped"
        else:
            if af_table is None and os.path.exists(af_path):
                with open(af_path, "r", encoding="utf-8") as f:
                    af_table = json.load(f)
            if not af_table:
                report["warnings"].append("缺 af_table.json,跳過 require 草擬")
            else:
                log(f"[{pdf_name}] 草擬 require.json ...")
                boot_idx = locate.boot_pages(pages)
                boot_text = "\n\n--- PAGE BREAK ---\n\n".join(
                    pages[i] for i in boot_idx
                )
                draft = profiles.draft_require(
                    provider, soc=soc, af_table=af_table, boot_text=boot_text
                )
                report["files"]["require.json"] = _write_json(req_path, draft, force)

    # 增項 B：上傳時人工填寫的安全保留群組（PMIC 等）——require 草稿就緒、
    # af_table 已誕生後併入（驗證＋AF 回填見 reserved_groups.py）。require
    # 被 skipped（增量重跑）也要跑：CLI 使用者可事後補 reserved_groups.json。
    if "require" in steps and os.path.exists(req_path):
        from . import reserved_groups
        report["warnings"] += reserved_groups.merge(
            board, input_dir=(input_dir or INPUT_DIR),
            req_path=req_path, af_path=af_path, log=log)

    if "dts" in steps:
        if not has_dts_input():
            report["warnings"].append("input/ 下沒有 .dts 檔,跳過 DTS 抽取")
        else:
            try:
                dts_report = dts_extract.run_board(
                    board, out_dir=out_dir, force=force, log=log
                )
                report["files"].update(dts_report["files"])
                report["warnings"] += dts_report["warnings"]
                lint = dts_report.get("lint") or {}
                if lint.get("fails"):
                    report["lint_failed"] = True
                    report["warnings"] += [f"lint FAIL:{x}"
                                           for x in lint["fails"]]
            except RuntimeError as exc:
                report["warnings"].append(f"DTS 抽取失敗:{exc}")

    return report
