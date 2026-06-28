"""M6 — structured-edits schema (plan.md v3 §6.1).

The LLM never writes free-form DTS. It is forced (Anthropic tool_choice) to
call `emit_dts_edits` whose input follows this JSON schema; the deterministic
renderer turns validated edits into the managed region. Guards in generate.py
enforce everything the schema can't express (plan-consistency, label collisions,
state completeness).
"""

PROMPT_VERSION = "m6-v1"
TOOL_NAME = "emit_dts_edits"

_SUBNODE = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "pin subnode name, e.g. pins1"},
        "pin": {"type": "string", "description": "SoC pin, e.g. PA4 — verbatim from the plan"},
        "mode": {"type": "string",
                 "description": "AF<n> exactly as the plan gives for this signal, or ANALOG for low-power states"},
        "signal": {"type": ["string", "null"],
                   "description": "signal this pin carries (comment only); null for ANALOG"},
        "props": {"type": "object",
                  "description": "electrical properties; value = raw DTS value string, or null for boolean flags, e.g. {\"bias-disable\": null, \"slew-rate\": \"<0>\"}"},
    },
    "required": ["name", "pin", "mode"],
}

_CHILD_NODE = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "node name incl. unit address, e.g. rtc@30"},
        "label": {"type": ["string", "null"]},
        "props": {"type": "object",
                  "description": "properties in emit order; value = raw DTS value string (compatible must be a quoted string, reg a <...> cell), or null for boolean flags"},
        "children": {"type": "array", "items": {"type": "object"},
                     "description": "nested child nodes, same shape as this object"},
    },
    "required": ["name", "props"],
}

_EDIT = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["pinctrl_group", "node_override"]},
        # --- pinctrl_group fields ---
        "label": {"type": "string",
                  "description": "pinctrl_group: DTS label, convention <node>_pins_generated / <node>_<state>_pins_generated"},
        "name": {"type": "string", "description": "pinctrl_group: node name, e.g. usart2-generated"},
        "state": {"type": "string",
                  "description": "pinctrl_group: which pinctrl state this group implements (default/idle/sleep)"},
        "subnodes": {"type": "array", "items": _SUBNODE},
        # --- node_override fields ---
        "target": {"type": "string", "description": "node_override: &label, must equal the assigned target_node"},
        "status": {"type": "string", "enum": ["okay"]},
        "pinctrl_names": {"type": "array", "items": {"type": "string"},
                          "description": "node_override: must equal the declared_pinctrl_states"},
        "pinctrl": {"type": "object",
                    "description": "node_override: {\"pinctrl-0\": [\"group_label\", ...], ...} referencing generated or existing group labels"},
        "props": {"type": "object",
                  "description": "node_override: extra binding properties; only values with a source in the provided context"},
        "children": {"type": "array", "items": _CHILD_NODE,
                     "description": "node_override: IC child nodes (slave devices on this bus)"},
        # --- provenance (required on every edit) ---
        "source": {"type": "string",
                   "description": "where the content came from: plan:L1 / plan:extras / fixed_connections / baseline / property_bindings / family-convention"},
        "reason": {"type": "string", "description": "one-line why this edit exists"},
    },
    "required": ["type", "source", "reason"],
}

EDITS_SCHEMA = {
    "type": "object",
    "properties": {
        "edits": {"type": "array", "items": _EDIT},
        "needs_info": {
            "type": "array",
            "description": "required data with NO source in the provided context — never guess",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["field", "why"],
            },
        },
    },
    "required": ["edits"],
}

EMIT_TOOL = {
    "name": TOOL_NAME,
    "description": "Emit the structured device-tree edits for the assigned peripheral. "
                   "This is the ONLY way to answer; free text is discarded.",
    "input_schema": EDITS_SCHEMA,
}
