"""Structured Baseline DTS index built on top of the core parser.

`build_index()` parses the whole baseline DTS source set and exposes the views
Stages 2/3/4 need:
  - pinctrl groups : label -> PinctrlGroup (entries with pin, af, signal, electrical)
  - node overrides : &label -> NodeOverride (status, pinctrl-0/1/2, props, deletions, children)
  - node defs      : label -> NodeDef (compatible, reg, #cells) from the SoC dtsi
  - all_labels     : every label defined anywhere (for alias validation)
  - family_of()    : peripheral family of a node label
  - signal_for()   : (pin, af) -> signal via af_table

Signal names come from af_table (authoritative), NOT from DTS comments.
"""
import os
import re
import json
from dataclasses import dataclass, field

from .parser import parse_dts, walk, iter_all, compat_primary
from ..pinmux_style import get_style

# pinmux 巨集的解析（原 _PINMUX_RE / _AF_MODE 硬編碼）已平移至
# pinmux_style.Stm32Style——多廠商化後由該板的 style 分派（缺檔預設 stm32）。

_FAMILY_RULES = [
    (r'^usart\d+$', 'usart'), (r'^uart\d+$', 'uart'), (r'^lpuart\d+$', 'lpuart'),
    (r'^i2c\d+$', 'i2c'), (r'^i3c\d+$', 'i3c'),
    (r'^spi\d+$', 'spi'), (r'^i2s\d+$', 'i2s'),
    (r'^sdmmc\d+$', 'sdmmc'),
    (r'^m_can\d+$', 'fdcan'), (r'^fdcan\d+$', 'fdcan'),
    (r'^eth\d+$', 'eth'),
    (r'^ommanager$', 'octospi'), (r'^ospi\d+$', 'octospi'), (r'^omi\d+$', 'octospi'),
]


def family_of(label):
    if not label:
        return None
    for pat, fam in _FAMILY_RULES:
        if re.match(pat, label.lower()):
            return fam
    return None


@dataclass
class PinEntry:
    pin: str                 # "PA4"
    af: int                  # 6  (or None for GPIO/ANALOG mode)
    mode: str                # "AF6" / "GPIO" / "ANALOG"
    signal: str              # from af_table, may be None if not found
    subnode: str             # "pins1"
    electrical: dict         # {"bias-disable": None, "slew-rate": "<0>", ...}


@dataclass
class PinctrlGroup:
    label: str
    name: str
    file: str
    line: int
    entries: list = field(default_factory=list)

    def pin_af_set(self):
        return {(e.pin, e.af) for e in self.entries}

    def pins(self):
        return {e.pin for e in self.entries}


@dataclass
class NodeOverride:
    label: str               # the &ref label
    file: str
    line: int
    status: str = None
    enabled: bool = True
    props: dict = field(default_factory=dict)
    deletions: list = field(default_factory=list)
    pinctrl_names: list = field(default_factory=list)
    pinctrl: dict = field(default_factory=dict)   # "pinctrl-0" -> ["usart2_pins_a"]
    children: list = field(default_factory=list)   # raw child Nodes

    @property
    def pinctrl_0(self):
        return self.pinctrl.get('pinctrl-0', [])


@dataclass
class NodeDef:
    label: str
    name: str
    file: str
    line: int
    unit_address: str = None
    compatible: str = None
    reg: str = None
    address_cells: str = None
    size_cells: str = None


class DtsIndex:
    def __init__(self, af_table):
        self.af_table = af_table or {}
        self.pinctrl_groups = {}     # label -> PinctrlGroup
        self.overrides = {}          # ref label -> NodeOverride (board file)
        self.defs = {}               # label -> NodeDef
        self.all_labels = set()
        self.files = {}              # filename -> [top-level Node]

    # ---- lookups ----
    def group(self, label):
        return self.pinctrl_groups.get(label)

    def override(self, label):
        return self.overrides.get(label)

    def node_def(self, label):
        return self.defs.get(label)

    def family_of(self, label):
        return family_of(label)

    def signal_for(self, pin, af):
        return self.af_table.get(pin, {}).get(str(af))

    def enabled_overrides(self):
        return {k: v for k, v in self.overrides.items() if v.enabled}


def _pinctrl_groups_from(nodes, fname, af_table):
    """A pinctrl group = a labeled node that carries pinmux content per the
    board's style（stm32：子節點的 pinmux 屬性；K3：節點自身的
    pinctrl-single,pins 陣列）。逐 top-level 節點下行，帶 top.ref 當
    domain 提示（K3 的 offset->pad 反查需要；stm32 忽略）。"""
    out = {}
    style = get_style()
    for top in nodes:
        hint = top.ref
        for nd in iter_all([top]):
            if not nd.label:
                continue
            sources = style.pin_sources(nd)
            if not sources:
                continue
            grp = PinctrlGroup(label=nd.label, name=nd.name, file=fname, line=nd.line)
            for sub, text in sources:
                electrical = {k: v for k, v in sub.props.items()
                              if k not in style.GROUP_PROPS}
                for pin, af, mode in style.iter_pinmux(text, domain=hint):
                    grp.entries.append(PinEntry(
                        pin=pin, af=af, mode=mode,
                        signal=(af_table.get(pin, {}).get(str(af)) if af is not None else None),
                        subnode=sub.name, electrical=dict(electrical),
                    ))
            if grp.entries:
                out[nd.label] = grp
    return out


def _override_from(nd, fname):
    ov = NodeOverride(label=nd.ref, file=fname, line=nd.line,
                      status=nd.status, enabled=(nd.status != 'disabled'),
                      props=dict(nd.props), deletions=list(nd.deletions),
                      children=list(nd.children))
    names = nd.props.get('pinctrl-names')
    if names:
        ov.pinctrl_names = re.findall(r'"([^"]+)"', names)
    for key in ('pinctrl-0', 'pinctrl-1', 'pinctrl-2', 'pinctrl-3'):
        if key in nd.props:
            ov.pinctrl[key] = re.findall(r'&(\w+)', nd.props[key])
    return ov


def _node_defs_from(nodes, fname):
    out = {}
    for nd in iter_all(nodes):
        if nd.label and not nd.ref and nd.label not in out:
            out[nd.label] = NodeDef(
                label=nd.label, name=nd.name, file=fname, line=nd.line,
                unit_address=nd.unit_address,
                compatible=compat_primary(nd.props.get('compatible')),
                reg=nd.props.get('reg'),
                address_cells=nd.props.get('#address-cells'),
                size_cells=nd.props.get('#size-cells'),
            )
    return out


def build_index(dts_dir=None, af_table=None, board_file=None):
    """Parse the baseline DTS source set into a DtsIndex.

    dts_dir    : directory with the .dts/.dtsi set (default: config.DTS_DIR)
    af_table   : af_table dict (default: load config.AF_TABLE)
    board_file : the board .dts whose &overrides are the board config
                 (default: config.BOARD_DTS name)
    """
    from .. import config
    dts_dir = str(dts_dir or config.DTS_DIR)
    board_file = board_file or config.BOARD_DTS.name
    if af_table is None:
        af_table = json.load(open(config.AF_TABLE))

    idx = DtsIndex(af_table)
    for fn in sorted(f for f in os.listdir(dts_dir) if f.endswith(('.dts', '.dtsi'))):
        nodes = parse_dts(open(os.path.join(dts_dir, fn)).read())
        idx.files[fn] = nodes
        idx.pinctrl_groups.update(_pinctrl_groups_from(nodes, fn, af_table))
        for label, nd in _node_defs_from(nodes, fn).items():
            idx.defs.setdefault(label, nd)
        idx.all_labels.update(nd.label for nd in iter_all(nodes) if nd.label)
        if fn == board_file:
            for nd in nodes:
                if nd.ref:
                    idx.overrides[nd.ref] = _override_from(nd, fn)
    return idx
