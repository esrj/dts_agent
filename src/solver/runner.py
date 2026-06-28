"""
runner.py — orchestration layer: tie data I/O + expansion + solver together.

  solve_signals      — solve a signal-level request (R given directly)
  solve_peripherals  — expand a peripheral-level request, then solve

Both return (required, result) and write the plan as a side effect.
"""
import json

from util.dataio import DTS, PROFILES, sigma_of, write_plan
from solver.peripherals import build_dts_index, expand, load_profiles
from solver.solve import Instance, PinAssign


def _load_dts_index(path):
    """official_dts_peripheral.json -> {peripheral_token: [signals]}."""
    with open(path, encoding="utf-8") as fh:
        return build_dts_index(json.load(fh)["peripherals"])


def solve_signals(af, required, must_gpio=None, must_bind=None, af_map=None):
    """Solve a request that already lists concrete signals.

    af_map (pin -> {af_int: [signals]}) is forwarded to write_plan so the CSV's
    `af` column is board-correct; when None, write_plan falls back to the default
    board's AF table.
    """
    instance = Instance(af=af, required=required,
                        must_gpio=set(must_gpio or ()),
                        must_bind=dict(must_bind or {}))
    result = PinAssign(instance).solve()
    write_plan(required, result, af_map)
    return required, result


def solve_peripherals(af, peripherals, must_gpio=None, must_bind=None, af_map=None):
    """Expand peripheral tokens into signals, then solve.

    Each token resolves via the official DTS set if enabled there, else the
    family profile (peripheral_profiles.json); ":mode" overrides the default.
    """
    sigma = sigma_of(af)
    profiles = load_profiles(PROFILES)
    dts_index = _load_dts_index(DTS)

    required, prov, problems = expand(peripherals, dts_index, profiles, sigma)

    # print(f"peripheral request: {len(peripherals)} peripherals "
    #       f"-> {len(required)} signals")
    # for tok in dict.fromkeys(peripherals):          # de-dup display, keep order
    #     sigs = [s for s in required if prov[s][0] == tok]
    #     src = prov[sigs[0]][1] if sigs else "—"
    #     print(f"   {tok:<18} [{src}]  {len(sigs)} signals")
    # if problems:
    #     print("\n problems:")
    #     for p in problems:
    #         print(f"    - {p}")
    # print()

    return solve_signals(af, required, must_gpio, must_bind, af_map)
