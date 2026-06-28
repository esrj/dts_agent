"""M1 — Shared Baseline DTS Parser (ROADMAP M1 / plan.md §6.2).

Parses the baseline DTS source set into a structured index reused by M3/M4/M5.

Public API:
    parse_dts(text) -> list[Node]        # low-level recursive parser
    walk / iter_all                       # tree traversal
    build_index(...) -> DtsIndex          # high-level structured index
        .pinctrl_groups[label] -> PinctrlGroup (entries: pin, af, signal, electrical)
        .overrides[label]      -> NodeOverride (status, pinctrl-0/1/2, props, ...)
        .defs[label]           -> NodeDef (compatible, reg, #cells)
        .all_labels, .family_of(), .signal_for()

Status: DONE (M1). Unit tests in tests/test_m1_dts_parser.py.
"""
from .parser import (
    Node, parse_dts, walk, iter_all,
    strip_comments_preserve, gpio_pin, compat_primary, compat_strings,
)
from .index import (
    build_index, DtsIndex, PinctrlGroup, PinEntry, NodeOverride, NodeDef,
    family_of,
)

__all__ = [
    "Node", "parse_dts", "walk", "iter_all",
    "strip_comments_preserve", "gpio_pin", "compat_primary", "compat_strings",
    "build_index", "DtsIndex", "PinctrlGroup", "PinEntry", "NodeOverride",
    "NodeDef", "family_of",
]
