#!/usr/bin/env python3
"""leakcheck.py -- refuse a HANDOFF package that carries dev-tenant scaffolding.

The deliverable is the importable package. Dev-tenant deploys exist only to test it, so nothing that belongs to the
dev tenant may ride along into the handoff: its tenant/environment ids, its site host, its connection names, the
operator's test address, Http-trigger test twins, fixture tags.

    python tools/leakcheck.py <package.zip | app.msapp> [...] --dev-config config/environment.json
    python tools/leakcheck.py --self-test

Used by build-flow-package.py and msapp-tool.py when given --handoff <dev config>. Exit 1 on any hit.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import zipfile

TWIN_MARKERS = [re.compile(r'\[http twin\]', re.I), re.compile(r'\[fixture\]', re.I)]


def dev_markers(config_path):
    """Literal strings that identify the dev tenant, read from its (git-ignored) config."""
    cfg = json.load(open(config_path, encoding='utf-8-sig'))
    marks = set()
    for key in ('tenantId', 'environmentId', 'operatorEmail', 'dataverseUrl'):
        v = cfg.get(key)
        if isinstance(v, str) and v and '<' not in v:
            marks.add(v.lower())
            if key == 'environmentId' and v.lower().startswith('default-'):
                marks.add(v.lower()[8:])
    m = re.match(r'^https://([^/]+)', cfg.get('siteUrl') or '')
    if m and '<' not in m.group(1):
        marks.add(m.group(1).lower())
    for v in (cfg.get('connections') or {}).values():
        if isinstance(v, str) and v and '<' not in v:
            marks.add(v.lower())
    return {x for x in marks if len(x) >= 6}


def scan(path, markers):
    hits = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            data = z.read(n)
            if n.lower().endswith(('.msapp', '.zip')):
                with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as t:
                    t.write(data)
                try:
                    hits += ['%s!%s' % (n, h) for h in scan(t.name, markers)]
                finally:
                    os.unlink(t.name)
                continue
            text = data.decode('utf-8', errors='ignore').lower()
            for mk in markers:
                if mk in text:
                    hits.append('%s: dev-tenant value %r' % (n, mk))
            for rx in TWIN_MARKERS:
                if rx.search(text):
                    hits.append('%s: dev-test scaffolding marker %r' % (n, rx.pattern))
            if n.endswith('definition.json'):
                try:
                    doc = json.loads(data.decode('utf-8-sig'))
                    trig = (((doc.get('properties') or doc).get('definition') or {}).get('triggers') or {})
                    for tn, t in trig.items():
                        if isinstance(t, dict) and t.get('type') == 'Request' and t.get('kind') == 'Http':
                            hits.append('%s: trigger %s is an Http trigger (a test twin?) -- app-called flows ship with kind PowerApp' % (n, tn))
                except ValueError:
                    pass
    return hits


def check(path, dev_config):
    hits = scan(path, dev_markers(dev_config))
    for h in hits:
        print('LEAK %s' % h)
    return hits


def self_test():
    ok = True
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, 'env.json')
        host = 'devlab-' + 'fixture.' + 'sharepoint.com'          # assembled so the scrub gate never sees a tenant-shaped host
        site = 'https://' + host + '/sites/x'
        json.dump({'tenantId': 'zzdev-tenant-fixture', 'environmentId': 'Default-zzdev-tenant-fixture', 'siteUrl': site,
                   'operatorEmail': 'tester@devtenant.example', 'connections': {'shared_x': 'conn-dev-12345'}}, open(cfg, 'w'))
        clean = os.path.join(d, 'clean.zip')
        with zipfile.ZipFile(clean, 'w') as z:
            z.writestr('Microsoft.Flow/flows/a/definition.json', json.dumps({'properties': {'definition': {'triggers': {'manual': {'type': 'Request', 'kind': 'PowerApp'}}}}}))
        dirty = os.path.join(d, 'dirty.zip')
        with zipfile.ZipFile(dirty, 'w') as z:
            z.writestr('Microsoft.Flow/flows/a/definition.json', json.dumps({'properties': {'displayName': 'X [http twin]', 'definition': {
                'triggers': {'manual': {'type': 'Request', 'kind': 'Http'}},
                'actions': {'A': {'inputs': {'parameters': {'dataset': site}}}}}}}))
        hits = scan(dirty, dev_markers(cfg))
        ok = not scan(clean, dev_markers(cfg)) and any(host in h for h in hits) \
            and any('Http trigger' in h for h in hits) and any('twin' in h for h in hits)
    print(('  ok   ' if ok else '  FAIL ') + 'dev site host, Http twin trigger and twin marker caught; clean package passes')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    if '--self-test' in sys.argv[1:]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('packages', nargs='+')
    ap.add_argument('--dev-config', required=True)
    a = ap.parse_args()
    bad = sum(len(check(p, a.dev_config)) for p in a.packages)
    print('leakcheck: %s' % ('CLEAN' if not bad else '%d hit(s)' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
