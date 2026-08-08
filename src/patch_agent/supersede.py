"""Comment out superseded pinctrl lines in the official board dts.

政策（2026-08-08 定案）：plan 換腳的周邊（action = generate / reuse），其官方
board-dts 段落中 depth-1 的 `pinctrl-names` / `pinctrl-N` 屬性行以 `//` 註解掉
並插入一行說明，消除「官方段與 managed region 兩段 &node 都帶 pinctrl」的閱讀
混淆；合併結果的 pinctrl 因此只來自 managed region。**只註解 pinctrl 行、不註解
整段**：段內其餘屬性（clock-frequency、時序、/delete-property/ …）與子節點必須
保留——整段註解會使段內定義的 label（如 EV1 &i2c2 的 imx335_ep 被 &csi 引用）
產生懸空引用而編譯失敗。

此 transform 是純函式：m4 / m6 產生端與 m7 regression 驗證端 import 同一實作
重算（expected prefix = apply(baseline, targets)），維持「managed region 以外
內容可確定性重現」的防偽保證。
"""
import re

BANNER = ("/* stm-agent: original pinctrl superseded by the managed region "
          "below (pins changed by plan) */")
_PINCTRL_LINE = re.compile(r"pinctrl(?:-names|-\d+)\s*=")


def _scan_line(line, depth, state):
    """Advance the char-level scanner across one line (no trailing newline).
    state: None | 'block' | 'str'  ('line' comments end with the line itself).
    Returns (depth, state, saw_semicolon_in_code)."""
    i, n, semi = 0, len(line), False
    while i < n:
        c = line[i]
        if state == 'block':
            if c == '*' and line[i:i + 2] == '*/':
                state = None
                i += 1
        elif state == 'str':
            if c == '\\':
                i += 1
            elif c == '"':
                state = None
        else:
            if c == '/' and line[i:i + 2] == '//':
                break                          # rest of line is a comment
            if c == '/' and line[i:i + 2] == '/*':
                state = 'block'
                i += 1
            elif c == '"':
                state = 'str'
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
            elif c == ';':
                semi = True
        i += 1
    return depth, state, semi


def _block_line_span(lines, start_line):
    """Given the index of a `&label {` line, return the index one past the
    closing `};` line (scanning with the same comment/string-aware counter)."""
    depth, state = 0, None
    for k in range(start_line, len(lines)):
        depth, state, _ = _scan_line(lines[k], depth, state)
        if k > start_line or '{' in lines[k]:
            if depth == 0 and state is None:
                return k + 1
    return len(lines)


def _comment(line):
    return re.sub(r"^(\s*)", lambda m: m.group(1) + "// ", line, count=1)


def _apply_one(text, label):
    """Comment pinctrl lines in every top-level `&label { ... }` block.
    Returns (new_text, changed?)."""
    lines = text.split("\n")
    opener = re.compile(rf"^&{re.escape(label)}\s*\{{")
    changed = False
    out, k = [], 0
    while k < len(lines):
        if not opener.match(lines[k]):
            out.append(lines[k])
            k += 1
            continue
        end = _block_line_span(lines, k)
        block = lines[k:end]
        # per-line depth/state at line START, relative to the block
        metas, depth, state = [], 0, None
        for ln in block:
            metas.append((depth, state))
            depth, state, _ = _scan_line(ln, depth, state)
        new_block, j, did = [], 0, False
        while j < len(block):
            d, st = metas[j]
            code = block[j].lstrip()
            if (d == 1 and st is None and _PINCTRL_LINE.match(code)
                    and not code.startswith("//")):
                # comment through the terminating ';' (usually the same line)
                while j < len(block):
                    _, _, semi = _scan_line(block[j], metas[j][0], metas[j][1])
                    new_block.append(_comment(block[j]))
                    j += 1
                    if semi:
                        break
                did = True
                continue
            new_block.append(block[j])
            j += 1
        if did:
            new_block.insert(1, "\t" + BANNER)
            changed = True
        out.extend(new_block)
        k = end
    return "\n".join(out), changed


def apply(baseline, labels):
    """Apply the transform for every label. Returns (text, [labels changed]).
    Idempotent: already-commented lines no longer match. Labels whose block is
    absent or carries no pinctrl lines are silent no-ops."""
    text, changed = baseline, []
    for label in sorted({l.lstrip("&") for l in labels}):
        text, did = _apply_one(text, label)
        if did:
            changed.append(label)
    return text, changed
