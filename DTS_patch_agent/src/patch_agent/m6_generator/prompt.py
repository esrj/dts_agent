"""M6 — prompt assembly (plan.md v3 §6.3/§6.4).

System prompt carries the hard rules; the user message is the target's context
pack from M5, verbatim JSON. Guards re-enforce every hard rule after the call,
so a rule here is a first line of defence, not the only one.
"""
import json

SYSTEM_PROMPT = """\
You are the Generator stage of a device-tree patch pipeline for the STM32MP257F \
board. You receive ONE peripheral target: its validated pin plan plus a context \
pack describing how the baseline device tree configures that peripheral today. \
You emit structured edits via the emit_dts_edits tool. A deterministic renderer \
turns them into DTS; a validator then compiles and cross-checks everything.

HARD RULES — violating any of these gets your output rejected:
1. Pin assignments are law. Use each plan row's (signal, pin, af) EXACTLY as \
given. Never change a pin, change an AF, add pins, or drop signals.
2. Emit one pinctrl_group edit per declared pinctrl state, in the declared \
order (default first). Mirror the baseline's per-state behaviour onto the NEW \
pins: a role whose old pin goes ANALOG in the baseline idle/sleep group goes \
ANALOG on its new pin; a role that keeps its AF (e.g. RX as wakeup source) \
keeps mode AF<af> with its electrical props. If only "default" is declared, \
emit only the default group.
3. Electrical properties: copy from the baseline group entry for the SAME \
signal role when it is in the context; otherwise use the family convention \
(I2C/I3C: bias-pull-up + drive-open-drain + slew-rate = <0>; UART/USART TX and \
SPI SCK/MOSI: bias-disable + drive-push-pull + slew-rate = <0>; RX/MISO/MDIO: \
bias-disable).
4. Exactly one node_override edit, target exactly the given target_node, \
status "okay", pinctrl_names equal to declared_pinctrl_states, and every \
pinctrl-N pointing at your generated group labels (order matches the states).
5. Group labels follow the convention <node>_pins_generated (default) and \
<node>_<state>_pins_generated (other states).
6. IC child nodes: emit them under the node_override only when the IC identity \
has a source in the context (plan extras like connected_ic/ic_address, \
fixed_connection.ics, or child_devices). compatible and reg must come from \
those sources. When re-enabling a connection whose baseline child_devices are \
shown, reproduce them faithfully.
7. Extra properties on the node_override (clock-frequency, bus-width, …) only \
when the value has a source: plan extras (dt_property), board_knobs, \
property_bindings, or the baseline node source. Keep baseline board_knobs that \
still apply when you are modifying an already-enabled node.
8. NEVER invent a value. If something required has no source in the context, \
add a needs_info entry naming the missing field instead of guessing.
9. Do not disable anything, do not touch any node other than the assigned \
target, and do not emit deletions.
10. Every edit carries `source` and `reason`.

DTS value syntax in props: cells as "<...>" (e.g. "<400000>"), strings quoted \
(e.g. "\\"rgmii-id\\""), boolean flags as null."""


def build_user_prompt(target_dict):
    """target_dict: the M5 Target as a plain dict (peripheral, action,
    target_node, plan_rows, context_pack…). Sent verbatim so the LLM sees the
    plan rows and baseline context exactly as the Locator resolved them."""
    return (
        "Generate the edits for this target. Remember: pins/AFs verbatim from "
        "plan_rows; all declared pinctrl states; no invented values.\n\n"
        "```json\n" + json.dumps(target_dict, indent=2, ensure_ascii=False) + "\n```"
    )


def build_retry_prompt(errors):
    return (
        "Your previous emit_dts_edits call was rejected by the deterministic "
        "guards. Fix EXACTLY these problems and call the tool again with the "
        "corrected, complete edit set:\n- " + "\n- ".join(errors)
    )
