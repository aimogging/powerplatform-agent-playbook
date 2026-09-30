"""
capa_common.py -- shared plumbing for the canvas-design toolkit
(render-screen.py and lint-canvas.py both import this).

Contains:
  - a line-number-preserving YAML loader for .pa.yaml files
  - control-tree walking helpers
  - a deliberately small Power Fx formula tokenizer / parser / evaluator (numbers,
    strings, +-*/, &, comparisons it parses-but-ignores, Parent./Self./ThisItem./
    varTheme. paths, Font.'x', Enum.Member, and a curated function allowlist:
    If/Switch/Coalesce/Max/Min/RGBA/ColorValue/Len/Trim -- everything else, and any
    parse failure, resolves to UNRESOLVED rather than raising)
  - varTheme token->CSS-color extraction out of an App.pa.yaml's OnStart formula
  - defaults-catalog.json loading + the explicit -> catalog -> fallback ladder

Never hardcode a control's default styling here -- that belongs in defaults-catalog.json
(seeded by build-catalog.py) or, as an absolute last resort, in FALLBACK_DEFAULTS below,
which exists only to guarantee every property renders *something* and is always
VISIBLY flagged when used.
"""
import json
import re
import zipfile
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# YAML loading with line numbers attached to every mapping
# ---------------------------------------------------------------------------


class LineDict(dict):
    __line__ = 0


class LineLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    d = LineDict()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        val = loader.construct_object(value_node, deep=True)
        d[key] = val
    d.__line__ = node.start_mark.line + 1
    return d


LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def load_pa_yaml(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    return yaml.load(text, Loader=LineLoader)


# ---------------------------------------------------------------------------
# Control tree helpers
# ---------------------------------------------------------------------------


def get_screen(doc):
    """Returns (screen_name, screen_node) for a screen .pa.yaml document. Also
    handles component/App-level .pa.yaml files that use 'ComponentDefinitions' or
    ' App' shape by falling back to whatever top-level dict looks screen-shaped."""
    if isinstance(doc, dict) and "Screens" in doc:
        scr = doc["Screens"]
        name = next(iter(scr))
        return name, scr[name]
    raise ValueError("Not a screen .pa.yaml (no top-level 'Screens' key)")


def iter_children(children):
    """children: the 'Children' list value (list of single-key {name: node} dicts).
    Yields (name, node) for direct children only (non-recursive)."""
    for entry in children or []:
        for name, node in entry.items():
            yield name, node


def iter_all_controls(children):
    """Recursively yields (name, node) for every control in the tree, depth-first."""
    for name, node in iter_children(children):
        yield name, node
        if isinstance(node, dict):
            yield from iter_all_controls(node.get("Children"))


def control_key(node):
    """The 'Control' tag value is already 'Name@Version' or 'Classic/Name@Version'."""
    return node.get("Control", "")


def control_base_variant(node):
    """('Vertical'|'Horizontal'|None) for Gallery Variant, used to pick layout axis."""
    return node.get("Variant")


# ---------------------------------------------------------------------------
# Formula tokenizer / parser / evaluator
# ---------------------------------------------------------------------------

UNRESOLVED = object()


class _Token:
    __slots__ = ("type", "text")

    def __init__(self, type_, text):
        self.type = type_
        self.text = text

    def __repr__(self):
        return f"{self.type}:{self.text!r}"


_TOKEN_RE = re.compile(
    r"""
      (?P<NUMBER>\d+(?:\.\d+)?)
    | (?P<STRING>"(?:[^"]|"")*")
    | (?P<QIDENT>'(?:[^']|'')*')
    | (?P<OP><>|<=|>=|&&|\|\||[+\-*/(),.<>=!&])
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<SKIP>[ \t\r\n]+)
    """,
    re.VERBOSE,
)


def tokenize(text):
    toks = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError(f"bad token at {pos}: {text[pos:pos+20]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "SKIP":
            continue
        toks.append(_Token(kind, m.group()))
    return toks


class ParseError(Exception):
    pass


class Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def next(self):
        t = self.peek()
        if t is None:
            raise ParseError("unexpected end of formula")
        self.i += 1
        return t

    def at_op(self, *ops):
        t = self.peek()
        return t is not None and t.type == "OP" and t.text in ops

    def at_ident(self, *words):
        t = self.peek()
        return t is not None and t.type == "IDENT" and t.text in words

    def expect_op(self, op):
        if not self.at_op(op):
            raise ParseError(f"expected {op!r}, got {self.peek()!r}")
        return self.next()

    def parse(self):
        node = self.parse_or()
        if self.peek() is not None:
            raise ParseError(f"trailing tokens: {self.peek()!r}")
        return node

    def parse_or(self):
        left = self.parse_and()
        while self.at_op("||") or self.at_ident("Or"):
            self.next()
            right = self.parse_and()
            left = ("bin", "or", left, right)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at_op("&&") or self.at_ident("And"):
            self.next()
            right = self.parse_not()
            left = ("bin", "and", left, right)
        return left

    def parse_not(self):
        if self.at_op("!") or self.at_ident("Not"):
            self.next()
            return ("un", "not", self.parse_not())
        return self.parse_compare()

    def parse_compare(self):
        left = self.parse_in()
        while self.at_op("=", "<>", "<=", ">=", "<", ">"):
            op = self.next().text
            right = self.parse_in()
            left = ("bin", op, left, right)
        return left

    def parse_in(self):
        left = self.parse_concat()
        while self.at_ident("in", "exactin"):
            op = self.next().text
            right = self.parse_concat()
            left = ("bin", op, left, right)
        return left

    def parse_concat(self):
        left = self.parse_add()
        while self.at_op("&"):
            self.next()
            right = self.parse_add()
            left = ("bin", "&", left, right)
        return left

    def parse_add(self):
        left = self.parse_mul()
        while self.at_op("+", "-"):
            op = self.next().text
            right = self.parse_mul()
            left = ("bin", op, left, right)
        return left

    def parse_mul(self):
        left = self.parse_unary()
        while self.at_op("*", "/"):
            op = self.next().text
            right = self.parse_unary()
            left = ("bin", op, left, right)
        return left

    def parse_unary(self):
        if self.at_op("-"):
            self.next()
            return ("un", "-", self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        t = self.peek()
        if t is None:
            raise ParseError("expected expression")
        if t.type == "NUMBER":
            self.next()
            return ("num", float(t.text) if "." in t.text else int(t.text))
        if t.type == "STRING":
            self.next()
            raw = t.text[1:-1].replace('""', '"')
            return ("str", raw)
        if t.type == "OP" and t.text == "(":
            self.next()
            e = self.parse_or()
            self.expect_op(")")
            return e
        if t.type in ("IDENT", "QIDENT"):
            segs = [self._ident_text(self.next())]
            while self.at_op("."):
                self.next()
                nt = self.peek()
                if nt is None or nt.type not in ("IDENT", "QIDENT"):
                    raise ParseError("expected identifier after '.'")
                segs.append(self._ident_text(self.next()))
            if self.at_op("("):
                self.next()
                args = []
                if not self.at_op(")"):
                    args.append(self.parse_or())
                    while self.at_op(","):
                        self.next()
                        args.append(self.parse_or())
                self.expect_op(")")
                return ("call", segs[-1], args)
            return ("path", segs)
        raise ParseError(f"unexpected token {t!r}")

    @staticmethod
    def _ident_text(tok):
        if tok.type == "QIDENT":
            return tok.text[1:-1].replace("''", "'")
        return tok.text


def parse_formula(raw):
    text = raw.strip()
    if text.startswith("="):
        text = text[1:]
    toks = tokenize(text)
    return Parser(toks).parse()


ENUM_NAMESPACES = {
    "FontWeight", "Align", "VerticalAlign", "ButtonAppearance", "DisplayMode", "TextMode",
    "DropShadow", "BorderStyle", "SortOrder", "TimeUnit", "NotificationType",
    "Overflow", "Live", "LayoutMode",
}

CALL_ALLOWLIST_NUMERIC = {"Max", "Min", "Abs", "Round", "RoundUp", "RoundDown"}


class EvalContext:
    """Per-property evaluation context. `dynamic` flips True the moment an If/Switch
    branch is taken *because its condition could not be statically determined* (a
    condition resolved against known `vars` doesn't set this -- see _eval_call).

    `vars`: OnStart-seeded scalar variables (see extract_var_seeds), optionally
    overridden by the caller (render-screen.py's --set flag). A bare `varFoo`
    reference, or `varFoo` used inside a comparison/boolean expression, resolves
    against this dict; anything not in it is UNRESOLVED, same as before this existed."""

    def __init__(self, parent_w=0, parent_h=0, this_item=None, self_resolved=None, theme=None, vars=None):
        self.parent_w = parent_w
        self.parent_h = parent_h
        self.this_item = this_item or {}
        self.self_resolved = self_resolved or {}
        self.theme = theme or {}
        self.vars = vars or {}
        self.dynamic = False
        self.missing_theme_tokens = set()


def _as_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return None


def _as_str(v):
    if v is UNRESOLVED:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    if isinstance(v, str):
        return v
    return None


def eval_ast(node, ctx: EvalContext):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "str":
        return node[1]
    if kind == "path":
        return _eval_path(node[1], ctx)
    if kind == "call":
        return _eval_call(node[1], node[2], ctx)
    if kind == "un":
        op, x = node[1], node[2]
        v = eval_ast(x, ctx)
        if op == "-":
            n = _as_number(v)
            return -n if n is not None else UNRESOLVED
        if op == "not":
            return (not v) if isinstance(v, bool) else UNRESOLVED
        return UNRESOLVED
    if kind == "bin":
        op, l, r = node[1], node[2], node[3]
        # "in"/"exactin" are substring-membership tests over runtime collections/text
        # (e.g. Lower(x) in Lower(y)) -- not modeled, always UNRESOLVED regardless of
        # operands (deliberately conservative: we'd rather stay silent than guess a
        # substring match).
        if op in ("in", "exactin"):
            return UNRESOLVED
        if op == "and":
            lv = eval_ast(l, ctx)
            if lv is False:
                return False  # short-circuit: known-false makes the whole AND false
            rv = eval_ast(r, ctx)
            if rv is False:
                return False
            if lv is True and rv is True:
                return True
            return UNRESOLVED
        if op == "or":
            lv = eval_ast(l, ctx)
            if lv is True:
                return True
            rv = eval_ast(r, ctx)
            if rv is True:
                return True
            if lv is False and rv is False:
                return False
            return UNRESOLVED
        if op in ("=", "<>", "<=", ">=", "<", ">"):
            lv = eval_ast(l, ctx)
            rv = eval_ast(r, ctx)
            if lv is UNRESOLVED or rv is UNRESOLVED:
                return UNRESOLVED
            try:
                if op == "=":
                    return lv == rv
                if op == "<>":
                    return lv != rv
                if op == "<":
                    return lv < rv
                if op == ">":
                    return lv > rv
                if op == "<=":
                    return lv <= rv
                if op == ">=":
                    return lv >= rv
            except TypeError:
                return UNRESOLVED
        lv = eval_ast(l, ctx)
        rv = eval_ast(r, ctx)
        if op == "&":
            ls, rs = _as_str(lv), _as_str(rv)
            if ls is None or rs is None:
                return UNRESOLVED
            return ls + rs
        ln, rn = _as_number(lv), _as_number(rv)
        if ln is None or rn is None:
            return UNRESOLVED
        if op == "+":
            return ln + rn
        if op == "-":
            return ln - rn
        if op == "*":
            return ln * rn
        if op == "/":
            return ln / rn if rn != 0 else UNRESOLVED
    return UNRESOLVED


def _eval_path(segs, ctx: EvalContext):
    head = segs[0]
    if head == "Parent":
        if len(segs) == 2 and segs[1] == "Width":
            return ctx.parent_w
        if len(segs) == 2 and segs[1] == "Height":
            return ctx.parent_h
        # Inside a gallery template the renderer passes the ROW box as the parent, so the
        # template's own dimensions are exactly parent_w/parent_h (2026-09-28: rows sized by
        # Parent.TemplateWidth all fell back to the 100px default before this).
        if len(segs) == 2 and segs[1] == "TemplateWidth":
            return ctx.parent_w
        if len(segs) == 2 and segs[1] == "TemplateHeight":
            return ctx.parent_h
        return UNRESOLVED
    if head == "Self":
        if len(segs) == 2 and segs[1] in ctx.self_resolved:
            return ctx.self_resolved[segs[1]]
        return UNRESOLVED
    if head == "ThisItem":
        cur = ctx.this_item
        for seg in segs[1:]:
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                return UNRESOLVED
        return cur
    if head in THEME_VARS:
        if len(segs) == 2:
            tok = segs[1]
            if tok in ctx.theme:
                return ctx.theme[tok]
            ctx.missing_theme_tokens.add(tok)
            return UNRESOLVED
        # varTheme.Hex.X: a common storage form keeps Color
        # values at the top level plus a nested Hex sub-record of the SAME strings,
        # used for inline HTML (e.g. HtmlText: ="...color:" & varTheme.Hex.Text & "...").
        # Hex.X and X are the identical value by construction, so this is not a guess.
        if len(segs) == 3 and segs[1] == "Hex":
            tok = segs[2]
            if tok in ctx.theme:
                return ctx.theme[tok]
            ctx.missing_theme_tokens.add(tok)
            return UNRESOLVED
        return UNRESOLVED
    if head == "Font" and len(segs) == 2:
        return segs[1]
    if head in ENUM_NAMESPACES:
        return segs[-1]
    if len(segs) == 1 and segs[0].lower() in ("true", "false"):
        return segs[0].lower() == "true"
    if len(segs) == 1 and segs[0] in ctx.vars:
        return ctx.vars[segs[0]]
    return UNRESOLVED


def _eval_call(name, args, ctx: EvalContext):
    if name == "If":
        # If(cond1, then1, [cond2, then2, ...], [else]). Walk cond/then pairs: a
        # condition that resolves to a concrete True/False (usually via a known
        # OnStart-seeded var, e.g. Visible: =varWsRollupOpen, or Visible:
        # =Len(varWsAction)>0 once varWsAction is seeded) is followed for real --
        # that's not "dynamic", it's a statically-known answer. Only a condition
        # that stays UNRESOLVED falls back to the old heuristic (take that branch,
        # flag the control dynamic) so previously-passing behavior is preserved for
        # genuinely runtime-dependent conditions.
        if len(args) < 2:
            return UNRESOLVED
        i = 0
        n = len(args)
        while i + 1 < n:
            cond_val = eval_ast(args[i], ctx)
            if cond_val is True:
                return eval_ast(args[i + 1], ctx)
            if cond_val is False:
                i += 2
                continue
            ctx.dynamic = True
            return eval_ast(args[i + 1], ctx)
        if i < n:
            return eval_ast(args[i], ctx)  # trailing else, reached only if every
        return UNRESOLVED                  # condition above was statically False
    if name == "Switch":
        # Switch(expr, case1, val1, case2, val2, ..., [default]). Same idea: if expr
        # resolves concretely, walk cases for real; only fall back to "take the
        # first case's value, flag dynamic" when expr itself is UNRESOLVED.
        if not args:
            return UNRESOLVED
        expr_val = eval_ast(args[0], ctx)
        rest = args[1:]
        if expr_val is UNRESOLVED:
            if len(rest) >= 2:
                ctx.dynamic = True
                return eval_ast(rest[1], ctx)
            if len(rest) == 1:
                return eval_ast(rest[0], ctx)
            return UNRESOLVED
        i = 0
        while i + 1 < len(rest):
            case_val = eval_ast(rest[i], ctx)
            if case_val is not UNRESOLVED and case_val == expr_val:
                return eval_ast(rest[i + 1], ctx)
            i += 2
        if i < len(rest):
            return eval_ast(rest[i], ctx)  # default
        return UNRESOLVED
    if name == "Coalesce":
        for a in args:
            v = eval_ast(a, ctx)
            if v is not UNRESOLVED and v is not None:
                return v
        return UNRESOLVED
    if name in CALL_ALLOWLIST_NUMERIC:
        vals = [_as_number(eval_ast(a, ctx)) for a in args]
        if any(v is None for v in vals):
            return UNRESOLVED
        if name == "Max":
            return max(vals)
        if name == "Min":
            return min(vals)
        if name == "Abs":
            return abs(vals[0])
        if name == "Round":
            return round(vals[0])
        if name == "RoundUp":
            import math
            return math.ceil(vals[0])
        if name == "RoundDown":
            import math
            return math.floor(vals[0])
    if name == "RGBA":
        vals = [_as_number(eval_ast(a, ctx)) for a in args[:4]]
        if any(v is None for v in vals) or len(vals) < 4:
            return UNRESOLVED
        r, g, b, a = vals
        return f"rgba({int(r)},{int(g)},{int(b)},{a})"
    if name == "ColorValue":
        if not args:
            return UNRESOLVED
        v = eval_ast(args[0], ctx)
        s = _as_str(v)
        if s is None:
            return UNRESOLVED
        return s
    if name == "Blank":
        return UNRESOLVED
    if name == "Len":
        v = eval_ast(args[0], ctx) if args else UNRESOLVED
        s = _as_str(v)
        return float(len(s)) if s is not None else UNRESOLVED
    if name in ("Trim",):
        v = eval_ast(args[0], ctx) if args else UNRESOLVED
        s = _as_str(v)
        return s.strip() if s is not None else UNRESOLVED
    # Pure string shaping over an already-resolved value. These matter because
    # list-row text is very often Left(...)/Upper(...) of a
    # sample-data field, and leaving them UNRESOLVED renders every row as
    # "(dynamic text)" -- which defeats the point of reviewing truncation and
    # typography. They only ever narrow a value that resolved on its own; a
    # server-side call underneath still resolves to UNRESOLVED and stays flagged.
    if name in ("Upper", "Lower"):
        s = _as_str(eval_ast(args[0], ctx)) if args else None
        if s is None:
            return UNRESOLVED
        return s.upper() if name == "Upper" else s.lower()
    if name in ("Left", "Right") and len(args) == 2:
        s = _as_str(eval_ast(args[0], ctx))
        n = _as_number(eval_ast(args[1], ctx))
        if s is None or n is None:
            return UNRESOLVED
        n = max(0, int(n))
        return s[:n] if name == "Left" else (s[-n:] if n else "")
    if name == "Mid" and len(args) >= 2:
        s = _as_str(eval_ast(args[0], ctx))
        start = _as_number(eval_ast(args[1], ctx))
        if s is None or start is None:
            return UNRESOLVED
        start = max(1, int(start))
        if len(args) == 2:
            return s[start - 1:]
        n = _as_number(eval_ast(args[2], ctx))
        if n is None:
            return UNRESOLVED
        return s[start - 1:start - 1 + max(0, int(n))]
    if name == "Concatenate":
        out = []
        for a in args:
            piece = _as_str(eval_ast(a, ctx))
            if piece is None:
                return UNRESOLVED
            out.append(piece)
        return "".join(out)
    if name == "Text" and len(args) == 1:
        # Single-argument Text() is a plain to-string. Text(value, "dd mmm yyyy")
        # and friends carry a format the evaluator does NOT model, so they stay
        # UNRESOLVED rather than render a wrong-looking date.
        v = eval_ast(args[0], ctx)
        if v is UNRESOLVED:
            return UNRESOLVED
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(int(v)) if float(v).is_integer() else str(v)
        return _as_str(v) if _as_str(v) is not None else UNRESOLVED
    # Anything else (formatted Text, Filter, Sort, ForAll, With, LookUp, CountRows,
    # Substitute, Concat, DateAdd, Today, Now, User, IsBlank, First, Distinct, ...)
    # is outside the supported subset -- resolves to UNRESOLVED, which sends the
    # property down the catalog/fallback ladder and gets flagged if the catalog
    # can't cover it either.
    return UNRESOLVED


def eval_formula(raw, ctx: EvalContext):
    """Returns (value_or_UNRESOLVED, dynamic_bool). Never raises."""
    try:
        ast = parse_formula(raw)
        val = eval_ast(ast, ctx)
        return val, ctx.dynamic
    except Exception:
        return UNRESOLVED, ctx.dynamic


# ---------------------------------------------------------------------------
# varTheme extraction from an App.pa.yaml OnStart formula
# ---------------------------------------------------------------------------


def _find_matching(text, open_idx, open_ch, close_ch):
    depth = 1
    i = open_idx + 1
    in_str = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _split_top_level(s, sep):
    parts = []
    depth = 0
    cur = []
    in_str = False
    for c in s:
        if in_str:
            cur.append(c)
            if c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _resolve_color_literal(v):
    v = v.strip()
    m = re.match(r'^ColorValue\(\s*"([^"]+)"\s*\)$', v)
    if m:
        return m.group(1)
    m = re.match(r'^RGBA\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)$', v)
    if m:
        r, g, b, a = m.groups()
        return f"rgba({int(float(r))},{int(float(g))},{int(float(b))},{a})"
    m = re.match(r'^"((?:[^"]|"")*)"$', v)
    if m:
        return m.group(1).replace('""', '"')
    return None


def _extract_theme_records(text):
    records = {}
    for m in re.finditer(r"Set\(\s*(var\w+)\s*,\s*\{", text):
        name = m.group(1)
        brace_open = m.end() - 1
        end = _find_matching(text, brace_open, "{", "}")
        if end == -1:
            continue
        body = text[brace_open + 1:end]
        fields = {}
        for f in _split_top_level(body, ","):
            if ":" not in f:
                continue
            k, v = f.split(":", 1)
            k = k.strip().strip("'\"")
            val = _resolve_color_literal(v.strip())
            if val is not None:
                fields[k] = val
        if fields:
            records[name] = fields
    return records


def _find_active_theme_name(text, records):
    dm = re.search(r"Set\(\s*varDark\s*,\s*(true|false)\s*\)", text, re.IGNORECASE)
    dark = bool(dm and dm.group(1).lower() == "true")

    m = re.search(r"Set\(\s*varTheme\s*,\s*", text)
    if not m:
        for cand in ("varThemeLight", "varTheme"):
            if cand in records:
                return cand
        return next(iter(records), None)
    paren_idx = text.index("(", m.start())
    end = _find_matching(text, paren_idx, "(", ")")
    if end == -1:
        return next(iter(records), None)
    expr = text[m.end():end].strip()
    mm = re.match(r"^If\(\s*varDark\s*,\s*(var\w+)\s*,\s*(var\w+)\s*\)$", expr)
    if mm:
        return mm.group(1) if dark else mm.group(2)
    mm2 = re.match(r"^(var\w+)$", expr)
    if mm2:
        return mm2.group(1)
    return next(iter(records), None)


def extract_theme(app_doc):
    """app_doc: parsed App.pa.yaml document (dict). Returns (theme_dict, theme_name,
    all_records) -- theme_dict maps token -> CSS color string for the active theme."""
    onstart = _get_onstart_text(app_doc)
    if onstart is None:
        return {}, None, {}
    records = _extract_theme_records(onstart)
    active = _find_active_theme_name(onstart, records)
    return records.get(active, {}), active, records


def _get_onstart_text(app_doc):
    try:
        onstart = app_doc["App"]["Properties"]["OnStart"]
    except (KeyError, TypeError):
        return None
    return onstart if isinstance(onstart, str) else None


def _find_all_set_calls(text):
    """Every top-level `Set(varName, <value>)` call in `text` (typically an App
    OnStart formula), as (name, raw_value_text, start_index), in the order they
    appear. raw_value_text is everything between the comma and the call's own
    matching closing paren -- may itself be a record `{...}`, a call like
    `If(varDark, varThemeDark, varThemeLight)`, or a plain literal."""
    results = []
    for m in re.finditer(r"Set\(\s*(var\w+)\s*,\s*", text):
        name = m.group(1)
        paren_idx = text.index("(", m.start())
        end = _find_matching(text, paren_idx, "(", ")")
        if end == -1:
            continue
        results.append((name, text[m.end():end], m.start()))
    return results


_BOOL_LITERAL_RE = re.compile(r"^(true|false)$", re.IGNORECASE)
_NUM_LITERAL_RE = re.compile(r"^-?\d+(\.\d+)?$")
_STR_LITERAL_RE = re.compile(r'^"((?:[^"]|"")*)"$')


def extract_var_seeds(app_doc):
    """Scans an App.pa.yaml's OnStart formula for `Set(varX, <literal>)` calls where
    the value is *exactly* a bare boolean/number/string literal (record literals like
    the varTheme ones, and anything formula-derived like `Set(varTheme, varThemeLight)`
    or `Set(varCurrentUserUPN, Lower(User().Email))`, are left alone -- not a static
    answer, so intentionally not seeded). Later Set() calls for the same name win,
    same as OnStart's own top-to-bottom execution order. This is what lets a bare
    `Visible: =varWsLoading` or a comparison like `Visible: =Len(varWsAction)>0`
    resolve to a real True/False during rendering instead of always being treated as
    unresolvable."""
    onstart = _get_onstart_text(app_doc)
    if onstart is None:
        return {}
    seeds = {}
    for name, raw_value, _start in _find_all_set_calls(onstart):
        v = raw_value.strip()
        if v.startswith("{"):
            continue  # a varTheme-style record; extract_theme() owns those
        m = _BOOL_LITERAL_RE.match(v)
        if m:
            seeds[name] = v.lower() == "true"
            continue
        if _NUM_LITERAL_RE.match(v):
            seeds[name] = float(v) if "." in v else int(v)
            continue
        m = _STR_LITERAL_RE.match(v)
        if m:
            seeds[name] = m.group(1).replace('""', '"')
            continue
        # else: formula-derived (If(...), another var, a function call, Blank(), ...)
        # -- not a literal, so not something we can safely seed.
    return seeds


def load_app_theme_for_screen(screen_path):
    """Looks for an App.pa.yaml next to the given screen file. Returns
    (theme_dict, theme_name, var_seeds, app_doc_or_None, all_theme_records). var_seeds
    is {} (not an error) when there's no sibling App.pa.yaml or it has no OnStart to
    scan. all_theme_records maps every Set(varThemeXxx, {...}) record found (e.g.
    "varThemeLight" / "varThemeDark") to its token dict -- lets a caller render the
    SAME screen in the other theme without touching App.pa.yaml (see render_screen's
    theme_override param, used to render the same screen in each theme)."""
    screen_path = Path(screen_path)
    app_path = screen_path.parent / "App.pa.yaml"
    if not app_path.exists():
        return {}, None, {}, None, {}
    try:
        app_doc = load_pa_yaml(app_path)
    except Exception:
        return {}, None, {}, None, {}
    theme, name, all_records = extract_theme(app_doc)
    var_seeds = extract_var_seeds(app_doc)
    return theme, name, var_seeds, app_doc, all_records


# ---------------------------------------------------------------------------
# defaults-catalog.json loading + resolution ladder
# ---------------------------------------------------------------------------

CATALOG_PATH = Path(__file__).resolve().parent / "defaults-catalog.json"
NEUTRAL_THEME_PATH = Path(__file__).resolve().parent / "theme-neutral.json"

# Record variable name(s) formulas read theme tokens from (varTheme.X / varTheme.Hex.X). --theme-var adds more.
THEME_VARS = {"varTheme"}


def load_theme_file(path=None):
    """A token file ({"tokens": {...}} or a flat {token: color}) -> dict. Default: the neutral palette."""
    p = Path(path) if path else NEUTRAL_THEME_PATH
    data = json.loads(p.read_text(encoding="utf-8"))
    return dict(data.get("tokens", data)) if isinstance(data, dict) else {}

FALLBACK_DEFAULTS = {
    "X": 0, "Y": 0,
    "Width": 150, "Height": 32,
    "Size": 11,
    "Color": "#333333",
    "Fill": "rgba(0,0,0,0)",
    "BorderColor": "#999999",
    "BorderThickness": 1,
    "RadiusTopLeft": 0, "RadiusTopRight": 0, "RadiusBottomLeft": 0, "RadiusBottomRight": 0,
    "FontWeight": "Normal",
    "Align": "Left",
    "VerticalAlign": "Top",
    "Wrap": True,
    "PaddingTop": 4, "PaddingRight": 4, "PaddingBottom": 4, "PaddingLeft": 4,
    "TemplateSize": 100,
    "TemplatePadding": 0,
}


def load_catalog(path=None):
    p = Path(path) if path else CATALOG_PATH
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def catalog_lookup(catalog, key, prop):
    """Returns (value, source_label, matched_key) or (None, None, None)."""
    entry = catalog.get(key)
    if entry and prop in entry:
        return entry[prop]["value"], entry[prop]["source"], key
    base = key.split("@")[0]
    for k, v in catalog.items():
        if k.split("@")[0] == base and prop in v:
            return v[prop]["value"], v[prop]["source"] + ":version-fallback", k
    return None, None, None


KNOWN_CONTROLS = {
    "GroupContainer", "Rectangle", "Label", "Classic/Button", "ModernButton",
    "Classic/TextInput", "ModernTextInput", "ModernDropdown", "ModernDatePicker",
    "Gallery", "HtmlViewer", "Image", "Attachments",
}


def base_name(key):
    return key.split("@")[0]


def is_known_control(key):
    return base_name(key) in KNOWN_CONTROLS


# ---------------------------------------------------------------------------
# Authored canvas size (DocumentLayoutWidth/Height out of the app's own .msapp)
# ---------------------------------------------------------------------------

DEFAULT_AUTHORED_SIZE = (1366, 768)


def find_authored_size(screen_path, default=DEFAULT_AUTHORED_SIZE):
    """Power Apps renders each screen at whatever Parent.Width/Height the live host
    gives it (often NOT the authored size at all -- Studio reflows to the viewport), but the *authored design size* -- the canvas size Studio's
    editor grid uses, and the one this renderer should default to for a stable,
    reproducible preview -- lives in the app's packed .msapp under
    Properties.json -> DocumentLayoutWidth/DocumentLayoutHeight. Never assume
    1366x768; read it.

    Finds the .msapp by walking upward from the screen file looking for a
    directory containing one or more *.msapp files, then opening each candidate to
    confirm it actually packages this exact screen (Src/<screen-filename> present
    inside the zip) before trusting its DocumentLayoutWidth/Height -- a product
    directory can hold more than one .msapp (e.g. an old/renamed export sitting
    next to the current one), so "found *a* .msapp nearby" isn't enough.

    Returns (width, height, source_msapp_path_or_None). Falls back to `default`
    (and a None source) if no .msapp anywhere upward packages this screen, or none
    can be read.
    """
    screen_path = Path(screen_path)
    screen_filename = screen_path.name

    search_dirs = []
    p = screen_path.parent
    for _ in range(6):
        p = p.parent
        if p == p.parent:  # hit filesystem root
            break
        search_dirs.append(p)

    candidates = []
    seen = set()
    for d in search_dirs:
        if not d.exists():
            continue
        for m in sorted(d.glob("*.msapp")):
            if m not in seen:
                seen.add(m)
                candidates.append(m)
    if not candidates:
        for d in search_dirs:
            if not d.exists():
                continue
            for m in sorted(d.glob("**/*.msapp")):
                if m not in seen:
                    seen.add(m)
                    candidates.append(m)

    for m in candidates:
        try:
            with zipfile.ZipFile(m) as z:
                names = {n.replace("\\", "/") for n in z.namelist()}
                if not any(n.endswith("Src/" + screen_filename) for n in names):
                    continue
                if "Properties.json" not in names:
                    continue
                data = json.loads(z.read("Properties.json").decode("utf-8-sig"))
        except Exception:
            continue
        w = data.get("DocumentLayoutWidth")
        h = data.get("DocumentLayoutHeight")
        if isinstance(w, (int, float)) and isinstance(h, (int, float)) and w > 0 and h > 0:
            return int(w), int(h), str(m)

    return default[0], default[1], None
