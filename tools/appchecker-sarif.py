#!/usr/bin/env python3
"""appchecker-sarif.py -- read Studio's App checker result out of a downloaded .msapp.

When a maker saves an app in Studio, the App checker result is embedded in the package as
AppCheckerResult.sarif. It is the ONLY machine-readable copy of Studio's formula errors: no source-level
tool, `pac canvas pack`, or Solution Checker re-analyses canvas formulas (Solution Checker merely echoes
this same file -- proven by deleting it: the checker then reports 0 issues).

    python tools/appchecker-sarif.py "<App>.msapp" [--all]
    python tools/appchecker-sarif.py --self-test

Prints the FORMULA errors (rule ids app-Err*) one per line:
    <control path>.<property> | <rule> | <message> | >><snippet>
Accessibility (acc-*), performance (app-Suggest*) and style results are counted but only listed with --all.
Exit 1 when any app-Err* result exists, so a pipeline can refuse a base that Studio itself marked red.
"""
import json
import os
import sys
import tempfile
import zipfile


def results(msapp_path):
    z = zipfile.ZipFile(msapp_path)
    names = [n for n in z.namelist() if n.replace('\\', '/').lower().endswith('appcheckerresult.sarif')]
    if not names:
        return None, []
    d = json.loads(z.read(names[0]).decode('utf-8-sig'))
    run = d['runs'][0]
    rules = {r['id']: r for r in run.get('tool', {}).get('driver', {}).get('rules', [])}
    out = []
    for r in run.get('results', []):
        rid = r.get('ruleId', '')
        tmpl = (rules.get(rid, {}).get('messageStrings') or {}).get('issue', {}).get('text', '')
        args = (r.get('message') or {}).get('arguments', [])
        try:
            text = tmpl.format(*args) if tmpl else ' '.join(str(a) for a in args)
        except Exception:
            text = tmpl + ' ' + ' '.join(str(a) for a in args)
        ph = (r.get('locations') or [{}])[0].get('physicalLocation', {})
        fq = (ph.get('address') or {}).get('fullyQualifiedName', '')
        snip = ((ph.get('region') or {}).get('snippet') or {}).get('text', '')
        out.append((rid, fq, text, snip))
    return names[0], out


def self_test():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'a.msapp')
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('AppCheckerResult.sarif', json.dumps({'runs': [{'tool': {'driver': {'rules': [
                {'id': 'app-ErrInvalidName', 'messageStrings': {'issue': {'text': "Name isn't valid. '{0}' isn't recognized."}}},
                {'id': 'acc-AccessibleLabelNeeded', 'messageStrings': {'issue': {'text': 'Missing accessible label'}}}]}},
                'results': [
                    {'ruleId': 'app-ErrInvalidName', 'message': {'arguments': ['colX']},
                     'locations': [{'physicalLocation': {'address': {'fullyQualifiedName': 'scrMain.galX.Items'},
                                                         'region': {'snippet': {'text': 'colX'}}}}]},
                    {'ruleId': 'acc-AccessibleLabelNeeded', 'message': {'arguments': []}, 'locations': []}]}]}))
        name, res = results(p)
        errs = [x for x in res if x[0].startswith('app-Err')]
        ok = name is not None and len(res) == 2 and len(errs) == 1 and "'colX' isn't recognized" in errs[0][2] \
            and errs[0][1] == 'scrMain.galX.Items'
        empty = os.path.join(d, 'b.msapp')
        with zipfile.ZipFile(empty, 'w') as z:
            z.writestr('Header.json', '{}')
        ok = ok and results(empty) == (None, [])
    print(('  ok   ' if ok else '  FAIL ') + 'formula error parsed with template arguments; no-sarif package handled')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    if '--self-test' in sys.argv[1:]:
        return self_test()
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    name, res = results(sys.argv[1])
    if name is None:
        print('no AppCheckerResult.sarif in', sys.argv[1], '(save the app in Studio, then Download a copy)')
        return 0
    errs = [x for x in res if x[0].startswith('app-Err')]
    for rid, fq, text, snip in (res if '--all' in sys.argv else errs):
        print('%s | %s | %s | >>%s' % (fq, rid, text, snip[:140].replace('\n', ' ')))
    print('\n%d formula error(s) (app-Err*), %d other result(s) in %s' % (len(errs), len(res) - len(errs), name))
    return 1 if errs else 0


if __name__ == '__main__':
    sys.exit(main())
