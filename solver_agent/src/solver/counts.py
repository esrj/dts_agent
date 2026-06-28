"""
counts.py — candidate-generation layer: lower "count" requests into concrete
peripheral instances, then hand a fully-resolved SolveSpec to the solver.

A count item ("我要兩個 I2C") only says *how many* of a family, not *which*
instances.  This layer searches the instance space:

  slots      : a count of N for family F becomes N "slots", each to be filled
               with a distinct concrete instance of F (I2C2, I2C3, ...).
  domain     : a slot's domain is every instance of F that exists in Σ and can
               be fully realised in the requested mode (no missing signals).
  ordering   : instances are tried official-DTS-first, then by index ascending
               (so 2×FDCAN tries {FDCAN1, FDCAN3} — the board defaults — first).
               This is a *soft* preference: if the official combination can't be
               routed, DFS backtracks to non-official instances and still solves.
  search     : DFS over the slots; after every pick the accumulated signal union
               is run through the real CSP solver.  Because UNSAT is monotone
               (adding required signals never makes an infeasible set feasible),
               an UNSAT partial prunes the whole subtree.  The first full-depth
               SAT combination wins.

Already-specified peripheral/signal items (a "mixed" request) become the fixed
prefix: they are resolved once, proven routable, and every count combination is
solved on top of them — so instance choice is arbitrated globally, against the
user's pinned signals as well as against the other count families.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solver.peripherals import _apply_alias, _split_family, expand_one
from solver.resolver import (
    Knowledge, ResolveError, SolveSpec, load_knowledge, normalize_items, resolve,
    to_instance,
)
from solver.solve import Instance, PinAssign


# --------------------------------------------------------------------------- #
# Slot model
# --------------------------------------------------------------------------- #
@dataclass
class _Slot:
    """One instance to be chosen for a family."""
    group: int                       # which count item this slot belongs to
    family_key: str                  # profiles family key (e.g. "i2c")
    domain: list[str]                # ordered instance tokens (official-first)
    sigs: dict[str, list[str]]       # instance token -> its expanded signals
    rank: dict[str, tuple]           # instance token -> sort key (tier, index)
    pred: int | None                 # earlier slot of the same family (symmetry)


@dataclass
class CountPlan:
    """Result of lowering a count/mixed request."""
    spec: SolveSpec                            # final solver-facing spec
    chosen: list[dict] = field(default_factory=list)  # per-family auto-selection


# --------------------------------------------------------------------------- #
# Family / instance helpers (all derived from Σ + profiles, no new data files)
# --------------------------------------------------------------------------- #
def _family_key(family: str, profiles: dict) -> str | None:
    """'CAN'/'I2C'/'ETH' -> profiles family key ('fdcan'/'i2c'/'eth')."""
    name = _apply_alias((family or "").upper(), profiles.get("aliases", {}))
    if name.lower() in profiles["families"]:
        return name.lower()
    fam = _split_family(name, profiles["families"])   # e.g. "OCTOSPI1" -> octospi
    return fam[2] if fam else None


def _instances_in_sigma(key: str, sigma: set, profiles: dict) -> list[str]:
    """Every instance token of family `key` that appears in Σ, index-sorted."""
    families = profiles["families"]
    found = set()
    for sig in sigma:
        fam = _split_family(sig.split("_", 1)[0], families)
        if fam and fam[2] == key:
            found.add(sig.split("_", 1)[0])
    return sorted(found, key=lambda t: int(_split_family(t, families)[1]))


def _official_instances(key: str, dts_index: dict, profiles: dict) -> set:
    """Instances of family `key` enabled in the official DTS (preference set)."""
    families = profiles["families"]
    out = set()
    for tok in dts_index:
        fam = _split_family(tok, families)
        if fam and fam[2] == key:
            out.add(tok)
    return out


def _index_of(tok: str, profiles: dict) -> int:
    return int(_split_family(tok, profiles["families"])[1])


# Boot-essential prefix items are injected by service._inject_boot tagged with
# one of these sources; a count deducts the same-family ones it already supplies.
_BOOT_SOURCES = {"board_pin_locked", "boot_required"}


def _boot_provided(fixed_items: list[dict], kb: Knowledge) -> dict[str, set]:
    """Family key -> instance tokens the board's boot-essential set supplies.

    Read purely from the injected, source-tagged prefix items, so it is
    board-driven (whatever require.json injected for THIS board) and never
    hard-coded — switching board switches the deduction with no prompt change.
    Tokens that don't resolve to a known family (e.g. OCTOSPIM) are skipped: they
    match no count domain, so there is nothing to deduct against.
    """
    families = kb.profiles["families"]
    out: dict[str, set] = {}
    for it in fixed_items:
        if it.get("source") not in _BOOT_SOURCES:
            continue
        sig = (it.get("signal") or "").upper()
        if not sig:
            continue
        tok = sig.split("_", 1)[0]
        fam = _split_family(tok, families)
        if fam:
            out.setdefault(fam[2], set()).add(tok)
    return out


# --------------------------------------------------------------------------- #
# Slot construction (+ magnitude precheck)
# --------------------------------------------------------------------------- #
def _build_slots(count_items: list[dict], used: set, kb: Knowledge,
                 boot_provided: dict[str, set] | None = None,
                 reserved: frozenset = frozenset()) -> list[_Slot]:
    """Lower count items into ordered, domain-filled slots.

    `used` are instance tokens already taken by the fixed prefix; they are
    removed from every domain so a count never re-selects a pinned instance.

    Boot / secure instances can never satisfy a general request (require.json
    policy "reserve_peripheral_instance"): emit-group instances (SDMMC1/2,
    USART2, …) are already excluded via `used` — their injected signals sit in
    the fixed prefix — and reserve_only ones (I2C7, …) arrive in `reserved`.
    A count therefore always allocates NEW instances, no deduction. Both sets
    come from this board's require.json, nothing hard-coded; `boot_provided`
    is kept for the magnitude error message only (which instances are taken).
    Raises ResolveError when a family is unknown or the request is
    unsatisfiable (more instances needed than the family has free — the
    "我要 20 個 I2C" guard).
    """
    profiles = kb.profiles
    boot_provided = boot_provided or {}
    slots: list[_Slot] = []
    last_of_family: dict[str, int] = {}

    for gi, item in enumerate(count_items):
        family = item.get("family") or ""
        count = int(item.get("count") or 0)
        mode = item.get("mode")
        if count <= 0:
            continue

        key = _family_key(family, profiles)
        if key is None:
            raise ResolveError(f"未知的 peripheral family：{family!r}")

        # Domain: in-Σ instances, minus those already used. The profile is a HINT
        # (which signals to turn on), NOT a gate: keep the mode's signals that
        # actually exist in Σ and drop only the few an instance lacks (e.g. ETH3 has
        # no MDC/MDIO/RGMII_CLK125). Skip an instance only when it has NO realisable
        # signal in this mode at all — so a partial instance still counts.
        official = _official_instances(key, kb.dts_index, profiles)
        domain, sigs_map, rank = [], {}, {}
        for tok in _instances_in_sigma(key, kb.sigma, profiles):
            if tok in used or tok in reserved:
                continue
            token = f"{tok}:{mode}" if mode else tok
            sigs, _src, _problems = expand_one(token, kb.dts_index, profiles, kb.sigma)
            valid = [s for s in sigs if s in kb.sigma]
            if not valid:
                continue                       # nothing realisable in this mode — skip
            domain.append(tok)
            sigs_map[tok] = valid              # use what exists (profile ≠ gate)
            rank[tok] = (0 if tok in official else 1, _index_of(tok, profiles))
        domain.sort(key=lambda t: rank[t])     # official-first, then index

        if count > len(domain):
            blocked = sorted(
                set(boot_provided.get(key, ())) |
                {t for t in reserved
                 if (_split_family(t, profiles["families"]) or (None, None, None))[2] == key},
                key=lambda t: _index_of(t, profiles))
            raise ResolveError(
                f"{family.upper()} 可另配 {len(domain)} 個：{domain or '—'}"
                f"（開機/secure 已保留，不可沿用：{blocked or '—'}），"
                f"無法滿足要求的 {count} 個。"
            )

        for _ in range(count):
            pred = last_of_family.get(key)
            slots.append(_Slot(group=gi, family_key=key, domain=domain,
                               sigs=sigs_map, rank=rank, pred=pred))
            last_of_family[key] = len(slots) - 1
    return slots


# --------------------------------------------------------------------------- #
# DFS over the slots, pruned by the real CSP solver
# --------------------------------------------------------------------------- #
def _solve(required: list[str], base: SolveSpec, kb: Knowledge):
    inst = Instance(af=kb.af, required=required,
                    must_gpio=set(base.must_gpio), must_bind=dict(base.must_bind))
    return PinAssign(inst).solve()


def _search(slots: list[_Slot], base: SolveSpec, kb: Knowledge) -> list[str] | None:
    """Return chosen instance tokens (aligned to slots), or None if infeasible."""
    chosen: list[str | None] = [None] * len(slots)

    def rec(i: int, acc: list[str]) -> list[str] | None:
        if i == len(slots):
            return []
        slot = slots[i]
        floor = slot.rank[chosen[slot.pred]] if slot.pred is not None else None
        taken = {c for c in chosen if c is not None}
        for tok in slot.domain:                       # already official-first sorted
            if floor is not None and slot.rank[tok] <= floor:
                continue                              # symmetry break: strictly after pred
            if tok in taken:
                continue
            merged = list(dict.fromkeys(acc + slot.sigs[tok]))
            if not _solve(merged, base, kb).sat:      # monotone UNSAT -> prune subtree
                continue
            chosen[i] = tok
            sub = rec(i + 1, merged)
            if sub is not None:
                return [tok] + sub
            chosen[i] = None
        return None

    return rec(0, list(base.required))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _summarize_choice(c: dict) -> str:
    """One '×N → …' note line; when the boot set covered part of the request,
    spell out which instances were reused vs newly allocated."""
    head = f"{c['family']}×{c['count']} → {', '.join(c['instances']) or '—'}"
    if not c["reused_boot"]:
        return head                       # nothing reused — the head says it all
    bits = [f"沿用開機必備 {', '.join(c['reused_boot'])}"]
    if c["new"]:
        bits.append(f"新配 {', '.join(c['new'])}")
    return head + f"（{'；'.join(bits)}）"


def _lower_standalone_counts(intent: dict, kb: Knowledge) -> dict:
    """Count items whose family is a STANDALONE (unnumbered) peripheral —
    declared per-instance in profiles["peripherals"] (PCIE, USBH, …) rather
    than the numbered-families model — cannot fill a count slot: there is no
    instance domain to search. Lower a count of 1 into a concrete peripheral
    item (instance = the name itself; pinned pads become pin_assignments the
    resolver intersects as usual). A count > 1 is a magnitude error, since a
    standalone name IS its single instance. Family counts are left untouched.
    """
    profiles = kb.profiles
    items = intent.get("items") or []
    out, changed = [], False
    for it in items:
        if (it.get("level") or "").lower() != "count":
            out.append(it)
            continue
        name = _apply_alias((it.get("family") or "").upper(), profiles.get("aliases", {}))
        if (_family_key(name, profiles) is not None
                or name not in (profiles.get("peripherals") or {})):
            out.append(it)
            continue
        n = int(it.get("count") or 0)
        if n <= 0:
            changed = True                  # zero-count: drop, same as slot building
            continue
        if n > 1:
            raise ResolveError(
                f"「{name}」是單一週邊（無多重 instance），共 1 個可用，"
                f"無法滿足要求的 {n} 個。")
        out.append({
            "level": "peripheral", "family": it.get("family"),
            "instance": name, "mode": it.get("mode"),
            "pin_assignments": [{"signal": None, "pin": p, "af": it.get("af")}
                                for p in (it.get("pins") or []) if p],
        })
        changed = True
    if not changed:
        return intent
    return {**intent, "items": out}


def plan(intent: dict, kb: Knowledge | None = None, *, must_gpio=None,
         reserved_instances=None) -> CountPlan:
    """Lower a count/mixed intent into a fully-resolved SolveSpec.

    No count items -> delegates straight to the resolver (chosen = []).
    Otherwise: resolve the fixed (peripheral/signal) prefix, prove it routable,
    then DFS-select instances for the count slots on top of it.

    reserved_instances: reserve_only group names from require.json — excluded
    from every count domain and refused by the resolver (kernel may not use
    them; see util.dataio.load_reserved).
    """
    kb = kb or load_knowledge()
    reserved = frozenset(str(r).upper() for r in (reserved_instances or ()))
    intent = normalize_items(intent)        # no-instance peripheral -> count (same as clarify)
    intent = _lower_standalone_counts(intent, kb)   # count of PCIE/USBH… -> peripheral item
    items = intent.get("items") or []
    count_items = [it for it in items if (it.get("level") or "").lower() == "count"]

    if not count_items:
        return CountPlan(spec=resolve(intent, kb, must_gpio=must_gpio,
                                      reserved_instances=reserved), chosen=[])

    # Fixed prefix = everything that is not a count item.
    fixed_items = [it for it in items if (it.get("level") or "").lower() != "count"]
    base = resolve({**intent, "items": fixed_items}, kb, must_gpio=must_gpio,
                   reserved_instances=reserved)

    if base.required and not _solve(list(base.required), base, kb).sat:
        res = _solve(list(base.required), base, kb)
        raise ResolveError(f"已指定的部分本身無法佈線：{res.reason}")

    used = {s.split("_", 1)[0] for s in base.required}
    boot_provided = _boot_provided(fixed_items, kb)    # for error messages only
    slots = _build_slots(count_items, used, kb, boot_provided, reserved)

    tokens = _search(slots, base, kb)
    if tokens is None:
        fams = sorted({s.family_key.upper() for s in slots})
        raise ResolveError(
            f"找不到可行的 instance 組合（count 需求 {fams} 與已指定條件衝突，"
            f"所有候選組合都無法佈線）")

    # Final spec: fixed prefix + the chosen instances' signals.
    spec = SolveSpec(required=list(base.required), must_gpio=set(base.must_gpio),
                     must_bind=dict(base.must_bind), notes=list(base.notes))
    seen = set(spec.required)
    for slot, tok in zip(slots, tokens):
        for s in slot.sigs[tok]:
            if s not in seen:
                seen.add(s)
                spec.required.append(s)

    # Per-family summary for the UI / notes. Boot/secure instances are never
    # reused for a general request (require.json "reserve_peripheral_instance"),
    # so every satisfying instance is newly allocated; reused_boot stays [] and
    # is kept only so the output shape is unchanged for the front-end.
    chosen: list[dict] = []
    for gi, item in enumerate(count_items):
        new = [tok for slot, tok in zip(slots, tokens) if slot.group == gi]
        chosen.append({
            "family": (item.get("family") or "").upper(),
            "count": int(item.get("count") or 0),
            "mode": item.get("mode"),
            "instances": new,             # every instance satisfying the request
            "reused_boot": [],            # boot instances are reserved, never reused
            "new": new,                   # newly allocated by the solver
        })
    spec.notes.append("count 自動選擇 — " + "；".join(
        _summarize_choice(c) for c in chosen))
    return CountPlan(spec=spec, chosen=chosen)


# --------------------------------------------------------------------------- #
# Self-test / demo
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    kb = load_knowledge()

    examples = [
        ("2 個 FDCAN（期望官方 FDCAN1, FDCAN3）", {
            "items": [{"level": "count", "family": "FDCAN", "mode": None,
                       "count": 2, "pins": [], "pin_mode": None}],
        }),
        ("2 個 I2C + 1 個 ETH", {
            "items": [
                {"level": "count", "family": "I2C", "mode": None, "count": 2},
                {"level": "count", "family": "ETH", "mode": None, "count": 1},
            ],
        }),
        ("mixed：1 個 FDCAN + 已指定 FDCAN1", {
            "items": [
                {"level": "peripheral", "family": "FDCAN", "instance": "FDCAN1",
                 "mode": None, "pin_assignments": []},
                {"level": "count", "family": "FDCAN", "mode": None, "count": 1},
            ],
        }),
        ("數量級不可能：20 個 I2C", {
            "items": [{"level": "count", "family": "I2C", "mode": None, "count": 20}],
        }),
    ]

    for title, intent in examples:
        print("=" * 72)
        print(title)
        try:
            cp = plan(intent, kb)
        except ResolveError as exc:
            print("  ResolveError:", exc)
            continue
        for c in cp.chosen:
            print(f"  {c['family']}×{c['count']} -> {c['instances']}")
        print("  required:", cp.spec.required)
        res = _solve(list(cp.spec.required), cp.spec, kb)
        print("  SAT:", res.sat, f"(k={res.k}, propagated={res.propagated})")






