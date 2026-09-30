#!/usr/bin/env python3
"""parse-appcheck.py -- turn a Studio "App checker" panel dump into a compact issue list.

The App checker panel has no export and its markup is ~250 KB of Fluent UI. Capture loop:
  1. In Studio open App checker and expand the results.
  2. DevTools (F12) -> select the results panel -> right-click the wrapping node -> Copy -> Copy outerHTML.
  3. Save it (e.g. debug/errors.html) and run:  python tools/parse-appcheck.py debug/errors.html
Never read the raw HTML into an agent's context -- it wastes tokens; parse it.

The panel encodes each issue as a run of title="..." attributes in document order: control, property,
message (an app/screen-level row may omit the property). Group headers ("Errors", "Warnings", ...) reset the
run and tag the severity. Output: per group, distinct messages with counts, then the control.property
locations. Errors cascade from ONE root cause (an unresolved data source or flow types a variable as Error,
then every .field / Coalesce / ParseJSON / comparison on it fails) -- read the least-frequent message first.

If DevTools are disabled on the machine: a screenshot of the expanded panel is readable by a vision model,
or save the app and read the embedded result with tools/appchecker-sarif.py instead.

    python tools/parse-appcheck.py <errors.html>
    python tools/parse-appcheck.py --self-test
"""
import html
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict

HEADERS = {'Errors', 'Warnings', 'Info', 'Accessibility', 'Performance', 'Rule', 'Formulas'}
IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def parse(path):
    raw = open(path, encoding='utf-8', errors='replace').read()
    titles = [html.unescape(t) for t in re.findall(r'title="([^"]*)"', raw)]
    rows, ids, group = [], [], 'Errors'
    for t in titles:
        t = t.strip()
        if not t:
            continue
        if t in HEADERS:
            group, ids = t, []
            continue
        if IDENT.match(t):
            ids = (ids + [t])[-2:]
        else:
            control = ids[-2] if len(ids) >= 2 else (ids[-1] if ids else '?')
            prop = ids[-1] if len(ids) >= 2 else ''
            rows.append((group, control, prop, t))
            ids = []
    return rows


def report(path, rows):
    if not rows:
        print('No issues parsed from %s (is it the App checker panel outerHTML?)' % path)
        return
    by = defaultdict(list)
    for g, c, p, m in rows:
        by[g].append((c, p, m))
    print('%d issue rows parsed from %s' % (len(rows), path))
    for group in sorted(by):
        items = by[group]
        print('\n=== %s (%d) ===' % (group, len(items)))
        for msg, n in Counter(m for _c, _p, m in items).most_common():
            print('  %3dx  %s' % (n, msg))
        print('  -- locations --')
        for (c, p), n in Counter((c, p) for c, p, _m in items).most_common(8):
            print('  %3dx  %s%s' % (n, c, ('.' + p) if p else ''))


def self_test():
    sample = ('<div title="Errors"></div><span title="galItems"></span><span title="Items"></span>'
              '<span title="Name isn&#39;t valid. &#39;colTickets&#39; isn&#39;t recognized."></span>'
              '<span title="lblCount"></span><span title="Text"></span>'
              '<span title="Name isn&#39;t valid. &#39;colTickets&#39; isn&#39;t recognized."></span>'
              '<div title="Warnings"></div><span title="scrMain"></span><span title="This screen has no accessible label."></span>')
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'errors.html')
        open(p, 'w', encoding='utf-8').write(sample)
        rows = parse(p)
    ok = rows == [
        ('Errors', 'galItems', 'Items', "Name isn't valid. 'colTickets' isn't recognized."),
        ('Errors', 'lblCount', 'Text', "Name isn't valid. 'colTickets' isn't recognized."),
        ('Warnings', 'scrMain', '', 'This screen has no accessible label.')]
    print(('  ok   ' if ok else '  FAIL ') + 'groups, control.property and messages recovered %s' % ('' if ok else rows))
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    if '--self-test' in sys.argv[1:]:
        return self_test()
    path = sys.argv[1] if len(sys.argv) > 1 else 'debug/errors.html'
    report(path, parse(path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
