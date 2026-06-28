"""M8 — repair prompt assembly (plan.md v3 §8).

The Repairer speaks the exact same contract as the Generator (same
emit_dts_edits tool, same hard rules) plus a minimal-diff discipline: fix only
what the validator errors identify, keep every other edit identical.
"""
import json

from ..m6_generator.prompt import SYSTEM_PROMPT as GENERATOR_SYSTEM_PROMPT

REPAIR_SYSTEM_PROMPT = GENERATOR_SYSTEM_PROMPT + """

REPAIR MODE — additional rules:
R1. You are fixing a previous emit_dts_edits output that failed deterministic
validation. The validator errors below are authoritative: address EVERY listed
error and change NOTHING else — unrelated edits must be byte-identical.
R2. If an error says a pinmux differs from the plan, restore the EXACT plan
(pin, af) values. The plan is law; never "fix" an error by changing a pin.
R3. If an error names a label collision, rename YOUR generated label (keep the
<node>_pins_generated convention, e.g. append a suffix); never rename or touch
baseline labels.
R4. If an error reports a dtc/syntax failure on a property, correct the value
FORMAT (cells in <...>, strings quoted); do not drop the property unless it was
invented without a source.
R5. If the previous output is missing or was completely rejected, produce the
full, correct edit set from the target context alone."""


def build_repair_prompt(target_dict, current_edits, errors):
    """User message: the original target context + the failing edits + the
    validator errors, all verbatim JSON."""
    err_lines = []
    for e in errors:
        d = e if isinstance(e, dict) else e.to_dict()
        loc = f" (line {d['line']})" if d.get("line") else ""
        err_lines.append(f"- [{d.get('error_type', 'ERROR')}]{loc} {d.get('message', '')}")
    parts = [
        "Your previous edits for this target failed validation. Fix exactly the "
        "errors below and call emit_dts_edits again with the complete, corrected "
        "edit set.",
        "\nVALIDATOR ERRORS:\n" + "\n".join(err_lines),
        "\nTARGET (unchanged, pins are law):\n```json\n"
        + json.dumps(target_dict, indent=2, ensure_ascii=False) + "\n```",
    ]
    if current_edits:
        parts.append("\nYOUR PREVIOUS EDITS (fix minimally):\n```json\n"
                     + json.dumps(current_edits, indent=2, ensure_ascii=False) + "\n```")
    else:
        parts.append("\nNo previous edits survive — produce the full edit set (rule R5).")
    return "\n".join(parts)
