"""
clarify.py — under-specification detector + KB-grounded clarification.

Some requests are ambiguous or self-contradictory in a way the solver can't
silently resolve, e.g. "我要 ETH 兩個，要在 PA0 上": a count says *how many*,
never *which pin*, yet a pad was pinned.  Rather than guess or hard-fail, we
detect the issue deterministically (we have the AF table / profiles), compute
the *real* candidate completions from Σ, and surface a narrow confirm question.

Division of labour (the whole point):
  * DETECTION + CANDIDATES  : deterministic, KB-aware  -> validate_clarify()
  * PHRASING                : the LLM only renders a precomputed option list
                              into one friendly sentence -> phrase_question().
                              It never sees the knowledge base, so it cannot
                              wander off and ask irrelevant things.
  * FOLD-BACK               : the user's pick maps deterministically onto the
                              intent (no LLM) -> apply_answer().

validate_clarify(intent, kb) -> Ready(intent) | Clarify(questions)

Detected kinds:
  count_pin     — a count item also pins a pad (count can't bind a pin).
  af_mismatch   — a pin_assignment's AF number names a signal that does NOT
                  belong to the requested peripheral instance.
  ambiguous_pin — a pin_assignment with no AF where the pad carries more than
                  one of the instance's signals.
  shared_pin    — several signal items pin the SAME pad (a pad carries one
                  signal); ask which signal that pad should carry.
  loose_pin     — a pad the user wants used (loose_pins / unresolved) but tied to
                  no signal; scan it against the whole request and ask which
                  requested signal that pad should serve.
"""
from __future__ import annotations

import copy
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solver.peripherals import _split_family
from solver.resolver import (
    Knowledge, ResolveError, _instance_of, load_knowledge, normalize_items,
    reserved_owner,
)
from solver.counts import _family_key


# --------------------------------------------------------------------------- #
# Result / question model
# --------------------------------------------------------------------------- #
@dataclass
class Option:
    """One KB-grounded completion the user can confirm."""
    label: str
    pin: str | None = None
    instance: str | None = None
    signal: str | None = None
    af: int | None = None
    mode: str | None = None
    note: str = ""
    bindings: list = field(default_factory=list)   # multi-signal folds: [{signal,pin,af}]


@dataclass
class Question:
    kind: str                                  # count_pin | count_af | af_mismatch | ambiguous_pin
    span: str                                  # the bit of the request it's about
    summary: str                               # deterministic description of the issue
    options: list[Option] = field(default_factory=list)
    item_index: int = -1                       # intent item that triggered it (fold-back)
    pin: str | None = None


@dataclass
class Clarify:
    questions: list[Question]


@dataclass
class Ready:
    intent: dict


# --------------------------------------------------------------------------- #
# KB queries (all derived from the AF table + profiles)
# --------------------------------------------------------------------------- #
def _candidates_on_pin(pin: str, family_key: str, kb: Knowledge,
                       instance: str | None = None) -> list[tuple[int, str]]:
    """[(af, signal)] that `pin` can carry for the family (or a given instance)."""
    families = kb.profiles["families"]
    out = []
    for af, sigs in kb.af_map.get(pin, {}).items():
        for s in sigs:
            tok = s.split("_", 1)[0]
            fam = _split_family(tok, families)
            if fam and fam[2] == family_key and (instance is None or tok == instance):
                out.append((int(af), s))
    return sorted(out)


def _mode_of(signal: str, instance: str, family_key: str, profiles: dict):
    """Which profile mode names this signal, and is it the default?  (None if
    the signal lives outside every profile mode — e.g. ETH MII vs rgmii/rmii)."""
    fam = profiles["families"][family_key]
    suffix = signal[len(instance) + 1:]
    for mname, suffixes in fam["modes"].items():
        if suffix in suffixes:
            return mname, mname == fam["default"]
    return None, False


def _families_on_pin(pin: str, kb: Knowledge) -> list[str]:
    return sorted({s.split("_", 1)[0] for s in kb.af.get(pin, set())})


def _instances_of(family_key: str, kb: Knowledge) -> list[str]:
    """Every instance token of `family_key` present in Σ, index-sorted."""
    families = kb.profiles["families"]
    found = {s.split("_", 1)[0] for s in kb.sigma
             if (_split_family(s.split("_", 1)[0], families) or (None, None, None))[2] == family_key}
    return sorted(found, key=lambda t: int(_split_family(t, families)[1]))


def _af_candidates(family_key: str, af: int, kb: Knowledge, *, mode: str | None = None,
                   exclude: set | None = None,
                   taken: set | None = None) -> list[tuple[str, list[dict]]]:
    """Instances of `family_key` fully realisable on `af` (no named pin).

    An instance qualifies iff every signal of its chosen mode (item mode, else
    family default) has a *free* pin exposing it on `af` — free meaning not a
    forced-GPIO pad, not already pinned by another item, and distinct from the
    other signals of the same instance (so the resulting must_bind is injective
    and won't collide with the rest of the request).  Returns [(instance,
    bindings)] with each signal pinned to its lowest such free AF pad.
    """
    exclude = exclude or set()
    taken = set(taken or ())
    fam = kb.profiles["families"][family_key]
    modes = fam["modes"]
    suffixes = modes.get(mode, modes[fam["default"]])
    out = []
    for inst in _instances_of(family_key, kb):
        if inst in exclude:
            continue
        bindings, ok = [], True
        chosen: set = set()                      # pads picked for THIS instance (distinctness)
        for suf in suffixes:
            sig = f"{inst}_{suf}"
            pins = sorted(p for p, m in kb.af_map.items()
                          if sig in m.get(af, []) and p not in taken and p not in chosen)
            if not pins:
                ok = False
                break
            chosen.add(pins[0])
            bindings.append({"signal": sig, "pin": pins[0], "af": af})
        if ok:
            out.append((inst, bindings))
    return out


def _used_instances(intent: dict) -> set:
    """Instance tokens already fixed by peripheral/signal items (so a count
    never re-selects one)."""
    used = set()
    for it in intent.get("items") or []:
        lv = (it.get("level") or "").lower()
        if lv == "peripheral" and it.get("instance"):
            used.add(it["instance"].upper())
        elif lv == "signal" and it.get("signal"):
            used.add(it["signal"].split("_", 1)[0].upper())
    return used


def _pinned_pads(intent: dict) -> set:
    """Pads the user already nailed down elsewhere in the request (signal.pin or
    any pin_assignments[].pin) — so a count_af binding never steals one."""
    pads = set()
    for it in intent.get("items") or []:
        if it.get("pin"):
            pads.add(it["pin"].upper())
        for pa in it.get("pin_assignments") or []:
            if pa.get("pin"):
                pads.add(pa["pin"].upper())
    return pads


def _pin_options(pin: str, family_key: str, kb: Knowledge,
                 instance: str | None = None) -> list[Option]:
    opts = []
    for af, s in _candidates_on_pin(pin, family_key, kb, instance):
        inst = _instance_of(s)
        mode, is_default = _mode_of(s, inst, family_key, kb.profiles)
        if is_default:
            note = ""
        elif mode:
            note = f"{mode} 模式（非預設）"
        else:
            note = "不在標準 profile 模式內（需特別確認）"
        opts.append(Option(label=f"{s} @ {pin} (AF{af})", pin=pin, instance=inst,
                           signal=s, af=af, mode=mode, note=note))
    return opts


def _floating_pins(intent: dict, kb: Knowledge) -> list[str]:
    """Pads the user wants used but tied to no signal/peripheral. The LLM either
    puts them in the top-level 'loose_pins', or (when it can't place them) dumps a
    free-text note in 'unresolved' (e.g. "且要加入 PZ2") — we scrape pad tokens from
    both. Excludes pads already targeted by an item (signal.pin / pin_assignments /
    count.pins) and anything not a real GPIO pad on this board. Order-preserving."""
    pins = [p.upper() for p in (intent.get("loose_pins") or []) if p]
    for note in intent.get("unresolved") or []:
        pins += re.findall(r"\bP[A-Z]\d{1,2}\b", str(note).upper())
    targeted = _pinned_pads(intent) | {
        (p or "").upper()
        for it in (intent.get("items") or []) for p in (it.get("pins") or [])}
    out, seen = [], set()
    for p in pins:
        if p in kb.af and p not in targeted and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _loose_candidates(pin: str, intent: dict, kb: Knowledge) -> list[tuple[int, str]]:
    """[(af, signal)] that `pin` can carry AND that the request actually wants —
    i.e. the pad's AF-table signals intersected with the requested signals (explicit
    signal items), families (count items) and instances (peripheral items). This is
    the "expand the request to signals, keep the ones this pad supports" the user
    asked for."""
    families = kb.profiles["families"]
    want_sig, want_fam, want_inst = set(), set(), set()
    for it in intent.get("items") or []:
        lv = (it.get("level") or "").lower()
        if lv == "signal" and it.get("signal"):
            want_sig.add(it["signal"].upper())
        elif lv == "count":
            k = _family_key(it.get("family") or "", kb.profiles)
            if k:
                want_fam.add(k)
        elif lv == "peripheral" and it.get("instance"):
            want_inst.add(it["instance"].upper())
    out = []
    for af, sigs in sorted(kb.af_map.get(pin, {}).items()):
        for s in sigs:
            su = s.upper()
            inst = _instance_of(su)
            fam = _split_family(inst, families)
            fk = fam[2] if fam else None
            if su in want_sig or inst in want_inst or (fk and fk in want_fam):
                out.append((int(af), su))
    return out


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def validate_clarify(intent: dict, kb: Knowledge | None = None,
                     *, must_gpio: set | None = None,
                     reserved_instances=None) -> Ready | Clarify:
    kb = kb or load_knowledge()
    profiles = kb.profiles
    # A family with no instance number ("SPI" / "SPI 要有 PZ2") is a count(×1);
    # lower it so a pinned pad becomes a count_pin clarify instead of crashing on a
    # missing instance. Must match plan()/apply_answer() so item_index stays stable.
    intent = normalize_items(intent)
    questions: list[Question] = []
    # Pads that are off-limits for an auto-chosen AF binding: forced-GPIO pads
    # plus anything the user already pinned elsewhere in the request.
    taken = set(must_gpio or ()) | _pinned_pads(intent)
    # reserve_only 週邊（require.json）：不得作為反問選項提出，否則使用者一選就
    # 產生 resolver 必拒的違規解。
    reserved = frozenset(str(r).upper() for r in (reserved_instances or ()))

    def _allowed(o: Option) -> bool:
        return ((o.instance or "").upper() not in reserved
                and reserved_owner(o.signal or "", reserved) is None)

    # Multiple SIGNAL items pinned to the SAME pad: a pad carries only one signal,
    # so the request is over-constrained (e.g. "USART2_RX + UART4_TX + UART4_RX 放
    # PA8" — the LLM copies the single pin onto every signal). Ask which signal that
    # pad should carry, offering ONLY the signals the pad actually supports (1, 2, …);
    # the unpicked ones keep their signal but lose the pin (solver places them).
    by_pin: dict[str, list] = {}
    for item in intent.get("items") or []:
        if (item.get("level") or "").lower() == "signal" and item.get("pin"):
            by_pin.setdefault(item["pin"].upper(), []).append(item)
    for pin, group in by_pin.items():
        if len(group) < 2:
            continue
        sigs = list(dict.fromkeys((it.get("signal") or "").upper() for it in group))
        supported = [s for s in sigs if s in kb.af.get(pin, set())]
        opts = [Option(label=f"{s} @ {pin}", pin=pin, signal=s,
                       instance=_instance_of(s),
                       note="" if len(supported) > 1 else f"{pin} 只支援這一個")
                for s in supported]
        questions.append(Question(
            kind="shared_pin", span=f"{pin} ×{len(group)}",
            summary=(f"{pin} 被同時指定給 {', '.join(sigs)}，但一支腳只能接一個訊號。"
                     f"{pin} 實際能接的是 {supported or '—'}；請選一個由 {pin} 接，"
                     f"其餘訊號會保留、改由系統自動配腳。"),
            options=opts, item_index=0, pin=pin))

    # Loose pads: a pad the user wants used but tied to no signal/peripheral (left in
    # loose_pins, or dumped into unresolved). Scan it against the WHOLE request — which
    # requested signal/family/instance can this pad serve — and ask which one it takes.
    for pin in _floating_pins(intent, kb):
        cands = [(af, s) for af, s in _loose_candidates(pin, intent, kb)
                 if reserved_owner(s, reserved) is None]
        opts = [Option(label=f"{s} @ {pin} (AF{af})", pin=pin, signal=s,
                       instance=_instance_of(s), af=af) for af, s in cands]
        questions.append(Question(
            kind="loose_pin", span=f"+{pin}",
            summary=(f"你要求用到腳位 {pin}，但沒指定給哪個訊號。對照你的需求，"
                     f"{pin} 能支援：{[s for _, s in cands] or '—'}；"
                     f"請選一個由 {pin} 接（其餘需求照常自動配腳）。"),
            options=opts, item_index=-1, pin=pin))

    for idx, item in enumerate(intent.get("items") or []):
        level = (item.get("level") or "").lower()

        if level == "count":
            family = item.get("family") or ""
            key = _family_key(family, profiles)
            af = item.get("af")

            # An AF constraint governs the whole item: it dictates instance AND
            # pins, so the (rarely co-occurring) pin-pool is subordinate — handle
            # AF here and skip the count_pin pin-pool questions for this item, so
            # answering one can never silently drop the other (review finding #1).
            if af is not None and key:
                mode = item.get("mode") if item.get("mode") in profiles["families"][key]["modes"] else None
                cands = _af_candidates(key, int(af), kb, mode=mode,
                                       exclude=_used_instances(intent) | set(reserved),
                                       taken=taken)
                if not cands:
                    # Impossible request — fail loudly instead of letting plan()
                    # silently auto-select an instance off the wrong AF (#2).
                    need = profiles["families"][key]["modes"][mode or profiles["families"][key]["default"]]
                    raise ResolveError(
                        f"AF{af} 上沒有可完整實現的 {family}"
                        f"（找不到在 AF{af} 同時具備 {need} 且腳位未被佔用的 instance）。")
                opts = []
                for inst, binds in cands:
                    where = ", ".join(f"{b['signal'].split('_', 1)[1]}@{b['pin']}" for b in binds)
                    opts.append(Option(
                        label=f"{inst}（AF{af}: {where}）", instance=inst, af=int(af),
                        mode=mode or profiles["families"][key]["default"], bindings=binds,
                        note=f"會帶入整組 {inst}"))
                questions.append(Question(
                    kind="count_af", span=f"{family} ×{item.get('count')} @ AF{af}",
                    summary=(f"你要求「{family} ×{item.get('count')}」要用 AF{af}——"
                             f"AF{af} 上能完整實現的 {family} 有 {[i for i, _ in cands]}，"
                             f"需確認用哪一個。"),
                    options=opts, item_index=idx, pin=None))
                continue

            for pin in (item.get("pins") or []):
                opts = [o for o in (_pin_options(pin, key, kb) if key else [])
                        if _allowed(o)]
                if not opts:
                    continue          # 此腳接不了任何該 family 訊號 -> 略過，不問死路
                                      # （plan 本就不使用 count 的 pins 池，等同忽略此腳；
                                      #  避免 LLM 把飄浮腳位誤塞到配不上的 family 時卡住）
                for o in opts:                    # family-level pin -> bring in whole instance
                    if o.mode:
                        o.note = ((o.note + "；") if o.note else "") + \
                                 f"會帶入整組 {o.instance}（{o.mode}）"
                summary = (f"你給的是數量需求「{family} ×{item.get('count')}」，"
                           f"但又把腳位 {pin} 釘住——count 不綁腳位，"
                           f"需要確認 {pin} 要當什麼用。")
                questions.append(Question(
                    kind="count_pin", span=f"{family} ×{item.get('count')} @ {pin}",
                    summary=summary, options=opts, item_index=idx, pin=pin))

        elif level == "peripheral":
            instance = (item.get("instance") or "").upper()
            key = _family_key(instance, profiles) or _family_key(item.get("family") or "", profiles)
            for pa in item.get("pin_assignments") or []:
                if pa.get("signal"):
                    continue                          # explicit signal — resolver validates it
                pin = (pa.get("pin") or "").upper()
                if not pin:
                    continue
                af = pa.get("af")
                if af is not None:
                    atoms = kb.af_map.get(pin, {}).get(int(af)) or []
                    if [s for s in atoms if _instance_of(s) == instance]:
                        continue                      # AF resolves cleanly to this instance
                    opts = _pin_options(pin, key, kb, instance)   # correct AFs for this instance
                    for s in atoms:                               # ...and what the given AF really is
                        opts.append(Option(
                            label=f"改用 {_instance_of(s)}：AF{af} = {s}", pin=pin,
                            instance=_instance_of(s), signal=s, af=int(af),
                            note="你給的 AF 其實對應到別的週邊"))
                    opts = [o for o in opts if _allowed(o)]
                    questions.append(Question(
                        kind="af_mismatch", span=f"{instance} {pin} AF{af}",
                        summary=f"{pin} 的 AF{af} 不是 {instance} 的訊號"
                                f"（AF{af} = {atoms or '—'}）。",
                        options=opts, item_index=idx, pin=pin))
                else:
                    opts = [o for o in _pin_options(pin, key, kb, instance)
                            if _allowed(o)]
                    if len(opts) > 1:
                        questions.append(Question(
                            kind="ambiguous_pin", span=f"{instance} {pin}",
                            summary=f"{pin} 上 {instance} 有多個訊號可放，需指定哪一個。",
                            options=opts, item_index=idx, pin=pin))

    return Clarify(questions) if questions else Ready(intent)


# --------------------------------------------------------------------------- #
# Fold-back: a confirmed Option -> edited intent (deterministic, no LLM)
# --------------------------------------------------------------------------- #
def apply_answer(intent: dict, question: Question, option: Option) -> dict:
    """Return a new intent with `option` folded in.

    count_pin   -> the pinned signal becomes a concrete signal-level binding and
                   the count is decremented by 1 (a single pinned signal can't be
                   a whole peripheral, so we honour it literally; the remaining
                   count still auto-selects full instances).
    count_af    -> the chosen instance becomes a concrete peripheral whose default
                   signals are pinned to their AF pads; the count drops by 1.
    af_mismatch / ambiguous_pin
                -> rewrite the pin_assignment to the chosen signal/AF; if the
                   choice is a different peripheral, move it to a signal item.
    shared_pin  -> the chosen signal keeps the pad; the other signal items that
                   were pinned to the same pad lose their pin (stay required, the
                   solver re-homes them).
    loose_pin   -> pin the chosen requested signal to the floating pad, attaching
                   to its item (signal/peripheral/count); drop the pad from
                   loose_pins.
    """
    intent = normalize_items(copy.deepcopy(intent))   # same lowering as validate_clarify
    items = intent.setdefault("items", [])

    if question.kind == "count_af":
        ci = items[question.item_index]
        ci["count"] = int(ci.get("count") or 0) - 1
        items.append({
            "level": "peripheral", "family": ci.get("family"),
            "instance": option.instance, "mode": option.mode,
            "pin_assignments": [dict(b) for b in (option.bindings or [])],
        })
        if ci["count"] <= 0:
            items.remove(ci)

    elif question.kind == "count_pin":
        ci = items[question.item_index]
        ci["count"] = int(ci.get("count") or 0) - 1
        ci["pins"] = [p for p in (ci.get("pins") or []) if p != question.pin]
        if option.mode:                           # known mode -> bring in the WHOLE instance
            items.append({
                "level": "peripheral", "family": ci.get("family"),
                "instance": option.instance, "mode": option.mode,
                "pin_assignments": [
                    {"signal": option.signal, "pin": option.pin, "af": option.af}],
            })
        else:                                     # outside the profile (e.g. ETH MII) -> lone signal
            items.append({
                "level": "signal", "family": ci.get("family"),
                "signal": option.signal, "pin": option.pin, "af": option.af,
                "pin_mode": "required",
            })
        if ci["count"] <= 0:
            items.remove(ci)

    elif question.kind in ("af_mismatch", "ambiguous_pin"):
        item = items[question.item_index]
        if option.instance == (item.get("instance") or "").upper():
            for pa in item.get("pin_assignments") or []:
                if (pa.get("pin") or "").upper() == question.pin:
                    pa["signal"], pa["af"], pa["pin"] = option.signal, option.af, option.pin
        else:                                     # chose a different peripheral
            item["pin_assignments"] = [
                pa for pa in (item.get("pin_assignments") or [])
                if (pa.get("pin") or "").upper() != question.pin]
            items.append({
                "level": "signal", "family": (option.instance or "")[:3],
                "signal": option.signal, "pin": option.pin, "af": option.af,
                "pin_mode": "required",
            })

    elif question.kind == "shared_pin":
        # The chosen signal keeps the pad; every OTHER signal item on this pad loses
        # its pin (stays required — the solver finds it another pad). Folds by pin,
        # so it is robust to item reordering / repeated rounds.
        chosen = (option.signal or "").upper()
        for it in items:
            if ((it.get("level") or "").lower() != "signal"
                    or (it.get("pin") or "").upper() != question.pin):
                continue
            if (it.get("signal") or "").upper() == chosen:
                it["pin"], it["af"] = option.pin, option.af
            else:
                it["pin"], it["af"] = None, None

    elif question.kind == "loose_pin":
        # A floating pad resolved to one requested signal. Pin that signal to the pad,
        # attaching to the item it belongs to: an explicit signal item -> set its pin;
        # a peripheral instance -> add a pin_assignment; a count of the family -> lower
        # one instance into a peripheral with the binding (count_pin-style). The pad is
        # now targeted by an item, so _floating_pins won't re-offer it next round.
        s = (option.signal or "").upper()
        inst = (option.instance or "").upper()
        fam = (re.match(r"^([A-Za-z]+)", inst) or [None, ""])[1].upper()
        intent["loose_pins"] = [p for p in (intent.get("loose_pins") or [])
                                if (p or "").upper() != (question.pin or "").upper()]
        done = False
        for it in items:
            lv = (it.get("level") or "").lower()
            if lv == "signal" and (it.get("signal") or "").upper() == s:
                it["pin"], it["af"] = option.pin, option.af
                done = True
            elif lv == "peripheral" and (it.get("instance") or "").upper() == inst:
                it.setdefault("pin_assignments", []).append(
                    {"signal": s, "pin": option.pin, "af": option.af})
                done = True
            elif lv == "count" and (re.match(r"^([A-Za-z]+)", it.get("family") or "")
                                    or [None, ""])[1].upper() == fam:
                it["count"] = int(it.get("count") or 0) - 1
                items.append({
                    "level": "peripheral", "family": it.get("family"),
                    "instance": inst, "mode": it.get("mode"),
                    "pin_assignments": [{"signal": s, "pin": option.pin, "af": option.af}]})
                if it["count"] <= 0:
                    items.remove(it)
                done = True
            if done:
                break
        if not done:                              # no matching item -> stand-alone signal
            items.append({"level": "signal", "family": fam, "signal": s,
                          "pin": option.pin, "af": option.af, "pin_mode": "required"})

    return intent


# --------------------------------------------------------------------------- #
# Phrasing — the ONLY place the LLM is (optionally) involved
# --------------------------------------------------------------------------- #
def phrase_question(question: Question, provider=None) -> str:
    """Render one question as a friendly confirm sentence.

    With no provider, returns a deterministic template (so this module is
    testable without an API key).  With a provider, the LLM rewrites it in
    natural language — but it only ever sees the precomputed option list.
    """
    lines = [f"[{i+1}] {o.label}" + (f" — {o.note}" if o.note else "")
             for i, o in enumerate(question.options)]
    if provider is None:
        body = "；".join(f"[{i+1}] {o.label}" for i, o in enumerate(question.options))
        return question.summary + (f" 可選：{body}" if body else "")

    from llm_provider import Message, Role
    system = (
        "你是 STM32 硬體腳位助理。下面是程式偵測到的一個需要向使用者確認的議題，"
        "以及已經算好的合法選項。請只用一句繁體中文，友善地請使用者從這些選項中確認，"
        "不要詢問選項以外的任何事情，不要自行發明選項。")
    user = (f"議題：{question.summary}\n選項：\n" + "\n".join(lines))
    resp = provider.complete(
        [Message(role=Role.SYSTEM, content=system),
         Message(role=Role.USER, content=user)],
        json_mode=False, temperature=0)
    return resp.content.strip()


# --------------------------------------------------------------------------- #
# Self-test / demo (no API key needed — provider defaults to None)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from solver.counts import plan
    from solver.resolver import to_instance
    from solver.solve import PinAssign

    from util.dataio import board_paths, load_gpio_pins

    kb = load_knowledge()
    MUST_GPIO = load_gpio_pins(board_paths()["require"])   # gpio_must_pins from board data

    examples = [
        ("我要 ETH 兩個，要在 PA0 上（MII，profile 外 -> 單一 signal）", {
            "items": [{"level": "count", "family": "ETH", "mode": None,
                       "count": 2, "pins": ["PA0"], "pin_mode": "required"}]}),
        ("ETH 兩個 + SPI 在 PZ2（已知 mode -> 帶整組 SPI8）", {
            "items": [
                {"level": "count", "family": "ETH", "mode": None, "count": 2,
                 "pins": [], "pin_mode": None},
                {"level": "count", "family": "SPI", "mode": None, "count": 1,
                 "pins": ["PZ2"], "pin_mode": "required"}]}),
        ("FDCAN 兩個 + ETH 兩個 + 一個 I2C 用 AF8（count_af）", {
            "items": [
                {"level": "count", "family": "FDCAN", "mode": None, "count": 2, "pins": []},
                {"level": "count", "family": "ETH", "mode": None, "count": 2, "pins": []},
                {"level": "count", "family": "I2C", "mode": None, "count": 1,
                 "pins": [], "af": 8}]}),
        ("SPI8 在 PZ1 用 AF8（AF 不屬於 SPI8）", {
            "items": [{"level": "peripheral", "family": "SPI", "instance": "SPI8",
                       "mode": None, "pin_assignments": [
                           {"signal": None, "pin": "PZ1", "af": 8}]}]}),
        # --- review-found regressions ---
        ("AF8 + 已釘 PZ1（候選 pin 避開已佔用 -> 不撞）", {
            "items": [
                {"level": "signal", "family": "SPI", "signal": "SPI8_MISO",
                 "pin": "PZ1", "af": 3, "pin_mode": "required"},
                {"level": "count", "family": "I2C", "mode": None, "count": 1,
                 "pins": [], "af": 8}]}),
        ("一個 I2C 用 AF8 + smbus（mode 應被尊重）", {
            "items": [{"level": "count", "family": "I2C", "mode": "smbus",
                       "count": 1, "pins": [], "af": 8}]}),
        ("一個 I2C 用 AF3（湊不出完整 I2C -> 應丟 ResolveError）", {
            "items": [{"level": "count", "family": "I2C", "mode": None,
                       "count": 1, "pins": [], "af": 3}]}),
    ]

    for title, intent in examples:
        print("=" * 72)
        print("輸入：", title)
        guard, n = 0, 0
        # ResolveError 可能出現在任何一步（首驗、折回後重驗、plan）——例如要求的
        # pin 落在該板 require.json 的 gpio_must_pins 上，就是合法的拒絕。
        try:
            res = validate_clarify(intent, kb, must_gpio=MUST_GPIO)
            while isinstance(res, Clarify) and guard < 10:
                guard += 1
                picked = None
                for q in res.questions:
                    print("\n  ⚠", phrase_question(q))       # provider=None -> template
                    for i, o in enumerate(q.options):
                        print(f"      [{i+1}] {o.label}" + (f"  — {o.note}" if o.note else ""))
                    if q.options and picked is None:
                        picked = q
                if picked is None:
                    print("\n  （此議題無可用候選，無法自動繼續）")
                    break
                print(f"\n  → demo 自動選 [1] {picked.options[0].label}")
                intent = apply_answer(intent, picked, picked.options[0])
                res = validate_clarify(intent, kb, must_gpio=MUST_GPIO)

            if isinstance(res, Ready):
                cp = plan(res.intent, kb, must_gpio=MUST_GPIO)
                r = PinAssign(to_instance(cp.spec, kb)).solve()
                if cp.chosen:
                    print("\n  count 自動選擇：",
                          "；".join(f"{c['family']}×{c['count']}→{c['instances']}" for c in cp.chosen))
                print("  最終 required:", cp.spec.required)
                print("  must_bind   :", cp.spec.must_bind)
                print("  SAT:", r.sat)
        except ResolveError as exc:
            print("  ResolveError:", exc)
            continue
