#!/usr/bin/env python3
"""canvas-lint.py -- offline checks for canvas-app source (.pa.yaml, "Source Code" layout).

`pac canvas pack` validates almost nothing and `pac canvas validate` is retired. Studio's strict YAML
parser, App checker and the service's runtime compiler are where canvas mistakes normally surface --
after a slow import. Every rule below is one of those failures, caught locally in a second.
(Incidents and evidence: reference/platform-traps.md, section "Canvas apps".)

    python tools/canvas-lint.py <Src dir | unpacked .msapp dir | file.pa.yaml> [...] [--strict]
    python tools/canvas-lint.py --self-test

ERRORS (exit 1)                                       what happens if you ship it
  yaml-strict          strict YAML parse fails        Studio refuses to open the app (PA1001)
  inline-colon-space   inline `Prop: =...` holding ': ' Studio refuses to open (PA1001 YamlInvalidSyntax)
  inline-hash          inline formula holding ' #'   YAML cuts the formula at the '#' (comment)
  tab                  tab character                  YAML forbids tabs in indentation
  dup-property         a property set twice on one control
  dup-control          two controls with the same name (names are app-global)
  unbalanced           unbalanced ( ) [ ] { } or an unterminated "string" in a formula
  utcnow               UTCNow()                       compiled to a NULL rule: the button/OnStart silently does nothing
  a11y-classic-button  AccessibleLabel on Classic/Button  runtime package stays "InProgress" forever
  hover-on-shape       Hover* on a Label/Rectangle    runtime package hangs (use a separate hover layer)
  unknown-property     property the control template rejects at import (PA2108)
  font-enum            Font.'Consolas' / Font.'Inter' (not in the Font enum; use Font.'Courier New')
  search-quoted-cols   Search(src, text, "Col")       column args are bare identifiers; the gallery never fills
  reserved-name        Set(Theme, ...)                'Theme' collides with a built-in; use varTheme

WARNINGS (exit 0 unless --strict)
  coalesce-empty       Coalesce(x, "") = ""           always false (Coalesce turns "" into Blank()); use IsBlank(x)
  set-blank-only       Set(v, Blank()) and never a typed value   "No type found for variable"
  untyped-empty-table  If(..., [], <table>)           [] has no schema; ThisItem.* stops resolving. Use Filter(T, false)
  json-whole-record    JSON(ThisItem) / JSON(x.Selected)  throws "contains media" on SharePoint records
  timer-tooltip-semi   ';' inside a Timer Tooltip string  observed with a runtime package that never finished
  onvisible-select     start screen OnVisible calls Select() while App.OnStart exists
                       (non-blocking OnStart: OnVisible runs in parallel and the Select can be dropped)
  select-shared-var    Set(v, ...) ; Select(ctl) repeated in one formula  Select is QUEUED; every run sees the last v
  alias-concat-filter  Concat(Filter(t As X, ...), X.col, ...) in a property  measured compiling to a NULL rule
  screen-yaml-only     a screen present only in YAML (no Controls/*.json in the base)  create screens in Studio first
  text-clipping        literal Height smaller than the text Size needs
"""
import argparse
import os
import re
import sys
import tempfile

try:
    import yaml  # PyYAML: strict parse, same failure class as Studio's importer
except ImportError:  # pragma: no cover
    yaml = None

# Properties each template REJECTS at import (PA2108 "Unknown property"), all observed live.
REJECTED_PROPS = [
    (re.compile(r'^ModernButton@'), {'HoverFill', 'Fill'}),
    (re.compile(r'^ModernTextInput@'), {'Mode'}),
    (re.compile(r'^ModernDropdown@'), {'Tooltip'}),
    (re.compile(r'^Classic/DropDown@'), {'HintText', 'RadiusTopLeft', 'RadiusTopRight', 'RadiusBottomLeft', 'RadiusBottomRight'}),
    (re.compile(r'^Rectangle@'), {'RadiusTopLeft', 'RadiusTopRight', 'RadiusBottomLeft', 'RadiusBottomRight'}),
    (re.compile(r'^Classic/'), {'Appearance', 'BasePaletteColor'}),
]
HOVER_SENSITIVE = re.compile(r'^(Label|Rectangle)@')
CLASSIC_BUTTON = re.compile(r'^Classic/Button@')
TEXT_CONTROLS = re.compile(r'^(Label|Classic/Button|ModernButton|Classic/TextInput|ModernTextInput)@')
MODERN = re.compile(r'^Modern')


class Entity(object):
    def __init__(self, kind, name, ctype, line, path):
        self.kind, self.name, self.ctype, self.line, self.path = kind, name, ctype, line, path
        self.props = {}          # name -> (formula text, first line number)
        self.dups = []


def strip_strings(f):
    """Blank out "..." string literals (Power Fx doubles quotes to escape them) so rules see code only."""
    return re.sub(r'"(?:[^"]|"")*"', lambda m: '"' + ' ' * (len(m.group(0)) - 2) + '"', f)


def strip_comments(f):
    out = []
    for ln in strip_strings(f).split('\n'):
        out.append(re.sub(r'//.*$', '', ln))
    return '\n'.join(out)


def parse_pa_yaml(text, path):
    """Line-based walk of a .pa.yaml: App, Screens and every control with its properties."""
    ents, lines = [], text.split('\n')
    cur_owner, prop_indent, in_screens = None, None, False
    owners = []   # (indent, entity)
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip('\r')
        n = i + 1
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        ind = len(raw) - len(raw.lstrip(' '))
        if ind == 0:
            in_screens = stripped == 'Screens:'
            prop_indent = None
            if stripped == 'App:':
                e = Entity('app', 'App', 'App', n, path)
                ents.append(e)
                owners = [(0, e)]
            else:
                owners = []
            i += 1
            continue
        if in_screens and ind == 2 and re.match(r'^[A-Za-z_]\w*:\s*$', stripped):
            e = Entity('screen', stripped[:-1], 'Screen', n, path)
            ents.append(e)
            owners = [(2, e)]
            prop_indent = None
            i += 1
            continue
        m = re.match(r'^- ([A-Za-z_]\w*):\s*$', stripped)
        if m:
            ctype = ''
            for j in range(i + 1, min(i + 4, len(lines))):
                mm = re.match(r'^\s*Control:\s*(\S+)', lines[j])
                if mm:
                    ctype = mm.group(1)
                    break
            e = Entity('control', m.group(1), ctype, n, path)
            ents.append(e)
            owners = [o for o in owners if o[0] < ind] + [(ind, e)]
            prop_indent = None
            i += 1
            continue
        if stripped == 'Properties:':
            owner = None
            for oi, oe in reversed(owners):
                if oi < ind:
                    owner = oe
                    break
            cur_owner, prop_indent = owner, ind + 2
            i += 1
            continue
        if prop_indent is not None and ind == prop_indent and cur_owner is not None:
            pm = re.match(r'^([A-Za-z_][\w.]*):\s*(.*)$', stripped)
            if pm:
                pname, val = pm.group(1), pm.group(2)
                if re.match(r'^[|>][-+]?\s*$', val):
                    body, j = [], i + 1
                    while j < len(lines):
                        r2 = lines[j].rstrip('\r')
                        if r2.strip() and (len(r2) - len(r2.lstrip(' '))) <= ind:
                            break
                        body.append(r2.strip() if not r2.strip() else r2[ind + 2:] if len(r2) > ind + 2 else r2.strip())
                        j += 1
                    val = '\n'.join(body).rstrip('\n')
                    i = j
                else:
                    i += 1
                if pname in cur_owner.props:
                    cur_owner.dups.append((pname, n))
                else:
                    cur_owner.props[pname] = (val, n)
                continue
        if prop_indent is not None and ind < prop_indent:
            prop_indent = None
        i += 1
    return ents


def balanced(formula):
    f = strip_comments(formula)
    if f.count('"') % 2:
        return 'unterminated "string"'
    stack, pairs = [], {')': '(', ']': '[', '}': '{'}
    for ch in re.sub(r'"[^"]*"', '""', f):
        if ch in '([{':
            stack.append(ch)
        elif ch in ')]}':
            if not stack or stack[-1] != pairs[ch]:
                return 'unexpected %r' % ch
            stack.pop()
    return 'unclosed %r' % stack[-1] if stack else None


def min_height(size, ctype):
    px = size if MODERN.match(ctype or '') else size * 4.0 / 3.0
    return int(px * 1.35 + 0.999)


def lint_text(text, path, issues, controls_seen, all_ents):
    add = lambda sev, rule, n, msg: issues.append((sev, rule, path, n, msg))  # noqa: E731
    lines = text.split('\n')
    block_indent = -1
    for idx, raw in enumerate(lines, 1):
        line = raw.rstrip('\r')
        if '\t' in line:
            add('error', 'tab', idx, 'tab character (YAML indentation must be spaces)')
        s = line.lstrip(' ')
        ind = len(line) - len(s)
        if block_indent >= 0:
            if not s or ind > block_indent:
                continue
            block_indent = -1
        if s.startswith('#'):
            continue
        if re.match(r'^(- )?[^\s:#][^:]*:\s*[|>][-+]?\s*$', s):
            block_indent = ind
            continue
        m = re.match(r'^([A-Za-z_][\w.]*):\s+(=.*)$', s)
        if m:
            val = m.group(2)
            if ': ' in val or val.endswith(':'):
                add('error', 'inline-colon-space', idx, "%s is an inline formula containing ': ' -- Studio refuses to "
                    "open the app (PA1001). Write '%s: |-' and put =formula on the next line, 2 spaces deeper"
                    % (m.group(1), m.group(1)))
            elif ' #' in val:
                add('error', 'inline-hash', idx, "%s is an inline formula containing ' #' -- YAML cuts it there; use a "
                    "|- block scalar" % m.group(1))
    if yaml is not None:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as ex:
            mark = getattr(ex, 'problem_mark', None)
            add('error', 'yaml-strict', (mark.line + 1) if mark else 0, 'strict YAML parse failed: %s'
                % str(ex).split('\n')[0])

    ents = parse_pa_yaml(text, path)
    all_ents.extend(ents)
    for e in ents:
        for pname, n in e.dups:
            add('error', 'dup-property', n, '%s.%s is set twice' % (e.name, pname))
        if e.kind == 'control':
            if e.name in controls_seen:
                add('error', 'dup-control', e.line, 'control name %s is already used at %s' % (e.name, controls_seen[e.name]))
            else:
                controls_seen[e.name] = '%s:%d' % (path, e.line)
        ctype = e.ctype or ''
        for pname, (val, n) in e.props.items():
            code = strip_comments(val)
            if val.lstrip().startswith('='):
                why = balanced(val.lstrip()[1:])
                if why:
                    add('error', 'unbalanced', n, '%s.%s: %s' % (e.name, pname, why))
            if re.search(r'\bUTCNow\s*\(', code, re.I):
                add('error', 'utcnow', n, '%s.%s calls UTCNow() -- the runtime compiler emits a NULL rule (silently dead). '
                    'Use Now() / TimeZoneOffset(Now())' % (e.name, pname))
            if re.search(r"Font\.'(Consolas|Inter)'", val):
                add('error', 'font-enum', n, "%s.%s: that font is not in the canvas Font enum -- use Font.'Courier New' "
                    "for monospace" % (e.name, pname))
            if re.search(r'\bSearch\s*\([^()]*?,[^()]*?,\s*"', val):
                add('error', 'search-quoted-cols', n, '%s.%s: Search() column arguments are bare identifiers, not '
                    '"strings" (the gallery silently never fills)' % (e.name, pname))
            if re.search(r'\bSet\s*\(\s*Theme\s*,', code):
                add('error', 'reserved-name', n, "%s.%s: 'Theme' is reserved -- name the variable varTheme" % (e.name, pname))
            if re.search(r'Coalesce\s*\([^()]*,\s*""\s*\)\s*=\s*""', val):
                add('warning', 'coalesce-empty', n, '%s.%s: Coalesce(x, "") = "" is always false; use IsBlank(x)' % (e.name, pname))
            if re.search(r'\bIf\s*\([^;]*,\s*\[\s*\]\s*,', code):
                add('warning', 'untyped-empty-table', n, '%s.%s: [] in an If branch has no schema -- use Filter(<table>, false)'
                    % (e.name, pname))
            if re.search(r'\bJSON\s*\(\s*(ThisItem|[A-Za-z_]\w*\.Selected)\s*[,)]', code):
                add('warning', 'json-whole-record', n, '%s.%s: JSON() of a whole SharePoint record throws "contains media"; '
                    'select the text fields first' % (e.name, pname))
            if re.search(r'Concat\s*\(\s*Filter\s*\([^()]*\bAs\s+\w+', code):
                add('warning', 'alias-concat-filter', n, '%s.%s: Concat(Filter(t As X, ...)) was measured compiling to a '
                    'NULL rule; use the unaliased Concat(Filter(t, cond), col, sep)' % (e.name, pname))
            sets = re.findall(r'\bSet\s*\(\s*(\w+)\s*,[^;]*;\s*Select\s*\(', code)
            if len(sets) >= 2:
                add('warning', 'select-shared-var', n, '%s.%s: Set(%s, ...); Select(...) repeated -- Select() is queued until '
                    'this formula ends, so every run sees the LAST value; let the worker pop its own queue' % (e.name, pname, sets[0]))
        if CLASSIC_BUTTON.match(ctype) and 'AccessibleLabel' in e.props:
            add('error', 'a11y-classic-button', e.props['AccessibleLabel'][1], '%s: AccessibleLabel on %s leaves the '
                'published runtime package InProgress forever; a classic button is announced by its Text' % (e.name, ctype))
        if HOVER_SENSITIVE.match(ctype):
            for pname in e.props:
                if pname.startswith('Hover'):
                    add('error', 'hover-on-shape', e.props[pname][1], '%s.%s on %s hangs the runtime package; put a '
                        'separate hover layer (a Classic button) over it' % (e.name, pname, ctype))
        for rx, bad in REJECTED_PROPS:
            if rx.match(ctype):
                for pname in sorted(bad & set(e.props)):
                    add('error', 'unknown-property', e.props[pname][1], '%s.%s: %s rejects this property at import (PA2108)'
                        % (e.name, pname, ctype))
        if ctype.startswith('Timer@') and 'Tooltip' in e.props and re.search(r'"[^"]*;[^"]*"', e.props['Tooltip'][0]):
            add('warning', 'timer-tooltip-semi', e.props['Tooltip'][1], "%s.Tooltip has ';' inside a string -- observed with "
                "a runtime package that never finished; reword" % e.name)
        if TEXT_CONTROLS.match(ctype):
            h, sz = e.props.get('Height'), e.props.get('Size')
            if h and sz:
                hm, sm = re.match(r'^=\s*(\d+(?:\.\d+)?)\s*$', h[0]), re.match(r'^=\s*(\d+(?:\.\d+)?)\s*$', sz[0])
                if hm and sm and float(hm.group(1)) < min_height(float(sm.group(1)), ctype):
                    add('warning', 'text-clipping', h[1], '%s: Height %s is below %d for Size %s (%s sizes are %s)' % (
                        e.name, hm.group(1), min_height(float(sm.group(1)), ctype), sm.group(1), ctype,
                        'px' if MODERN.match(ctype) else 'pt'))


def cross_file_checks(all_ents, issues, controls_json_screens):
    sets_typed, sets_blank = set(), {}
    app = next((e for e in all_ents if e.kind == 'app'), None)
    for e in all_ents:
        for pname, (val, n) in e.props.items():
            code = strip_comments(val)
            for m in re.finditer(r'\bSet\s*\(\s*(\w+)\s*,\s*([^;]*)', code):
                if re.match(r'^\s*Blank\s*\(\s*\)\s*\)?\s*$', m.group(2)):
                    sets_blank.setdefault(m.group(1), (e.path, n))
                else:
                    sets_typed.add(m.group(1))
    for v, (p, n) in sorted(sets_blank.items()):
        if v not in sets_typed:
            issues.append(('warning', 'set-blank-only', p, n, 'variable %s is only ever Set to Blank() -- it has no type '
                           '("No type found"); seed it with a typed value' % v))
    if app and 'OnStart' in app.props:
        screens = [e for e in all_ents if e.kind == 'screen']
        start = app.props.get('StartScreen')
        first = None
        if start:
            first = next((s for s in screens if s.name in start[0]), None)
        first = first or (screens[0] if screens else None)
        if first and 'OnVisible' in first.props and re.search(r'\bSelect\s*\(', first.props['OnVisible'][0]):
            issues.append(('warning', 'onvisible-select', first.path, first.props['OnVisible'][1],
                           '%s.OnVisible calls Select() while App.OnStart exists -- with non-blocking OnStart they run in '
                           'parallel and the Select can be dropped; boot from a Timer gated on a varOnStartDone flag' % first.name))
    if controls_json_screens is not None:
        for e in all_ents:
            if e.kind == 'screen' and e.name not in controls_json_screens:
                issues.append(('warning', 'screen-yaml-only', e.path, e.line, 'screen %s exists only in YAML (no '
                               'Controls/*.json in the base) -- create screens in Studio first, then download' % e.name))


def controls_screens(root):
    cdir = os.path.join(root, 'Controls')
    if not os.path.isdir(cdir):
        return None
    names = set()
    for f in os.listdir(cdir):
        if f.endswith('.json'):
            t = open(os.path.join(cdir, f), encoding='utf-8', errors='replace').read(20000)
            m = re.search(r'"TopParent"\s*:\s*\{[^{}]*?"Name"\s*:\s*"([^"]+)"', t)
            if m:
                names.add(m.group(1))
    return names


def collect(paths):
    files, ctl = [], None
    for p in paths:
        if os.path.isfile(p):
            files.append(p)
            continue
        src = os.path.join(p, 'Src') if os.path.isdir(os.path.join(p, 'Src')) else p
        if src != p:
            ctl = controls_screens(p)
        for dp, _dn, fn in os.walk(src):
            for f in sorted(fn):
                if f.endswith('.pa.yaml') and not f.startswith('_EditorState'):
                    files.append(os.path.join(dp, f))
    return files, ctl


def lint(paths):
    issues, seen, ents = [], {}, []
    files, ctl = collect(paths)
    for f in files:
        lint_text(open(f, encoding='utf-8-sig').read(), f, issues, seen, ents)
    cross_file_checks(ents, issues, ctl)
    return files, issues


def report(files, issues, strict):
    for sev, rule, path, n, msg in sorted(issues, key=lambda x: (x[2], x[3])):
        print('%s %s:%d [%s] %s' % ('ERROR' if sev == 'error' else 'WARN ', path, n, rule, msg))
    e = sum(1 for i in issues if i[0] == 'error')
    w = len(issues) - e
    print('canvas-lint: %d file(s), %d error(s), %d warning(s)%s' % (len(files), e, w, '' if yaml else ' (PyYAML missing: strict parse skipped)'))
    return 1 if e or (strict and w) else 0


# ------------------------------------------------------------------------------------ self-test
CLEAN = '''App:
  Properties:
    OnStart: |-
      =Set(varTheme, {Bg: RGBA(255, 255, 255, 1)});
      Set(varCount, 0);
      Set(varOnStartDone, true)
Screens:
  scrMain:
    Properties:
      Fill: =varTheme.Bg
    Children:
      - lblTitle:
          Control: Label@2.5.1
          Properties:
            Text: ="Help desk"
            Size: =12
            Height: =40
      - btnGo:
          Control: Classic/Button@2.2.0
          Properties:
            Text: ="Go"
            OnSelect: |-
              =Set(varCount, varCount + 1);
              Notify("Saved: " & Text(Now(), "hh:mm"))
            Size: =10
            Height: =36
      - galItems:
          Control: Gallery@2.15.0
          Variant: Vertical
          Properties:
            Items: =Search(colItems, txtFind.Text, Title)
'''


def self_test():
    ok = True

    def run(text, extra_files=None):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'Src')
            os.makedirs(src)
            open(os.path.join(src, 'App.pa.yaml'), 'w', encoding='utf-8').write(text)
            for name, t in (extra_files or {}).items():
                open(os.path.join(src, name), 'w', encoding='utf-8').write(t)
            _f, issues = lint([d])
            return issues

    def expect(mut, rule, sev='error', extra=None):
        nonlocal ok
        issues = run(mut(CLEAN), extra)
        hit = any(r == rule and s == sev for s, r, _p, _n, _m in issues)
        print(('  ok   ' if hit else '  FAIL ') + 'mutation caught: %s (%s)' % (rule, sev) + ('' if hit else ' got %s' % [(i[0], i[1]) for i in issues]))
        ok = ok and hit

    base = run(CLEAN)
    print(('  ok   ' if not base else '  FAIL ') + 'clean fixture: no findings %s' % ('' if not base else base))
    ok = ok and not base
    R = lambda old, new: (lambda t: t.replace(old, new, 1))  # noqa: E731
    expect(R('            Text: ="Help desk"', '            Text: =With({v: "Help desk"}, v)'), 'inline-colon-space')
    expect(R('            Text: ="Help desk"', '            Text: ="Help" & " #1"'), 'inline-hash')
    expect(R('      =Set(varCount, varCount + 1);', '      =Set(varCount, UTCNow());'), 'utcnow')
    expect(R('            Text: ="Go"\n', '            Text: ="Go"\n            AccessibleLabel: ="Go"\n'), 'a11y-classic-button')
    expect(R('            Text: ="Help desk"\n', '            Text: ="Help desk"\n            HoverColor: =Color.Red\n'), 'hover-on-shape')
    expect(R('            Text: ="Help desk"\n', '            Text: ="Help desk"\n            Text: ="Again"\n'), 'dup-property')
    expect(R('      - btnGo:', '      - lblTitle:'), 'dup-control')
    expect(R('            Size: =12', '            Size: =(12'), 'unbalanced')
    expect(R('Title)', '"Title")'), 'search-quoted-cols')
    expect(R('      =Set(varTheme,', '      =Set(Theme,'), 'reserved-name')
    expect(R('            Size: =12\n', "            Size: =12\n            Font: =Font.'Consolas'\n"), 'font-enum')
    expect(lambda t: t.replace('Control: Classic/Button@2.2.0', 'Control: ModernButton@1.0.0').replace(
        '            Text: ="Go"\n', '            Text: ="Go"\n            HoverFill: =Color.Red\n'), 'unknown-property')
    expect(R('            Height: =40', '            Height: =12'), 'text-clipping', 'warning')
    expect(R('      Set(varCount, 0);', '      Set(varDead, Blank());\n      Set(varCount, 0);'), 'set-blank-only', 'warning')
    expect(R('Items: =Search(colItems, txtFind.Text, Title)', 'Items: =If(IsBlank(txtFind.Text), [], colItems)'), 'untyped-empty-table', 'warning')
    expect(R('      Fill: =varTheme.Bg', '      Fill: =varTheme.Bg\n      OnVisible: =Select(btnGo)'), 'onvisible-select', 'warning')
    expect(R('              =Set(varCount, varCount + 1);', '              =Set(varA, 1); Select(btnGo); Set(varA, 2); Select(btnGo);'), 'select-shared-var', 'warning')
    expect(R('Items: =Search(colItems, txtFind.Text, Title)', 'Items: =JSON(ThisItem)'), 'json-whole-record', 'warning')
    expect(R('Text: ="Help desk"', 'Text: =Coalesce(varX, "") = ""'), 'coalesce-empty', 'warning')
    expect(R('Text: ="Help desk"', 'Text: =Concat(Filter(colItems As M, M.On), M.Id, ",")'), 'alias-concat-filter', 'warning')
    expect(R('            Size: =12', '\tSize: =12'), 'tab')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--strict', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.paths:
        ap.error('give a Src folder, an unpacked .msapp folder, or .pa.yaml files')
    files, issues = lint(a.paths)
    return report(files, issues, a.strict)


if __name__ == '__main__':
    sys.exit(main())
