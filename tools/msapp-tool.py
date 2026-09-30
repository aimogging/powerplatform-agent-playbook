#!/usr/bin/env python3
"""msapp-tool.py -- turn edited .pa.yaml sources into a .msapp a human imports by hand.

A .msapp is a zip. Studio only reads the YAML under Src/ when the package carries pac's marker file
packed.json with "LoadConfiguration": {"LoadFromYaml": true}. A raw Studio "Download a copy" has Src/
but NO packed.json, so Studio loads the compiled Controls/*.json and silently IGNORES every Src edit.
That single fact is the difference between "my edit shipped" and "nothing changed".

Subcommands
  unpack <app.msapp> <dir>               extract a Studio download for editing (refuses a non-empty dir)
  lint   <dir>                           package-level checks + tools/canvas-lint.py over <dir>/Src
  pack   <dir> <out.msapp>               lint, add packed.json if missing, zip the folder
  stamp  <base.msapp> <SrcDir> <out.msapp>
                                         copy every entry of a Studio-downloaded BASE byte-for-byte, replace
                                         Src/*.pa.yaml from SrcDir, add packed.json (the base keeps the
                                         tenant bindings: data sources, flow ids, connection ids)
  msapr  <base.msapp> <out.msapr>        resource pack for the pac route:
                                         pac canvas pack --sources <dir with Src/ and the .msapr> --msapp out.msapp
  info   <app.msapp>                     entries, packed.json state, Studio's app name, App checker counts
  --self-test                            offline test with a synthetic package

Proven route (a real tenant): Download a copy -> unpack -> edit Src -> add packed.json -> zip -> Import app >
From file -> App checker 0 formula errors -> Save as > Replace existing -> Publish -> runtime package Ready.
That proof zipped with Windows tar ("tar -a -cf x.zip", forward-slash names, directory entries).
[UNVERIFIED] this script's Python zip of the same tree has not itself been imported; if Studio refuses it,
rezip the unpacked folder with tar (tar -a -cf out.zip -C <dir> .) and rename .zip -> .msapp.
Studio opened Python-written msapps in the 'stamp' shape before (entries copied from a Studio download).

Traps this tool refuses or warns about: see reference/platform-traps.md "Canvas packaging".
"""
import argparse
import datetime
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BS = chr(92)
PACKED_JSON = ('{"PackedStructureVersion":"0.1","LastPackedDateTimeUtc":"%s",'
               '"PackingClient":{"Name":"Pac CLI","Version":"2.8.1"},"LoadConfiguration":{"LoadFromYaml":true}}')


def _load(modfile, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, modfile))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def norm(n):
    return n.replace(BS, '/')


def packed_json():
    return PACKED_JSON % datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')


def app_name_from(get):
    raw = get('Resources/PublishInfo.json')
    if raw is None:
        return '(no Resources/PublishInfo.json)'
    m = re.search(r'"AppName"\s*:\s*"([^"]*)"', raw.decode('utf-8-sig', 'replace'))
    return m.group(1) if m else '(AppName not found)'


def cmd_unpack(msapp, dst):
    if os.path.isdir(dst) and os.listdir(dst):
        raise SystemExit('refusing: %s is not empty' % dst)
    os.makedirs(dst, exist_ok=True)
    with zipfile.ZipFile(msapp) as z:
        for n in z.namelist():
            nn = norm(n)
            if nn.endswith('/'):
                continue
            p = os.path.join(dst, *nn.split('/'))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'wb') as fh:
                fh.write(z.read(n))
        names = [norm(n) for n in z.namelist()]
        name = app_name_from(lambda e: next((z.read(n) for n in z.namelist() if norm(n) == e), None))
    print('RESULT: unpacked %s -> %s (%d entries)' % (msapp, dst, len(names)))
    if not any(n.startswith('Src/') for n in names):
        print('WARN: no Src/ folder -- this package cannot be edited as YAML. Open it in Studio, Save, Download a copy again.')
    if 'packed.json' not in names:
        print('NOTE: no packed.json (a raw Studio download). pack/stamp add one; without it Studio ignores Src edits.')
    print("NOTE: edit only Src/*.pa.yaml. Studio will show the app name: %s" % name)
    print('NOTE: a file opened in Studio is a NEW unsaved app with that name -- use Save as > Replace existing, never plain Save.')


def package_lint(root):
    errors, warnings = [], []
    src = os.path.join(root, 'Src')
    if not os.path.isdir(src):
        errors.append('no Src/ folder in %s -- not an unpacked canvas app' % root)
        return errors, warnings
    for req in ('Header.json', 'Properties.json'):
        if not os.path.isfile(os.path.join(root, req)):
            errors.append('missing %s at the package root -- unpack the whole .msapp, not only Src' % req)
    for dp, _dn, fn in os.walk(src):
        for f in fn:
            if not f.endswith('.pa.yaml'):
                errors.append('Src/%s is not a .pa.yaml file (editor backup?) -- remove it' % f)
    pj = os.path.join(root, 'packed.json')
    if os.path.isfile(pj) and not re.search(r'"LoadFromYaml"\s*:\s*true', open(pj, encoding='utf-8-sig').read()):
        errors.append('packed.json does not set "LoadFromYaml": true -- Studio would ignore Src; delete it and let pack write it')
    return errors, warnings


def run_canvas_lint(root):
    cl = _load('canvas-lint.py', 'canvas_lint')
    files, issues = cl.lint([root])
    return cl.report(files, issues, False)


def cmd_lint(root):
    errors, warnings = package_lint(root)
    for w in warnings:
        print('WARN: ' + w)
    for e in errors:
        print('LINT ERROR: ' + e)
    rc = run_canvas_lint(root) if os.path.isdir(os.path.join(root, 'Src')) else 1
    return 1 if errors or rc else 0


def cmd_pack(root, out, skip_lint=False):
    if not out.lower().endswith('.msapp'):
        raise SystemExit('the output must end in .msapp')
    if not skip_lint and cmd_lint(root):
        raise SystemExit('refusing to pack: fix the LINT errors above (or --skip-lint)')
    pj = os.path.join(root, 'packed.json')
    if not os.path.isfile(pj):
        with open(pj, 'w', encoding='ascii', newline='') as fh:
            fh.write(packed_json())
        print('NOTE: added packed.json (LoadFromYaml=true)')
    count = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for dp, _dn, fn in os.walk(root):
            for f in sorted(fn):
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, root).replace(os.sep, '/')
                if rel.startswith('.'):
                    continue
                z.write(full, rel)
                count += 1
    print('RESULT: packed %d entries -> %s' % (count, out))
    print('NEXT: Import app > From file (.msapp) -> check App checker -> Save as > Replace existing -> Publish')
    return 0


def refuse_if_red(base, allow_errors):
    sar = _load('appchecker-sarif.py', 'appchecker_sarif')
    name, res = sar.results(base)
    errs = [x for x in res if x[0].startswith('app-Err')]
    if errs and not allow_errors:
        for e in errs[:10]:
            print('  %s | %s | %s' % (e[1], e[0], e[2]))
        raise SystemExit('refusing: the base carries %d formula error(s) in Studio\'s own App checker result; '
                         'fix them in Studio first (or --allow-errors)' % len(errs))


def cmd_stamp(base, src_dir, out, allow_errors=False, skip_lint=False):
    refuse_if_red(base, allow_errors)
    if not skip_lint:
        cl = _load('canvas-lint.py', 'canvas_lint')
        files, issues = cl.lint([src_dir])
        if cl.report(files, issues, False):
            raise SystemExit('refusing to stamp: fix the canvas-lint errors above (or --skip-lint)')
    repo_src = {f for f in os.listdir(src_dir) if f.endswith('.pa.yaml')}
    replaced, added, seen = 0, 0, set()
    with zipfile.ZipFile(base) as z, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as o:
        for info in z.infolist():
            nn = norm(info.filename)
            if nn in ('packed.json', 'AppCheckerResult.sarif'):
                continue
            data = z.read(info.filename)
            if nn.startswith('Src/') and nn.endswith('.pa.yaml'):
                leaf = nn.split('/')[-1]
                seen.add(leaf)
                if leaf in repo_src:
                    data = io.open(os.path.join(src_dir, leaf), 'rb').read()
                    replaced += 1
            o.writestr(info, data)
        for leaf in sorted(repo_src - seen):
            o.writestr('Src/' + leaf, io.open(os.path.join(src_dir, leaf), 'rb').read())
            added += 1
            if not leaf.startswith(('App.', '_EditorState')):
                print('WARN: Src/%s is new (not in the base). A screen that exists only in YAML may not load -- '
                      'create the screen in Studio, Save, Download a copy, and stamp onto that.' % leaf)
        o.writestr('packed.json', packed_json())
    print('RESULT: stamped %s: %d Src file(s) replaced, %d added, packed.json set -> %s' % (base, replaced, added, out))
    print('NEXT: Import app > From file (.msapp) -> App checker -> Save as > Replace existing -> Publish')
    return 0


def cmd_msapr(base, out, allow_errors=False):
    refuse_if_red(base, allow_errors)
    kept = 0
    with zipfile.ZipFile(base) as z, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as o:
        o.writestr('msapr-header.json', json.dumps({'MsaprStructureVersion': '0.1',
                                                    'UnpackedConfiguration': {'ContentTypes': ['PaYamlSourceCode']}}, indent=2))
        for n in z.namelist():
            nn = norm(n)
            if nn.startswith('Src/') or nn.endswith('/') or nn == 'AppCheckerResult.sarif':
                continue
            o.writestr('msapp/' + nn, z.read(n))
            kept += 1
    print('RESULT: msapr with %d entries -> %s' % (kept, out))
    print('NEXT: put it beside Src/ and run: pac canvas pack --sources <that dir> --msapp <out.msapp>')
    return 0


def cmd_info(msapp):
    with zipfile.ZipFile(msapp) as z:
        names = [norm(n) for n in z.namelist()]
        get = lambda e: next((z.read(n) for n in z.namelist() if norm(n) == e), None)  # noqa: E731
        pj = get('packed.json')
        print('entries: %d (Src: %d, Controls: %d)' % (len(names), sum(n.startswith('Src/') for n in names),
                                                     sum(n.startswith('Controls/') for n in names)))
        print('packed.json: %s' % ('absent -> Studio IGNORES Src' if pj is None else
                                   ('LoadFromYaml=true' if b'"LoadFromYaml":true' in pj.replace(b' ', b'') else 'present WITHOUT LoadFromYaml=true')))
        print('app name Studio shows: %s' % app_name_from(get))
    sar = _load('appchecker-sarif.py', 'appchecker_sarif')
    name, res = sar.results(msapp)
    if name:
        errs = sum(1 for x in res if x[0].startswith('app-Err'))
        print('App checker result inside: %d formula error(s), %d other' % (errs, len(res) - errs))
    else:
        print('App checker result inside: none')
    return 0


# ------------------------------------------------------------------------------------ self-test
def _synthetic_base(path, with_sarif_error=False):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('Header.json', '{"DocVersion":"1.0"}')
        z.writestr('Properties.json', '{"Name":"Demo"}')
        z.writestr('Resources/PublishInfo.json', '{"AppName":"Demo App"}')
        z.writestr('Controls/1.json', '{"TopParent":{"Type":"ControlInfo","Name":"App"}}')
        z.writestr('Controls/4.json', '{"TopParent":{"Type":"ControlInfo","Name":"scrMain"}}')
        z.writestr('Src/App.pa.yaml', 'App:\n  Properties:\n    OnStart: =Set(varN, 0)\n')
        z.writestr('Src/scrMain.pa.yaml', 'Screens:\n  scrMain:\n    Properties:\n      Fill: =Color.White\n')
        if with_sarif_error:
            z.writestr('AppCheckerResult.sarif', json.dumps({'runs': [{'tool': {'driver': {'rules': [
                {'id': 'app-ErrInvalidName', 'messageStrings': {'issue': {'text': 'Name isn\'t valid. {0}'}}}]}},
                'results': [{'ruleId': 'app-ErrInvalidName', 'message': {'arguments': ['x']},
                             'locations': [{'physicalLocation': {'address': {'fullyQualifiedName': 'scrMain.lbl.Text'}}}]}]}]}))


def self_test():
    ok = True

    def check(cond, what):
        nonlocal ok
        print(('  ok   ' if cond else '  FAIL ') + what)
        ok = ok and cond

    with tempfile.TemporaryDirectory() as d:
        base = os.path.join(d, 'base.msapp')
        _synthetic_base(base)
        un = os.path.join(d, 'un')
        cmd_unpack(base, un)
        check(os.path.isfile(os.path.join(un, 'Src', 'scrMain.pa.yaml')), 'unpack extracts Src')
        with open(os.path.join(un, 'Src', 'scrMain.pa.yaml'), 'a', encoding='utf-8') as fh:
            fh.write('    Children:\n      - lblHi:\n          Control: Label@2.5.1\n          Properties:\n            Text: ="Hi"\n')
        out = os.path.join(d, 'out.msapp')
        check(cmd_pack(un, out) == 0, 'pack succeeds on a clean edit')
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            check('packed.json' in names and b'"LoadFromYaml":true' in z.read('packed.json'), 'pack adds packed.json LoadFromYaml=true')
            check(all(BS not in n for n in names), 'pack writes forward-slash entry names')
            check(b'lblHi' in z.read('Src/scrMain.pa.yaml'), 'pack carries the Src edit')
        # a bad edit must be refused
        with open(os.path.join(un, 'Src', 'scrMain.pa.yaml'), 'a', encoding='utf-8') as fh:
            fh.write('      - lblBad:\n          Control: Label@2.5.1\n          Properties:\n            Text: =With({v: 1}, v)\n')
        refused = False
        try:
            cmd_pack(un, os.path.join(d, 'bad.msapp'))
        except SystemExit:
            refused = True
        check(refused, 'pack refuses an inline colon-space formula')
        # stamp: base bytes preserved, Src replaced, packed.json added
        src = os.path.join(d, 'src')
        os.makedirs(src)
        open(os.path.join(src, 'scrMain.pa.yaml'), 'w', encoding='utf-8').write(
            'Screens:\n  scrMain:\n    Properties:\n      Fill: =Color.Black\n')
        st = os.path.join(d, 'stamped.msapp')
        cmd_stamp(base, src, st)
        with zipfile.ZipFile(base) as zb, zipfile.ZipFile(st) as zs:
            check(zb.read('Properties.json') == zs.read('Properties.json'), 'stamp keeps base entries byte-identical')
            check(b'Color.Black' in zs.read('Src/scrMain.pa.yaml'), 'stamp replaces Src from the source dir')
            check(b'Set(varN' in zs.read('Src/App.pa.yaml'), 'stamp keeps base Src files the source dir does not have')
            check('packed.json' in zs.namelist(), 'stamp adds packed.json')
        red = os.path.join(d, 'red.msapp')
        _synthetic_base(red, with_sarif_error=True)
        refused = False
        try:
            cmd_stamp(red, src, os.path.join(d, 'x.msapp'))
        except SystemExit:
            refused = True
        check(refused, 'stamp refuses a base whose App checker result has formula errors')
        msapr = os.path.join(d, 'base.msapr')
        cmd_msapr(base, msapr)
        with zipfile.ZipFile(msapr) as z:
            names = z.namelist()
            check('msapr-header.json' in names and 'msapp/Properties.json' in names and not any(n.startswith('msapp/Src/') for n in names),
                  'msapr carries every non-Src entry under msapp/')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    if '--self-test' in sys.argv[1:]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('unpack'); p.add_argument('msapp'); p.add_argument('dir')
    p = sub.add_parser('lint'); p.add_argument('dir')
    p = sub.add_parser('pack'); p.add_argument('dir'); p.add_argument('out'); p.add_argument('--skip-lint', action='store_true')
    p = sub.add_parser('stamp'); p.add_argument('base'); p.add_argument('src'); p.add_argument('out')
    p.add_argument('--allow-errors', action='store_true'); p.add_argument('--skip-lint', action='store_true')
    p = sub.add_parser('msapr'); p.add_argument('base'); p.add_argument('out'); p.add_argument('--allow-errors', action='store_true')
    p = sub.add_parser('info'); p.add_argument('msapp')
    a = ap.parse_args()
    if a.cmd == 'unpack':
        return cmd_unpack(a.msapp, a.dir) or 0
    if a.cmd == 'lint':
        return cmd_lint(a.dir)
    if a.cmd == 'pack':
        return cmd_pack(a.dir, a.out, a.skip_lint)
    if a.cmd == 'stamp':
        return cmd_stamp(a.base, a.src, a.out, a.allow_errors, a.skip_lint)
    if a.cmd == 'msapr':
        return cmd_msapr(a.base, a.out, a.allow_errors)
    return cmd_info(a.msapp)


if __name__ == '__main__':
    sys.exit(main())
