"""
peripherals.py — expand peripheral-level requests into signal-level requests.

A request like ["I2C2", "SPI3", "SDMMC2:8bit"] is turned into the concrete
signal list the CSP solver consumes.  Resolution order per token:

  1. official DTS  — if the peripheral is enabled in
     official_dts_peripheral.json, use exactly those signals (board-proven),
     unless an explicit ":mode" override is given.
  2. profile       — otherwise expand via peripheral_profiles.json:
     {FAMILY}{INSTANCE}_{suffix} for the family's default (or ":mode") signals.
  3. standalone    — unnumbered peripherals (PCIE, USBH, LCD, …) that fit no
     family template: expand via peripheral_profiles.json's per-instance
     "peripherals" section, using its default_required_signals. An EMPTY list
     is a valid expansion (the peripheral runs on dedicated pads and needs no
     pin assignment); optional_signals are never auto-enabled — the user asks
     for those explicitly as signal-level requests.

Every produced name is validated against Σ (the AF table); names absent from Σ
(e.g. ETH3 has no RGMII_CLK125) are reported as problems, never silently dropped.
"""
from __future__ import annotations

import json
from typing import Dict, List, Set, Tuple


def load_profiles(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_dts_index(dts_peripherals: dict) -> Dict[str, List[str]]:
    """Canonical peripheral token (I2C2, FDCAN1, ETH1) -> [signals], from DTS.

    The token before the first '_' in a signal name IS the canonical peripheral
    (e.g. "FDCAN1_TX" -> "FDCAN1", "ETH1_RGMII_TXD0" -> "ETH1").
    """
    idx: Dict[str, List[str]] = {}
    for node, v in dts_peripherals.items():
        sigs = v.get("signals") if isinstance(v, dict) else v
        for s in (sigs or []):
            idx.setdefault(s.split("_", 1)[0], []).append(s)
    return idx


def _apply_alias(name: str, aliases: Dict[str, str]) -> str:
    for a, b in aliases.items():
        if name.startswith(a) and not name.startswith(b):
            return b + name[len(a):]
    return name


def _split_family(name: str, families) -> Tuple[str, str, str] | None:
    """'SPI3' -> ('SPI','3','spi'); 'I2C2' -> ('I2C','2','i2c').  Longest match."""
    best = None
    for key in families:
        F = key.upper()
        if name.startswith(F) and name[len(F):].isdigit():
            if best is None or len(F) > len(best[0]):
                best = (F, name[len(F):], key)
    return best


def expand_one(token: str, dts_index, profiles: dict, sigma: Set[str]):
    """Return (signals, provenance, problems) for one peripheral token."""
    aliases = profiles.get("aliases", {})
    families = profiles["families"]

    raw, _, mode = token.upper().partition(":")
    name = _apply_alias(raw, aliases)

    # Tier 1 — official DTS (only when no explicit mode override).
    if not mode and name in dts_index:
        sigs = dts_index[name]
        return sigs, "official-dts", _missing(token, sigs, sigma)

    # Tier 2 — family profile.
    fam = _split_family(name, families)
    if not fam:
        # Tier 3 — standalone per-instance profile (no {FAMILY}{N} split).
        prof = (profiles.get("peripherals") or {}).get(name)
        if isinstance(prof, dict):
            sigs = [str(s).upper() for s in prof.get("default_required_signals") or []]
            problems = _missing(token, sigs, sigma)
            if mode and mode.lower() != str(prof.get("mode") or "").lower():
                problems.append(
                    f"{token}: 「{name}」不支援 mode 覆寫"
                    f"（profile 固定為 '{prof.get('mode')}'），已忽略 ':{mode}'")
            return sigs, "instance-profile", problems
        return [], "unknown-family", [f"{token}: unknown peripheral family"]
    prefix, inst, key = fam
    prof = families[key]
    m = mode.lower() or prof["default"]
    if m not in prof["modes"]:
        return [], "unknown-mode", [
            f"{token}: mode '{m}' not in {sorted(prof['modes'])}"]
    # Most families follow {FAMILY}{INSTANCE}_{suffix}; a family may override
    # with its own "template" (e.g. OCTOSPI -> OCTOSPIM_P{inst}_{suf}).
    template = prof.get("template", "{prefix}{inst}_{suf}")
    sigs = [template.format(prefix=prefix, inst=inst, suf=suf)
            for suf in prof["modes"][m]]
    return sigs, f"profile:{key}/{m}", _missing(token, sigs, sigma)


def _missing(token, sigs, sigma):
    return [f"{token}: '{s}' not in Σ (not available on this SoC)"
            for s in sigs if s not in sigma]


def expand(tokens: List[str], dts_index, profiles: dict, sigma: Set[str]):
    """Expand peripheral tokens -> (required, provenance, problems).

    required   : ordered, de-duplicated signal list for the solver
    provenance : signal -> (origin token, resolution source)
    problems   : human-readable strings for unknown families/modes/signals
    """
    required: List[str] = []
    provenance: Dict[str, Tuple[str, str]] = {}
    problems: List[str] = []
    seen: Set[str] = set()
    for tok in tokens:
        sigs, src, probs = expand_one(tok, dts_index, profiles, sigma)
        problems.extend(probs)
        for s in sigs:
            if s not in seen:
                seen.add(s)
                required.append(s)
                provenance[s] = (tok, src)
    return required, provenance, problems
