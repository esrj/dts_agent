from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# --------------------------------------------------------------------------- #
# Problem instance  I = (P, Sigma, AF, R, G, mu)   (Definition 1.1)
# --------------------------------------------------------------------------- #
@dataclass
class Instance:
    """A pin-assignment instance I = (P, Sigma, AF, R, G, mu)."""

    af: Dict[str, Set[str]]          # AF : P -> 2^Sigma   (pin -> available signals)
    required: List[str]              # R   subset of Sigma  (signals to place)
    must_gpio: Set[str] = field(default_factory=set)   # G  subset of P
    must_bind: Dict[str, str] = field(default_factory=dict)  # mu : P -> R (pin -> signal)

    @property
    def pins(self) -> Set[str]:
        """P — the set of GPIO-capable pins."""
        return set(self.af.keys())

    @property
    def signals(self) -> Set[str]:
        # 把 af table 中所有 pin 的 signal 集合起來
        return {s for sigs in self.af.values() for s in sigs}

    def validate(self) -> None:
        """Sanity-check the instance against Definitions 1.1 / 1.2."""
        # mu must be a partial *injection* P -> R: distinct pins -> distinct signals.
        seen: Dict[str, str] = {}
        for pin, sig in self.must_bind.items():
            if sig in seen:
                raise ValueError(
                    f"must_bind is not injective: '{sig}' bound to both "
                    f"'{seen[sig]}' and '{pin}'."
                )
            seen[sig] = pin
        # Forced bindings must target required signals.
        req = set(self.required)
        for pin, sig in self.must_bind.items():
            if sig not in req:
                raise ValueError(
                    f"must_bind targets '{sig}' which is not in the required set R."
                )
        # A forced pin cannot also be GPIO-locked.
        clash = set(self.must_bind) & self.must_gpio
        if clash:
            raise ValueError(f"pins are both must_bind and must_gpio: {sorted(clash)}")


# --------------------------------------------------------------------------- #
# Solver result
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    sat: bool
    assignment: Dict[str, str] = field(default_factory=dict)  # signal -> pin 對應
    reason: str = ""                       # explanation when UNSAT
    k: int = 0                             # |R'| 剩餘要 backtrack 的 signals 數量
    propagated: int = 0                    # signals resolved purely by AC
    nodes: int = 0                         # backtracking node evaluations
    elapsed: float = 0.0                   # wall-clock seconds
    unknown: List[str] = field(default_factory=list)  # required names absent from Σ


# --------------------------------------------------------------------------- #
# Algorithm 1 : PinAssign(I)
# --------------------------------------------------------------------------- #
class PinAssign:
    """Signal-first CSP solver implementing Algorithm 1 of the paper."""

    def __init__(self, instance: Instance):
        self.I = instance
        self.nodes = 0

    # -- main entry point ---------------------------------------------------- #
    def solve(self) -> Result:
        I = self.I
        I.validate()
        t0 = time.perf_counter()

        # De-duplicate R while preserving order: a signal listed twice is one
        # variable, not two (otherwise it would be assigned two distinct pins).
        seen: Set[str] = set()
        required = [s for s in I.required if not (s in seen or seen.add(s))]

        # Pre-flight: signals that appear in NO pin's AF set are not "infeasible"
        # in the interesting sense — they are unknown names (typo / wrong naming
        # convention). Report them distinctly instead of as a bare empty domain.
        universe = I.signals
        unknown = [s for s in required if s not in universe]
        if unknown:
            return self._unsat(
                f"{len(unknown)} required signal(s) are not in Σ (unknown name "
                f"or wrong naming convention): {unknown}", t0, unknown=unknown)

        # ----- Phase 1 | Domain initialisation (lines 1-3) ------------------ #
        #   D(sigma) <- { p in P \ G | sigma in AF(p) }
        domains: Dict[str, Set[str]] = {}
        for sigma in required:
            domains[sigma] = {
                p for p in I.pins
                if p not in I.must_gpio and sigma in I.af.get(p, set())
            }
            if not domains[sigma]:
                return self._unsat(
                    f"Phase 1: signal '{sigma}' has no candidate pin "
                    f"(empty domain).", t0)

        # ----- Phase 2 | must_bind initialisation (lines 4-7) --------------- #
        #   forced : P -> R  is a partial map of committed pins.
        forced: Dict[str, str] = {}          # pin -> signal
        for pin, sigma in I.must_bind.items():
            if pin not in domains[sigma]:     # line 6: p* not in D(sigma*)
                return self._unsat(
                    f"Phase 2: forced binding {pin} -> {sigma} is infeasible "
                    f"(pin not a candidate for the signal).", t0)
            forced[pin] = sigma               # line 7
            domains[sigma] = {pin}            # commit: collapse domain to singleton

        # ----- Phase 3 | AC propagation = unit propagation (lines 8-15) ----- #
        #   Repeat Eq. (10) until quiescence.
        while True:
            changed = False
            forced_pins = set(forced)
            forced_sigs = set(forced.values())
            for sigma in required:
                if sigma in forced_sigs:           # sigma already in range(forced)
                    continue
                before = len(domains[sigma])
                domains[sigma] -= forced_pins      # line 10: D <- D \ dom(forced)
                if not domains[sigma]:             # line 11
                    return self._unsat(
                        f"Phase 3: domain of '{sigma}' wiped out by "
                        f"propagation.", t0)
                if len(domains[sigma]) != before:
                    changed = True
                if len(domains[sigma]) == 1:       # line 12: singleton -> force
                    p_star = next(iter(domains[sigma]))
                    forced[p_star] = sigma         # line 14
                    forced_pins.add(p_star)
                    forced_sigs.add(sigma)
                    changed = True
            if not changed:                        # line 15: until no domain changed
                break

        # ----- Phase 4 | Early termination (lines 16-17) -------------------- #
        forced_sigs = set(forced.values())
        residual = [s for s in required if s not in forced_sigs]  # R'
        propagated = len(required) - len(residual)

        if not residual:                           # R' = empty -> done, k = 0
            assignment = {sig: pin for pin, sig in forced.items()}
            return self._sat(assignment, k=0, propagated=propagated, t0=t0)

        # ----- Phase 5 | Backtracking on the reduced sub-problem (18-21) ---- #
        # Order R' by |D(sigma)| ascending  (Minimum Remaining Values, MRV).
        residual.sort(key=lambda s: len(domains[s]))
        used: Set[str] = set(forced)               # pins already taken
        partial: Dict[str, str] = {}               # f* : R' -> P
        self.nodes = 0

        if self._backtrack(residual, 0, domains, used, partial):
            assignment = {sig: pin for pin, sig in forced.items()}
            assignment.update(partial)             # forced  union  f*
            return self._sat(assignment, k=len(residual),
                             propagated=propagated, t0=t0)

        return self._unsat(
            "Phase 5: no AllDifferent assignment exists for the residual "
            "sub-problem (backtracking exhausted).", t0,
            k=len(residual), propagated=propagated)

    # -- complete, sound backtracking search over AllDifferent --------------- #
    def _backtrack(self, order: List[str], i: int,
                   domains: Dict[str, Set[str]], used: Set[str],
                   partial: Dict[str, str]) -> bool:
        if i == len(order):
            return True
        self.nodes += 1
        sigma = order[i]
        # Try candidate pins not already used (AllDifferent).
        for pin in sorted(domains[sigma] - used):
            partial[sigma] = pin
            used.add(pin)
            if self._backtrack(order, i + 1, domains, used, partial):
                return True
            used.discard(pin)
            del partial[sigma]
        return False

    # -- result helpers ------------------------------------------------------ #
    def _sat(self, assignment, k, propagated, t0) -> Result:
        return Result(sat=True, assignment=assignment, k=k,
                      propagated=propagated, nodes=self.nodes,
                      elapsed=time.perf_counter() - t0)

    def _unsat(self, reason, t0, k=0, propagated=0, unknown=None) -> Result:
        return Result(sat=False, reason=reason, k=k, propagated=propagated,
                      nodes=self.nodes, elapsed=time.perf_counter() - t0,
                      unknown=unknown or [])


# --------------------------------------------------------------------------- #
# Hall's-condition witness (Corollary 3.1) — explains *why* an instance is
# infeasible by exhibiting a deficient set S with |N(S)| < |S|.
# --------------------------------------------------------------------------- #
def hall_violator(instance: Instance) -> Optional[Tuple[List[str], Set[str]]]:
    """Return (S, N(S)) violating Hall's condition, or None if none is found
    cheaply.  Uses a maximum bipartite matching; a signal left unmatched
    certifies infeasibility, and its reachable set forms the deficient S."""
    # Build domains (signal -> candidate pins), mirroring Phase 1.
    dom: Dict[str, Set[str]] = {}
    for sigma in instance.required:
        dom[sigma] = {
            p for p in instance.pins
            if p not in instance.must_gpio and sigma in instance.af.get(p, set())
        }
    # Hopcroft–Karp would be faster; Kuhn's algorithm is plenty for |R| <= 30.
    match_pin: Dict[str, str] = {}   # pin -> signal

    def try_kuhn(sig: str, seen: Set[str]) -> bool:
        for pin in dom[sig]:
            if pin in seen:
                continue
            seen.add(pin)
            if pin not in match_pin or try_kuhn(match_pin[pin], seen):
                match_pin[pin] = sig
                return True
        return False

    unmatched = None
    for sig in sorted(instance.required, key=lambda s: len(dom[s])):
        if not try_kuhn(sig, set()):
            unmatched = sig
            break
    if unmatched is None:
        return None  # perfect matching exists -> feasible

    # Alternating-tree reachable set from the unmatched signal gives a
    # deficient S (a standard König / Hall certificate).
    S: Set[str] = set()
    NS: Set[str] = set()
    stack = [unmatched]
    while stack:
        sig = stack.pop()
        if sig in S:
            continue
        S.add(sig)
        for pin in dom[sig]:
            if pin in NS:
                continue
            NS.add(pin)
            if pin in match_pin:
                stack.append(match_pin[pin])
    return sorted(S), NS


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def load_af_table(path: str) -> Dict[str, Set[str]]:
    """Load and normalise af_table.json into  pin -> set(signal names).

    Accepts the two common layouts:
        { "PD3": ["UART7_TX", "SPI3_SCK"] }
        { "PD3": {"7": "UART7_TX", "5": "SPI3_SCK"} }
    Entries of the form "AF7:UART7_TX" are normalised to "UART7_TX".
    Multi-mode cells "SPI2_SCK/I2S2_CK" (as found in DS14284) are split on
    '/' into the individual signal names SPI2_SCK and I2S2_CK.
    """
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    def explode(value):                # one cell -> individual signal names
        text = str(value).strip()
        if ":" in text:                # "AF7:UART7_TX" -> "UART7_TX"
            text = text.split(":", 1)[1].strip()
        for part in text.split("/"):   # "SPI2_SCK/I2S2_CK" -> two signals
            sig = part.strip()
            if sig and sig != "-" and sig.upper() != "GPIO":
                yield sig

    table: Dict[str, Set[str]] = {}
    for pin, sigs in raw.items():
        if isinstance(sigs, dict):
            values = sigs.values()
        elif isinstance(sigs, (list, tuple, set)):
            values = sigs
        else:
            values = [sigs]
        cleaned: Set[str] = set()
        for v in values:
            cleaned.update(explode(v))
        table[pin] = cleaned
    return table


def load_request(path: str) -> Tuple[List[str], Set[str], Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as fh:
        req = json.load(fh)
    required = list(req.get("required", []))
    must_gpio = set(req.get("must_gpio", []))
    must_bind = dict(req.get("must_bind", {}))
    return required, must_gpio, must_bind

# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def report(instance: Instance, result: Result) -> None:
    print("=" * 64)
    print("  GPIO Pin Assignment — Signal-First CSP Solver ")
    print("=" * 64)
    print(f"  |P| pins              : {len(instance.pins)}")
    print(f"  |Sigma| signals       : {len(instance.signals)}")
    print(f"  |R| required signals  : {len(instance.required)}")
    print(f"  |G| GPIO-locked pins  : {len(instance.must_gpio)}")
    print(f"  |mu| forced bindings  : {len(instance.must_bind)}")
    print("-" * 64)

    if result.sat:
        print("  RESULT: SAT — feasible assignment f : R -> P")
        print(f"  AC-propagated (degree-1 / cascaded) : {result.propagated}")
        print(f"  k (residual backtracking variables) : {result.k}")
        print(f"  backtracking node evaluations       : {result.nodes}")
        print(f"  wall-clock time                     : {result.elapsed*1e3:.3f} ms")
        print("-" * 64)
        for sigma in instance.required:
            print(f"    {sigma:<24} -> {result.assignment[sigma]}")
    elif result.unknown:
        print("  RESULT: UNSAT — unknown signal name(s)")
        print(f"  {len(result.unknown)} of {len(instance.required)} required "
              f"signals are not in Σ (check naming convention):")
        for s in result.unknown:
            print(f"    ✗ {s}")
    else:
        print("  RESULT: UNSAT — no feasible assignment exists")
        print(f"  reason: {result.reason}")
        witness = hall_violator(instance)
        if witness is not None:
            S, NS = witness
            print("-" * 64)
            print("  Hall-condition violation (Corollary 3.1): |N(S)| < |S|")
            print(f"    S      ({len(S)}) = {S}")
            print(f"    N(S)   ({len(NS)}) = {sorted(NS)}")
    print("=" * 64)

