"""
resolver.py — Requirement Resolver: Intent IR (from the LLM parser) -> SolveSpec.

The deterministic anti-corruption layer between the LLM and the CSP solver.
The LLM's JSON never reaches the solver directly; only a validated SolveSpec
(required_signals, must_gpio, must_bind) does.

Handled request levels (see src/llm_provider/system_prompt.md for the IR):
  - "signal"     : one item = one signal (+ optional pin).
                   -> add to required; if a pin is given, add a must_bind.
  - "peripheral" : one item = one peripheral.
                   -> expand to required signals (official DTS / family profile,
                      with :mode); each pin_assignment is resolved to a must_bind
                      by INTERSECTING the pin's AF-table signals with this
                      peripheral's expanded signals — so the AF number is NOT
                      needed for the common case.

  - "count"      : NOT resolved here.  A candidate-generation layer (instance
                   selection / preference search) must first lower a count item
                   into concrete "peripheral" items, which this resolver handles.

Before returning, the three solver hard-rules are enforced (Instance.validate
remains the backstop):
  1. every must_bind target signal is also in required;
  2. must_bind is injective on signals (distinct pins -> distinct signals);
  3. a must_bind pin is never also in must_gpio.
"""
from __future__ import annotations

import os
import sys
import json
from dataclasses import dataclass, field

# Allow running directly: `python src/solver/resolver.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from util.dataio import AF_TABLE, PROFILES, DTS, load_af, load_af_map, sigma_of
from solver.peripherals import build_dts_index, expand_one, load_profiles, _split_family
from solver.solve import Instance, PinAssign


class ResolveError(Exception):
    """Raised when an Intent IR cannot be turned into a valid SolveSpec."""


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
@dataclass
class SolveSpec:
    """The solver-facing contract. Only these three feed Instance."""
    required: list[str] = field(default_factory=list)         # required_signals
    must_gpio: set[str] = field(default_factory=set)          # gpio_must_pins
    must_bind: dict[str, str] = field(default_factory=dict)   # pin -> signal
    notes: list[str] = field(default_factory=list)            # informational only


@dataclass
class Knowledge:
    """Loaded knowledge bases the resolver queries."""
    af: dict          # pin -> set(signals)
    af_map: dict      # pin -> {af_int: [signals]}   (for AF-number resolution)
    sigma: set        # Σ — every distinct signal
    profiles: dict    # peripheral_profiles.json
    dts_index: dict   # peripheral token -> [signals]  (official DTS)


def load_knowledge(af_path=AF_TABLE, profiles_path=PROFILES, dts_path=DTS) -> Knowledge:
    """Build the resolver's knowledge base. Defaults to the legacy single-board
    paths; pass a board's files (see util.dataio.board_paths) to load per-board
    af_table / profiles / official-DTS together."""
    af = load_af(af_path)
    with open(dts_path, encoding="utf-8") as fh:
        dts_index = build_dts_index(json.load(fh)["peripherals"])
    return Knowledge(af=af, af_map=load_af_map(af_path), sigma=sigma_of(af),
                     profiles=load_profiles(profiles_path), dts_index=dts_index)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _instance_of(signal: str) -> str:
    """'SPI8_SCK' -> 'SPI8'  (token before the first underscore)."""
    return signal.split("_", 1)[0]


def reserved_owner(name: str, reserved) -> str | None:
    """Which reserve_only group (require.json) owns `name`, or None.

    `name` may be a signal or an instance token; it belongs to a reserved group
    iff it equals the group name or starts with "<group>_" — so I2C7_SCL and
    I2C7 both match group I2C7, OCTOSPIM_P1_CLK matches OCTOSPIM_P1, while
    OCTOSPIM_P2_CLK does not.
    """
    n = (name or "").upper()
    for r in reserved or ():
        if n == r or n.startswith(r + "_"):
            return r
    return None


def _conflict_hint(pin: str, signal: str, kb: "Knowledge") -> str:
    """KB-grounded explanation for an infeasible pin->signal binding: which
    same-peripheral signals the pad CAN carry, and where the signal CAN go.
    Used to make a hard conflict actionable (which pad clashes + candidates)."""
    inst = _instance_of(signal)
    pin_can = sorted(s for s in kb.af.get(pin, set()) if _instance_of(s) == inst)
    sig_pins = sorted(p for p, sigs in kb.af.items() if signal in sigs)
    return (f"（{pin} 能接的 {inst} 訊號：{pin_can or '—'}；"
            f"'{signal}' 只能放：{sig_pins or '—'}）")


def _add_required(spec: SolveSpec, sigs) -> None:
    for s in sigs:
        if s not in spec.required:
            spec.required.append(s)


def _bind(spec: SolveSpec, pin: str, signal: str) -> None:
    if pin in spec.must_bind and spec.must_bind[pin] != signal:
        raise ResolveError(
            f"{pin} bound to two signals: {spec.must_bind[pin]} and {signal}")
    spec.must_bind[pin] = signal


# --------------------------------------------------------------------------- #
# Per-level resolution
# --------------------------------------------------------------------------- #
def _resolve_signal(item: dict, kb: Knowledge, spec: SolveSpec,
                    reserved=frozenset()) -> None:
    signal = (item.get("signal") or "").upper()
    if not signal:
        raise ResolveError("signal item missing 'signal'")
    if signal not in kb.sigma:
        raise ResolveError(f"unknown signal '{signal}' (not in Σ / wrong name)")
    owner = reserved_owner(signal, reserved)
    if owner:
        raise ResolveError(
            f"訊號 {signal} 屬於保留週邊「{owner}」（require.json reserve_only："
            f"由 secure/bootloader 持有），kernel 不可使用。")
    _add_required(spec, [signal])

    pin = item.get("pin")
    if not pin:
        return
    pin = pin.upper()
    on_pin = kb.af.get(pin)
    if on_pin is None:
        raise ResolveError(f"{pin}: not a GPIO pin in the AF table")
    if signal not in on_pin:
        raise ResolveError(
            f"腳位衝突：{pin} 接不了 '{signal}' {_conflict_hint(pin, signal, kb)}")
    _bind(spec, pin, signal)


def _resolve_peripheral(item: dict, kb: Knowledge, spec: SolveSpec,
                        reserved=frozenset()) -> None:
    instance = (item.get("instance") or "").upper()
    if not instance:
        raise ResolveError("peripheral item missing 'instance'")
    mode = item.get("mode")
    token = f"{instance}:{mode}" if mode else instance

    sigs, src, problems = expand_one(token, kb.dts_index, kb.profiles, kb.sigma)
    spec.notes.extend(problems)
    valid = [s for s in sigs if s in kb.sigma]
    # A reserve_only group (require.json) can be hit by the instance name itself
    # (I2C7) or via expansion (OCTOSPIM -> OCTOSPIM_P1_*): both are refused —
    # the kernel neither owns these peripherals nor may re-home their signals.
    owners = {o for o in (reserved_owner(s, reserved) for s in valid) if o}
    owners |= {o for o in [reserved_owner(instance, reserved)] if o}
    if owners:
        raise ResolveError(
            f"「{instance}」屬於保留週邊（{', '.join(sorted(owners))}；require.json "
            f"reserve_only，由 secure/bootloader 持有），kernel 不可請求；"
            f"一般需求請改用其他 instance。")
    if not valid:
        if src == "instance-profile" and not sigs:
            # The profile DECLARES zero default signals (dedicated pads, e.g.
            # USBH): enabling it costs no GPIO AF pin — legal, nothing to place.
            spec.notes.append(
                f"{instance}: 使用專用腳位，無需 pin assignment（不佔用 GPIO AF 腳）")
            for pa in item.get("pin_assignments") or []:
                _resolve_pin_assignment(pa, instance, set(), kb, spec)
            return
        if src == "unknown-family":
            raise ResolveError(
                f"目前不支援「{instance}」這個週邊，它不在 STM32MP257 的腳位表裡。")
        raise ResolveError(
            f"{token}: 無法展開成有效訊號（{src}）；{problems}")
    _add_required(spec, valid)
    inst_signals = set(valid)            # this peripheral's signals, mode-applied

    for pa in item.get("pin_assignments") or []:
        _resolve_pin_assignment(pa, instance, inst_signals, kb, spec)


def _pick_canonical(cands: list[str], instance: str, profiles: dict) -> str:
    """Among AF aliases for one pad (e.g. USART6_RTS / USART6_DE), pick the
    signal a family profile actually names, else the alphabetically-first.
    RTS appears in usart's async_flow mode; DE does not -> RTS wins."""
    if len(cands) == 1:
        return cands[0]
    fam = _split_family(instance, profiles["families"])
    if fam:
        known = set()
        for suffixes in profiles["families"][fam[2]]["modes"].values():
            known.update(suffixes)
        prefer = [c for c in cands if c[len(instance) + 1:] in known]
        if prefer:
            return sorted(prefer)[0]
    return sorted(cands)[0]


def _resolve_pin_assignment(pa: dict, instance: str, inst_signals: set,
                            kb: Knowledge, spec: SolveSpec) -> None:
    pin = (pa.get("pin") or "").upper()
    if not pin:
        return                            # nothing to bind; signal already required
    on_pin = kb.af.get(pin)
    if on_pin is None:
        raise ResolveError(f"{pin}: not a GPIO pin in the AF table")

    signal = pa.get("signal")
    af = pa.get("af")
    if signal:                            # explicit signal: trust + validate
        signal = signal.upper()
        if signal not in inst_signals:
            raise ResolveError(
                f"{pin}: signal '{signal}' is not part of {instance}'s signals")
        if signal not in on_pin:
            raise ResolveError(
                f"腳位衝突：{pin} 接不了 '{signal}' {_conflict_hint(pin, signal, kb)}")
        chosen = signal
    elif af is not None:                  # AF number given: look it up directly
        atoms = kb.af_map.get(pin, {}).get(int(af))
        if not atoms:
            raise ResolveError(f"{pin}: AF{af} has no signal in the AF table")
        mine = [s for s in atoms if _instance_of(s) == instance]
        if not mine:
            raise ResolveError(
                f"{pin}: AF{af} = {atoms}, none belongs to {instance}")
        chosen = _pick_canonical(mine, instance, kb.profiles)
        # An AF-pinned signal may live outside the default mode (e.g. USART6_RTS
        # while mode=async); the explicit pin is authoritative, so require it.
        _add_required(spec, [chosen])
    else:                                 # infer by INTERSECTION (no AF needed)
        cands = on_pin & inst_signals
        if len(cands) == 1:
            chosen = next(iter(cands))
        elif not cands:
            same = sorted(s for s in on_pin if _instance_of(s) == instance)
            raise ResolveError(
                f"{pin}: carries no {instance} signal (pin has: {same or '—'})")
        else:                             # genuine residual: ask for an AF number
            raise ResolveError(
                f"{pin}: ambiguous for {instance} -> {sorted(cands)}; "
                f"specify an AF number to disambiguate.")
    _bind(spec, pin, chosen)


# --------------------------------------------------------------------------- #
# The three solver hard-rules (Instance.validate is the backstop)
# --------------------------------------------------------------------------- #
def _finalize(spec: SolveSpec) -> None:
    req = set(spec.required)
    # rule 1 — must_bind targets must be in required (add if missing).
    for sig in spec.must_bind.values():
        if sig not in req:
            spec.required.append(sig)
            req.add(sig)
    # rule 2 — injective on signals (distinct pins -> distinct signals).
    seen: dict[str, str] = {}
    for pin, sig in spec.must_bind.items():
        if sig in seen:
            raise ResolveError(
                f"must_bind not injective: '{sig}' bound to both "
                f"{seen[sig]} and {pin}")
        seen[sig] = pin
    # rule 3 — a forced pin cannot also be GPIO-locked.
    clash = set(spec.must_bind) & spec.must_gpio
    if clash:
        raise ResolveError(
            f"pins are both must_bind and must_gpio: {sorted(clash)}")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def normalize_items(intent: dict) -> dict:
    """Lower an under-specified 'peripheral' item — a family with NO instance
    number (the LLM parsed "SPI" / "SPI 要有 PZ2" without a number) — into a
    count(×1) of that family, carrying any pinned pads as the count's pin pool.

    Rationale: 'one unnamed SPI' IS a count. The LLM cannot map a pad to an
    instance (it has no AF table), so a bare family + pad must not become a
    peripheral with a missing instance (which crashes the resolver). As a count
    it flows through the existing count machinery: with a pinned pad it becomes a
    count_pin clarify (scan the pad for the family's signals -> the pick fixes the
    instance); with no pad the instance is auto-selected. Idempotent (a count item
    is left untouched), and a no-op intent is returned unchanged so existing flows
    are byte-identical. Must be applied identically by validate_clarify / plan /
    apply_answer so a clarify's item_index stays consistent across rounds."""
    items = intent.get("items") or []
    out, changed = [], False
    for it in items:
        if (it.get("level") or "").lower() == "peripheral" and not (it.get("instance") or "").strip():
            pins = [pa.get("pin") for pa in (it.get("pin_assignments") or []) if pa.get("pin")]
            out.append({
                "level": "count", "family": it.get("family"), "mode": it.get("mode"),
                "count": 1, "pins": pins, "pin_mode": "required" if pins else None,
                "af": None,
            })
            changed = True
        else:
            out.append(it)
    if not changed:
        return intent
    new = dict(intent)
    new["items"] = out
    return new


def resolve(intent: dict, kb: Knowledge | None = None, *, must_gpio=None,
            reserved_instances=None) -> SolveSpec:
    """Intent IR (dict) -> validated SolveSpec. Raises ResolveError on failure.

    reserved_instances: reserve_only group names from require.json (see
    util.dataio.load_reserved) — requests touching them are refused."""
    kb = kb or load_knowledge()
    reserved = frozenset(str(r).upper() for r in (reserved_instances or ()))
    spec = SolveSpec(must_gpio=set(must_gpio or ()))

    if intent.get("bootable_default") and not (intent.get("items") or []):
        # Pure official-default request: the boot-essential set is handled
        # downstream (require.json); nothing to resolve into signals here.
        # With items present（「官方 plan 再加…」）the flag is additive — the
        # official rows arrive as injected signal items (service._inject_official)
        # and everything resolves normally.
        return spec

    for item in intent.get("items") or []:
        level = (item.get("level") or "").lower()
        if level == "signal":
            _resolve_signal(item, kb, spec, reserved)
        elif level == "peripheral":
            _resolve_peripheral(item, kb, spec, reserved)
        elif level == "count":
            raise ResolveError(
                "count-level item must be lowered into concrete peripheral "
                "items by the candidate-generation layer before resolving "
                f"(got family={item.get('family')}, count={item.get('count')}).")
        else:
            raise ResolveError(f"unknown item level: {level!r}")

    _finalize(spec)
    return spec


def to_instance(spec: SolveSpec, kb: Knowledge) -> Instance:
    return Instance(af=kb.af, required=list(spec.required),
                    must_gpio=set(spec.must_gpio), must_bind=dict(spec.must_bind))


def solve(intent: dict, kb: Knowledge | None = None, *, must_gpio=None):
    """Resolve then solve. Returns (SolveSpec, Result)."""
    kb = kb or load_knowledge()
    spec = resolve(intent, kb, must_gpio=must_gpio)
    return spec, PinAssign(to_instance(spec, kb)).solve()


# --------------------------------------------------------------------------- #
# Self-test / demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    kb = load_knowledge()

    examples = [
        ("peripheral + pin, signal inferred by INTERSECTION (no AF used)", {
            "request_type": "peripheral",
            "items": [{
                "level": "peripheral", "family": "SPI", "instance": "SPI8",
                "mode": None,
                "pin_assignments": [
                    {"signal": None, "pin": "PZ2", "af": None},
                    {"signal": None, "pin": "PZ0", "af": None},
                ],
            }],
        }),
        ("signal level, one signal + one pin", {
            "request_type": "signal",
            "items": [{
                "level": "signal", "family": "ETH", "signal": "ETH1_MDC",
                "pin": None, "af": None, "pin_mode": None,
            }],
        }),
        ("peripheral, plain expand (no pins)", {
            "request_type": "peripheral",
            "items": [{
                "level": "peripheral", "family": "I2C", "instance": "I2C2",
                "mode": None, "pin_assignments": [],
            }],
        }),
    ]

    for title, intent in examples:
        print("=" * 72)
        print(title)
        try:
            spec, res = solve(intent, kb)
        except ResolveError as exc:
            print("  ResolveError:", exc)
            continue
        print("  required :", spec.required)
        print("  must_bind:", spec.must_bind)
        if spec.notes:
            print("  notes    :", spec.notes)
        print("  SAT      :", res.sat)
        if res.sat:
            for sig in spec.required:
                print(f"    {sig:<22} -> {res.assignment[sig]}")
        else:
            print("  reason   :", res.reason)
