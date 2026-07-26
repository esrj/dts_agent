"""pinmux_style.py — pinmux 渲染/解析的 per-vendor style 抽象（PINMUX_STYLE_PLAN W1）。

第二段對 kernel DTS pinmux 行的四種操作——m1 解析 baseline、m4/m6 渲染、
m6 guard 的 mode 詞彙對賬、m7 結構驗證——全部經本模組分派。

style 選擇：data/<board>/dts_generation/pinmux_style.json 的 "style" 欄。
**缺檔預設 stm32**——stm32mp257f-ev1 不放此檔，行為與抽象前 bit-for-bit
相同（相容紅線，同多板化階段 A 慣例）。

k3-iopad / nuvoton-mfp 由 W3/W4 實作——需 Knowledge Extractor 供料
pad_params.json（規格見 prompts/5_pinmux_data_supply.md）後同步交付；
在那之前選用會得到明確的 NotImplementedError（kb_lint 會在進場先攔）。

介面（duck-type，不強制 ABC——與 validator engines 同慣例）：
  iter_pinmux(text, domain=None)  pinmux 內容 -> [(pin, af|None, mode_token)]
                                  （K3 需 domain 提示做 offset->pad 反查）
  pin_sources(nd)          群組節點 -> [(子節點, pinmux 內容)]（stm32 是
                           subnode 的 pinmux 屬性；K3 是節點自身的
                           pinctrl-single,pins 陣列）
  render_pinmux(pin, mode) (pin, mode token) -> DTS 巨集字串
  render_group(label, name, entries) -> (容器 ref, 群組 DTS 文字)
                           entries = [(subname, pin, mode, electrical, signal)]
  containers()             pinctrl 容器 top-level ref 集（不含 &）
  mode_token(af)           plan 的 af 號 -> mode 詞彙。**全 style 統一 AF<n>**
                           （內部語彙，非輸出文字）——m6 schema/guard 與 m7
                           對賬因此零改動；輸出格式由 render/parse 邊界轉換
  lowpower_token()         低功耗態詞彙（stm32=ANALOG；無此概念回 None）
  af_of_mode(mode)         mode 詞彙 -> af 號（無 af 的 mode 回 None）
  GROUP_PROPS              群組承載 pinmux 的屬性名（electrical 過濾用）
  RAW_NUMBER_RE/macro_label  m7 結構檢查（pinmux 不准寫裸數字）用
"""
import json
import re

from . import config


def assemble_group_sections(tagged_groups):
    """[(容器 ref, 群組文字)] -> managed region 的 pinctrl 區段行列表。
    依容器分組、保序輸出（m4/m6 assembly 共用）。stm32 單容器時輸出與
    改造前逐字相同：["&pinctrl {", *groups, "};", ""]。"""
    by, order = {}, []
    for cont, txt in tagged_groups:
        if cont not in by:
            order.append(cont)
        by.setdefault(cont, []).append(txt)
    out = []
    for cont in order:
        out.append(f"{cont} {{")
        out += by[cont]
        out.append("};")
        out.append("")
    return out


class Stm32Style:
    """ST 的 STM32_PINMUX('B', 5, AF9) 巨集。

    原 m1_dts_parser.index（_PINMUX_RE/_AF_MODE）、m4_patch_generation.generate
    （_pinmux）、m7_validator.validate（_PINMUX_RE/MACRO_EXPANDED 規則）的
    硬編碼**平移**至此——邏輯逐字保留，行為不變。
    """

    name = "stm32"
    PINMUX_RE = re.compile(r"STM32_PINMUX\('([A-Z])',\s*(\d+),\s*(\w+)\)")
    # m7 MACRO_EXPANDED：pinmux 寫成裸數字（巨集被 cpp 展開）是結構錯誤
    RAW_NUMBER_RE = re.compile(r"pinmux\s*=\s*<\s*(0x[0-9a-fA-F]+|\d+\s+\d+)")
    macro_label = "STM32_PINMUX(...)"
    GROUP_PROPS = ("pinmux",)

    # AF 巨集 token -> af 號；GPIO/ANALOG 無 af（原 m1 _AF_MODE）
    _AF_MODE = {"GPIO": None, "ANALOG": None,
                **{f"AF{i}": i for i in range(16)}}

    def iter_pinmux(self, text, domain=None):
        """pinmux 屬性原文 -> [(pin, af|None, mode_str)]（stm32 不需 domain）。"""
        out = []
        for m in self.PINMUX_RE.finditer(text or ""):
            mode = m.group(3)
            out.append((f"P{m.group(1)}{m.group(2)}", self.af_of_mode(mode), mode))
        return out

    def pin_sources(self, nd):
        """群組節點 -> [(承載 pinmux 的節點, pinmux 內容)]（原 m1 pin_children）。"""
        subs = [(c, c.props["pinmux"]) for c in nd.children if "pinmux" in c.props]
        if "pinmux" in nd.props:            # rare: pinmux directly on the group
            subs.append((nd, nd.props["pinmux"]))
        return subs

    def containers(self):
        return {"pinctrl"}

    def af_of_mode(self, mode):
        return self._AF_MODE.get(mode)

    def render_pinmux(self, pin, mode):
        return f"STM32_PINMUX('{pin[1]}', {int(pin[2:])}, {mode})"

    def render_group(self, label, name, entries):
        """(原 m4._render_subnode/_render_group 與 m6.render_group 的共同形狀，
        逐字平移。) 回 ("&pinctrl", 群組文字)。"""
        out = [f"\t{label}: {name} {{"]
        for sub, pin, mode, electrical, signal in entries:
            out.append(f"\t\t{sub} {{")
            cm = f"\t/* {signal} */" if signal else ""
            out.append(f"\t\t\tpinmux = <{self.render_pinmux(pin, mode)}>;{cm}")
            for k, v in (electrical or {}).items():
                out.append(f"\t\t\t{k};" if v is None else f"\t\t\t{k} = {v};")
            out.append("\t\t};")
        out.append("\t};")
        return "&pinctrl", "\n".join(out)

    def mode_token(self, af):
        return f"AF{af}"

    def lowpower_token(self):
        return "ANALOG"


class K3IopadStyle(Stm32Style):
    """TI K3（AM65x）pinctrl-single IOPAD 巨集（PINMUX_STYLE_PLAN W3）。

    與 stm32 的三個結構差異：
      1. pinmux 內容在群組節點自身的 `pinctrl-single,pins = < ... >` 陣列，
         不是 subnode 的 pinmux 屬性；
      2. 巨集是 `<MACRO>(offset, FLAGS, mux)`——offset 由 pad_params.json
         供料（Extractor，prompts/5），且 **offset 以 domain 為鍵空間**
         （main_pmx0/main_pmx1/wkup_pmx0… 各自起算）；
      3. 群組要放進所屬 domain 的容器（&main_pmx0…），一群組限單一 domain。
    內部 mode 詞彙沿用 AF<n>（繼承），只在 render/parse 邊界轉 mux 整數。
    K3 無 ANALOG 低功耗腳態 -> lowpower_token=None。
    """

    name = "k3-iopad"
    GROUP_PROPS = ("pinctrl-single,pins",)
    RAW_NUMBER_RE = re.compile(
        r"pinctrl-single,pins\s*=\s*<\s*(0x[0-9a-fA-F]+|\d+)[\s>]")
    PINMUX_RE = re.compile(
        r"([A-Z0-9_]*IOPAD)\(\s*(0x[0-9a-fA-F]+)\s*,\s*([A-Za-z0-9_|()\s]+?)\s*,\s*(\d+)\s*\)")

    def __init__(self, style_cfg, pad_params):
        self.cfg = style_cfg
        self.macros = style_cfg.get("macros") or {}          # domain -> macro
        self.node_refs = (style_cfg.get("nodes")
                          or {d: f"&{d}" for d in self.macros})
        self.role_flags = style_cfg.get("role_flags") or {}
        self.macro_label = " / ".join(
            f"{m}(...)" for m in sorted(set(self.macros.values()))) or "IOPAD(...)"
        self.pad_of = {}      # (domain, offset_int) -> pad
        self.param_of = {}    # PAD -> (domain, offset_int)
        for pad, pr in ((pad_params or {}).get("pads") or {}).items():
            dom, off = pr.get("domain"), int(str(pr.get("offset")), 16)
            self.pad_of[(dom, off)] = pad
            self.param_of[pad.upper()] = (dom, off)
        self._doms_of_macro = {}
        for dom, mac in self.macros.items():
            self._doms_of_macro.setdefault(mac, []).append(dom)

    def iter_pinmux(self, text, domain=None):
        """IOPAD 巨集 -> [(pad, mux, "AF<mux>")]。offset->pad 反查優先用
        domain 提示（群組所在的 &pmx 節點）；無提示時取該巨集各 domain 的
        唯一候選；反查不到時回可識別的保底名（不誤配到別的 pad）。"""
        out = []
        for m in self.PINMUX_RE.finditer(text or ""):
            mac, off = m.group(1), int(m.group(2), 16)
            mux = int(m.group(4))
            pad = None
            if domain and (domain, off) in self.pad_of:
                pad = self.pad_of[(domain, off)]
            else:
                cands = [self.pad_of[(d, off)]
                         for d in self._doms_of_macro.get(mac, [])
                         if (d, off) in self.pad_of]
                if len(cands) == 1:
                    pad = cands[0]
            if pad is None:
                pad = f"{domain or mac}@0x{off:04x}"
            out.append((pad, mux, f"AF{mux}"))
        return out

    def pin_sources(self, nd):
        text = nd.props.get("pinctrl-single,pins")
        return [(nd, text)] if text else []

    def containers(self):
        return set(self.node_refs)

    def _params(self, pin):
        pr = self.param_of.get((pin or "").upper())
        if pr is None:
            raise ValueError(
                f"pad_params.json 缺 pad {pin!r} 的 domain/offset——"
                f"kb_lint 的覆蓋率檢查應已在進場攔下（prompts/5 供料規格）")
        return pr

    def _flag(self, signal):
        """signal 角色 -> 電氣 flag（role_flags 詞彙由 Extractor 從官方 DTS 供料）。"""
        tail = (signal or "").upper().rsplit("_", 1)[-1]
        if "RX" in tail:
            key = "rx"
        elif "TX" in tail:
            key = "tx"
        elif tail in ("SCL", "CK", "CLK", "SCLK"):
            key = "clk"
        elif tail in ("SDA",):
            key = "bidir"
        else:
            key = "default"
        return (self.role_flags.get(key) or self.role_flags.get("default")
                or "PIN_INPUT")

    def render_pinmux(self, pin, mode, signal=None):
        dom, off = self._params(pin)
        mux = self.af_of_mode(mode)
        if mux is None:
            raise ValueError(f"k3-iopad 無法渲染 mode {mode!r}（pad {pin}）"
                             "——K3 無 GPIO/ANALOG 腳態詞彙")
        return f"{self.macros[dom]}(0x{off:04x}, {self._flag(signal)}, {mux})"

    def render_group(self, label, name, entries):
        doms = {self._params(pin)[0] for _s, pin, _m, _e, _sig in entries}
        if len(doms) != 1:
            raise ValueError(
                f"群組 {label} 的 pads 橫跨多個 pinmux domain {sorted(doms)}"
                "——K3 一個群組限單一 domain（請檢查 plan 的腳位組合）")
        dom = doms.pop()
        out = [f"\t{label}: {name} {{", "\t\tpinctrl-single,pins = <"]
        for _sub, pin, mode, _elec, signal in entries:
            cm = f"\t/* {signal} */" if signal else ""
            out.append(f"\t\t\t{self.render_pinmux(pin, mode, signal)}{cm}")
        out += ["\t\t>;", "\t};"]
        return self.node_refs.get(dom, f"&{dom}"), "\n".join(out)

    def lowpower_token(self):
        return None


class NuvotonMfpStyle(Stm32Style):
    """Nuvoton MA35D1 的 nuvoton,pins 巨集（PINMUX_STYLE_PLAN W4/M5）。

    官方格式（ma35d1-evb.dts 實抓）：
        &pinctrl {
            gmac0 {                             ← 週邊分組中介層（可有可無）
                pinctrl_gmac0: gmac0grp {
                    nuvoton,pins =
                        <SYS_GPE_MFPL_PE0MFP_RGMII0_MDC  &pcfg_emac_1_8V>, …;
                };
            };
        };
    - 巨集 SYS_GP{bank}_MFP{L|H}_P{bank}{num}MFP_{SIGNAL} 展開為
      (reg, shift, value) 三 cell——**mux value 內建於巨集**，渲染不需要
      pad offset 供料；num 0–7 → MFPL、8–15 → MFPH。
    - 每條帶 &pcfg_* 組態 ref（電氣組態由 pcfg 承擔，不用 per-pin 屬性）。
    - signal token 與 af_table 逐字一致（nuvoton 的 af_table 由同一份
      pinfunc.h 權威重建）——巨集名可由 (pin, signal) 機械組出；
      解析側的 mux 值以 af_table 反查 (pin, signal)。
    內部 mode 詞彙沿用 AF<n>（繼承）；無 ANALOG 低功耗腳態。
    """

    name = "nuvoton-mfp"
    GROUP_PROPS = ("nuvoton,pins",)
    RAW_NUMBER_RE = re.compile(r"nuvoton,pins\s*=\s*<\s*(0x[0-9a-fA-F]+|\d+)\s")
    PINMUX_RE = re.compile(r"SYS_GP\w+_MFP[LH]_P([A-Z])(\d+)MFP_(\w+)")
    macro_label = "SYS_GPx_MFPy_PxnMFP_…（ma35d1-pinfunc.h 巨集）"

    def __init__(self, style_cfg, af_table):
        self.cfg = style_cfg or {}
        self.role_flags = self.cfg.get("role_flags") or {}
        # (PIN, SIGNAL 大寫) -> mux（af_table 為命名權威）
        self._mux: dict = {}
        for pin, muxes in (af_table or {}).items():
            if not isinstance(muxes, dict):
                continue
            for mux, cell in muxes.items():
                for s in str(cell).split("/"):
                    s = s.strip()
                    if s and s != "-":
                        self._mux.setdefault((pin.upper(), s.upper()), int(mux))

    def iter_pinmux(self, text, domain=None):
        out = []
        for m in self.PINMUX_RE.finditer(text or ""):
            pin = f"P{m.group(1)}{int(m.group(2))}"
            sig = m.group(3)
            if sig.upper() == "GPIO":
                out.append((pin, None, "GPIO"))
                continue
            af = self._mux.get((pin, sig.upper()))
            out.append((pin, af, f"AF{af}" if af is not None else "GPIO"))
        return out

    def pin_sources(self, nd):
        subs = [(c, c.props["nuvoton,pins"]) for c in nd.children
                if "nuvoton,pins" in c.props]
        if "nuvoton,pins" in nd.props:
            subs.append((nd, nd.props["nuvoton,pins"]))
        return subs

    # containers() 繼承 Stm32Style 的 {"pinctrl"}——MA35 同名

    def _macro(self, pin, signal):
        bank, num = pin[1], int(pin[2:])
        half = "L" if num <= 7 else "H"
        return f"SYS_GP{bank}_MFP{half}_{pin}MFP_{signal}"

    def render_pinmux(self, pin, mode, signal=None):
        if not signal:
            raise ValueError(f"nuvoton-mfp 渲染需要 signal 名（pad {pin}）")
        return self._macro(pin.upper(), signal)

    def render_group(self, label, name, entries):
        pcfg = self.role_flags.get("default") or "&pcfg_default"
        rows = []
        for _sub, pin, mode, _elec, signal in entries:
            # 一致性防呆：plan 的 (pin, signal) 必須能在 af_table 反查到 mux
            if signal and self._mux.get((pin.upper(), signal.upper())) is None:
                raise ValueError(
                    f"nuvoton-mfp：af_table 查不到 {signal}@{pin} 的 mux——"
                    "pinfunc 巨集無法成立（知識庫不一致？）")
            rows.append(
                f"\t\t\t<{self.render_pinmux(pin, mode, signal)}\t{pcfg}>")
        out = [f"\t{label}: {name} {{", "\t\tnuvoton,pins ="]
        out.append(",\n".join(rows) + ";")
        out.append("\t};")
        return "&pinctrl", "\n".join(out)

    def lowpower_token(self):
        return None


_KNOWN_PENDING: set = set()   # 全部 style 已實作
_cache: dict = {}          # board id -> style 實例（init_board 換板即換 key）


def get_style():
    """目前板（config.BOARD）的 pinmux style。缺檔／style=stm32 -> Stm32Style。"""
    board = config.BOARD
    if board in _cache:
        return _cache[board]
    cfg = {}
    try:
        with open(config.PINMUX_STYLE_JSON, encoding="utf-8") as fh:
            cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("not a mapping")
    except FileNotFoundError:
        pass                                    # 缺檔＝stm32 預設（行為不變）
    except ValueError as exc:
        raise ValueError(
            f"pinmux_style.json 損壞：{config.PINMUX_STYLE_JSON}：{exc}") from None
    name = (cfg.get("style") or "stm32").lower()
    if name == "stm32":
        style = Stm32Style()
    elif name == "k3-iopad":
        try:
            with open(config.PAD_PARAMS, encoding="utf-8") as fh:
                pads = json.load(fh)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"style=k3-iopad 需要 pad_params.json（pad→domain/offset 表）："
                f"{config.PAD_PARAMS}——由 Knowledge Extractor 供料"
                f"（prompts/5）") from None
        style = K3IopadStyle(cfg, pads)
    elif name == "nuvoton-mfp":
        try:
            with open(config.AF_TABLE, encoding="utf-8") as fh:
                af_table = json.load(fh)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"style=nuvoton-mfp 需要 af_table.json（(pin, signal)→mux "
                f"反查）：{config.AF_TABLE}") from None
        style = NuvotonMfpStyle(cfg, af_table)
    else:
        raise ValueError(f"未知的 pinmux style {name!r}（板 {board}）；"
                         f"合法值：stm32 / k3-iopad / nuvoton-mfp")
    _cache[board] = style
    return style
