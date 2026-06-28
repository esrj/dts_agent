"""Core recursive DTS parser.

Offset-preserving comment stripping (so `;`/`{`/`=` inside a `/* */` or `//`
comment can't inject phantom properties) while keeping file:line provenance
accurate. Produces a tree of `Node`. This is the shared low-level parser used by
the DTS index (and the data-extraction tools).
"""
import re

GPIO_REF = re.compile(r'&gpio([a-z]+)\s+(\d+)')


def gpio_pin(bank, line):
    """&gpiod 9 -> PD9  (bank letter -> port letter, line -> pin number)."""
    return f"P{bank.upper()}{int(line)}"


def compat_strings(raw):
    """All strings of a (possibly multi-string) `compatible` value."""
    return re.findall(r'"([^"]+)"', raw or '')


def compat_primary(raw):
    s = compat_strings(raw)
    return s[0] if s else None


def strip_comments_preserve(s):
    """Blank /* */ and // comments and cpp directive lines, keeping every newline
    so character offsets still map to original line numbers."""
    def blank(m):
        return re.sub(r'[^\n]', ' ', m.group(0))
    s = re.sub(r'/\*.*?\*/', blank, s, flags=re.S)
    s = re.sub(r'//[^\n]*', blank, s)
    s = re.sub(r'(?m)^\s*#\s*(include|define|if|ifdef|ifndef|else|elif|endif|'
               r'undef|pragma|error|warning)\b.*$', blank, s)
    return s


class Node:
    """A DTS node. `props` maps key -> raw value string (None for a boolean flag).
    `deletions` lists `/delete-property/` targets. `children` are sub-nodes."""
    __slots__ = ('header', 'label', 'ref', 'name', 'status', 'props',
                 'deletions', 'children', 'line')

    def __init__(self, header, line):
        self.header = header
        self.line = line
        self.label = None
        self.ref = None
        self.name = header
        self.status = None
        self.props = {}
        self.deletions = []
        self.children = []
        m = re.match(r'(?:(\w+)\s*:\s*)?(.*)', header.strip())
        if m:
            self.label = m.group(1)
            rest = m.group(2).strip()
            if rest.startswith('&'):
                self.ref = rest[1:].strip()
                self.name = self.ref
            else:
                self.name = rest

    @property
    def unit_address(self):
        return self.name.split('@', 1)[1] if '@' in (self.name or '') else None

    @property
    def ident(self):
        if self.label:
            return self.label
        if self.ref:
            return self.ref
        if self.name and self.name not in ('/', ''):
            return self.name
        c = compat_primary(self.props.get('compatible'))
        return c.split(',')[-1] if c else None

    def prop(self, key, default=None):
        return self.props.get(key, default)

    def __repr__(self):
        return f"<Node {self.ident!r} line={self.line} props={len(self.props)} children={len(self.children)}>"


def parse_dts(text):
    """Parse a full .dts/.dtsi text into a list of top-level Node (root '/' and
    every &override). Comments are stripped offset-preserving first."""
    text = strip_comments_preserve(text)
    n = len(text)

    def line_of(off):
        return text.count('\n', 0, off) + 1

    def handle_stmt(stmt, props, dels):
        if stmt.startswith('/delete-property/'):
            dels.append(stmt[len('/delete-property/'):].strip())
            return
        if stmt.startswith('/'):           # /dts-v1/, /delete-node/, /memreserve/, ...
            return
        if '=' in stmt:
            k, v = stmt.split('=', 1)
            props[k.strip()] = v.strip()
        else:
            props[stmt.strip()] = None

    def parse_body(i):
        nodes, props, dels = [], {}, []
        buf_start = i
        buf = ''
        while i < n:
            c = text[i]
            if c == '}':
                return nodes, props, dels, i + 1
            if c == '{':
                header = buf.strip()
                hdr_off = buf_start + (len(buf) - len(buf.lstrip()))
                node = parse_node(header, line_of(hdr_off), i + 1)
                nodes.append(node[0])
                i = node[1]
                buf = ''
                buf_start = i
                continue
            if c == ';':
                stmt = buf.strip()
                if stmt:
                    handle_stmt(stmt, props, dels)
                i += 1
                buf = ''
                buf_start = i
                continue
            buf += c
            i += 1
        return nodes, props, dels, i

    def parse_node(header, line, i):
        children, props, dels, ci = parse_body(i)
        node = Node(header, line)
        node.props = props
        node.deletions = dels
        node.children = children
        st = props.get('status')
        if st is not None:
            node.status = st.strip().strip('"')
        return node, ci

    nodes, _, _, _ = parse_body(0)
    return nodes


def walk(nodes, parent_active=True):
    """Yield (node, effective_active) depth-first. A node is active if neither it
    nor any ancestor has status = "disabled"."""
    for nd in nodes:
        active = parent_active and (nd.status != 'disabled')
        yield nd, active
        yield from walk(nd.children, active)


def iter_all(nodes):
    """Yield every node depth-first (status-agnostic)."""
    for nd in nodes:
        yield nd
        yield from iter_all(nd.children)
