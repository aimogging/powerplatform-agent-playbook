#!/usr/bin/env python3
"""deploy.py -- plan / confirm / apply a whole DEV-TENANT deployment from one manifest: SharePoint provisioning,
cloud flows, canvas apps, publish, and the runtime gate. Stops at the first failure and prints the exact resume command.

    python tools/deploy.py --manifest example/deploy.manifest.json               # PRE, PLAN, confirm, APPLY
    python tools/deploy.py --manifest ... --plan-only                           # read-only
    python tools/deploy.py --manifest ... --resume-from S1-APPS                 # after a stop (plan runs again)
    python tools/deploy.py --manifest ... --stop-after S1-FLOWS --skip P1
    python tools/deploy.py --manifest ... --flows '=HelpDeskSubmitTicket' --apps none
    python tools/deploy.py --manifest ... --yes                                 # answer the confirmation (unattended)
    python tools/deploy.py --self-test                                          # offline: step table, rules, mutations

Phases
  PRE    offline and free: manifest rules (below), flowcheck on every flow source, canvas-lint on every app's Src,
         premium-connector audit of every .msapp. Any ERROR stops here.
  PLAN   read-only against the tenant: what each provisioner/flow/app step WOULD do; ERROR lines refuse the apply.
  APPLY  the step table in order: P<n> provisioners, then per stage <S>-FLOWS, <S>-APPS (draft + references),
         <S>-PUBLISH, then GATE (launch packageStatus + NULL-rule scan of every app).

Manifest rules (each one encodes an incident):
  * every flow an app calls is deployed in the SAME or an EARLIER stage than the app -- apps bind flows by GUID and
    the embedded Run() signature is refreshed from the live flow at app-deploy time;
  * flows are UPDATED in place (GUID kept); a list-triggered flow is "mustExist": true once it is live, so a name miss
    can never CREATE a second, double-firing copy;
  * app display names contain none of . \\ / : * ? " < > |;
  * a name appears once; stage ids are unique; selectors: 'Name*' wildcard, substring, '=Exact', 'none'.
The deploy log goes to ~/.pp-playbook/logs (outside the repo: it can contain tenant ids).
"""
import argparse
import datetime
import fnmatch
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
BAD_APP_NAME = re.compile(r'[.\\/:*?"<>|]')


def _load(fname, mod):
    spec = importlib.util.spec_from_file_location(mod, os.path.join(HERE, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def rp(path):
    return path if os.path.isabs(path) else os.path.join(REPO, path)


# ------------------------------------------------------------------------------------------------ selection
def select(entries, selector, kind):
    if not selector:
        return entries
    if selector.strip().lower() == 'none':
        return []
    keep = []
    for pat in [p.strip() for p in selector.split(',') if p.strip()]:
        if pat.startswith('='):
            hit = [e for e in entries if e['displayName'] == pat[1:]]
        elif any(ch in pat for ch in '*?['):
            hit = [e for e in entries if fnmatch.fnmatch(e['displayName'].lower(), pat.lower())]
        else:
            hit = [e for e in entries if pat.lower() in e['displayName'].lower()]
        if not hit:
            raise SystemExit('--%s %r matches nothing; manifest has: %s' % (kind, pat, ', '.join(e['displayName'] for e in entries)))
        keep += [h for h in hit if h not in keep]
    return keep


# ------------------------------------------------------------------------------------------------ steps
def steps_for(manifest, flows_sel='', apps_sel='', publish=True):
    flows = select(manifest.get('flows') or [], flows_sel, 'flows')
    apps = select(manifest.get('apps') or [], apps_sel, 'apps')
    stages = manifest.get('stages') or [{'id': 'S1', 'flows': [f['displayName'] for f in manifest.get('flows') or []],
                                         'apps': [a['displayName'] for a in manifest.get('apps') or []]}]
    steps = [{'id': p['id'], 'kind': 'provision', 'items': [p]} for p in manifest.get('provision') or []]
    for st in stages:
        sf = [f for f in flows if f['displayName'] in st.get('flows', [])]
        sa = [a for a in apps if a['displayName'] in st.get('apps', [])]
        if sf:
            steps.append({'id': st['id'] + '-FLOWS', 'kind': 'flows', 'items': sf})
        if sa:
            steps.append({'id': st['id'] + '-APPS', 'kind': 'apps', 'items': sa})
            if publish:
                steps.append({'id': st['id'] + '-PUBLISH', 'kind': 'publish', 'items': sa})
    if apps and (manifest.get('gates') or {}).get('runtimeRules', True) and publish:
        steps.append({'id': 'GATE', 'kind': 'gate', 'items': apps})
    return steps


def manifest_errors(manifest):
    errs = []
    flows = manifest.get('flows') or []
    apps = manifest.get('apps') or []
    for kind, entries in (('flow', flows), ('app', apps)):
        names = [e['displayName'] for e in entries]
        for n in set(names):
            if names.count(n) > 1:
                errs.append('%s %r listed %d times' % (kind, n, names.count(n)))
    for a in apps:
        if BAD_APP_NAME.search(a['displayName']):
            errs.append('app %r: display name contains . \\ / : * ? " < > | (the service refuses it on every later full PUT)' % a['displayName'])
    stages = manifest.get('stages') or [{'id': 'S1', 'flows': [f['displayName'] for f in flows], 'apps': [a['displayName'] for a in apps]}]
    ids = [s['id'] for s in stages]
    for i in set(ids):
        if ids.count(i) > 1:
            errs.append('stage id %r used twice' % i)
    stage_of_flow = {}
    for idx, s in enumerate(stages):
        for f in s.get('flows', []):
            stage_of_flow[f] = idx
        for a in s.get('apps', []):
            app = next((x for x in apps if x['displayName'] == a), None)
            if app is None:
                errs.append('stage %s names unknown app %r' % (s['id'], a))
                continue
            for need in app.get('requiredFlows') or []:
                if need not in stage_of_flow:
                    errs.append('app %r requires flow %r, which is not deployed in stage %s or earlier (apps bind flows by GUID; '
                                'deploy the flow first)' % (a, need, s['id']))
    listed = {f for s in stages for f in s.get('flows', [])}
    for f in flows:
        if f['displayName'] not in listed:
            errs.append('flow %r is in no stage' % f['displayName'])
    return errs


def offline_checks(manifest):
    """PRE: flowcheck, canvas-lint, premium audit. Returns (errors, warnings)."""
    errs, warns = list(manifest_errors(manifest)), []
    fc = _load('flowcheck.py', 'flowcheck')
    for f in manifest.get('flows') or []:
        src = rp(f.get('source') or '')
        if f.get('source') and os.path.isdir(src):
            raw, path = fc.load_path(src)
            c = fc.Checker(raw, path).run()
            errs += ['flowcheck %s: [%s] %s' % (f['displayName'], r, m) for r, m in c.errors]
            warns += ['flowcheck %s: [%s] %s' % (f['displayName'], r, m) for r, m in c.warnings]
    cl = _load('canvas-lint.py', 'canvas_lint')
    for a in manifest.get('apps') or []:
        if a.get('src') and os.path.isdir(rp(a['src'])):
            _files, issues = cl.lint([rp(a['src'])])
            errs += ['canvas-lint %s: %s:%d [%s] %s' % (a['displayName'], p, n, r, m) for s, r, p, n, m in issues if s == 'error']
            warns += ['canvas-lint %s: [%s] %s' % (a['displayName'], r, m) for s, r, p, n, m in issues if s != 'error']
        if a.get('msapp') and os.path.isfile(rp(a['msapp'])):
            from devtenant import canvasdoc
            for hit in canvasdoc.premium_references(canvasdoc.Msapp(rp(a['msapp'])).refs()):
                (errs if hit['dataSources'] else warns).append('premium %s: %s (%s)' % (
                    a['displayName'], hit['api'], 'bound as a data source' if hit['dataSources'] else 'flow dependency: will be unwired'))
    return errs, warns


# ------------------------------------------------------------------------------------------------ executor
class Live(object):
    """Real tenant calls (devtenant). Swapped for a fake in the self-test."""

    def __init__(self):
        from devtenant import auth, config, http
        from devtenant.flows import FlowClient
        from devtenant.powerapps import PowerAppsClient
        from devtenant.sharepoint import SharePoint
        self.cfg = config.load()
        h = http.Http()
        a = auth.Auth(self.cfg, h)
        self.fc, self.pa, self.sp = FlowClient(self.cfg, a, h), PowerAppsClient(self.cfg, a, h), SharePoint(self.cfg, a, h)
        self.http = h
        self.app_ids = {}
        self.app_refs = {}
        self.published = {}

    def plan(self, step):
        out = []
        if step['kind'] == 'provision':
            schema = json.load(open(rp(step['items'][0]['schema']), encoding='utf-8'))
            for spec in schema['lists']:
                _row, actions = self.sp.plan_list(spec)
                out += [('ERROR' if k == 'mangled' else 'PLAN', '%s %s: %s' % (k, spec['title'], n)) for k, n, _a in actions] or [('INFO', '%s up to date' % spec['title'])]
        elif step['kind'] == 'flows':
            flows = self.fc.list()
            cat = self.fc.connection_catalog(flows)
            for f in step['items']:
                try:
                    p = self.fc.deploy(rp(f.get('source') or f.get('package')), f['displayName'], apply=False, flows=flows, catalog=cat,
                                       must_exist=f.get('mustExist', False))
                    out.append(('PLAN', '%s: %s%s' % (f['displayName'], 'UPDATE ' + p['existing'] if p['existing'] else 'CREATE',
                                                      (' (rename from %r)' % p['renamedFrom']) if p['renamedFrom'] else '')))
                except SystemExit as ex:
                    out.append(('ERROR', str(ex)))
        elif step['kind'] == 'apps':
            apps = self.pa.apps()
            for a in step['items']:
                try:
                    t = self.pa.find(a['displayName'], apps, a.get('name', ''), a.get('owner', ''))
                    if t is None and not a.get('allowCreate'):
                        out.append(('ERROR', '%s: no live app and allowCreate is false' % a['displayName']))
                    else:
                        out.append(('PLAN', '%s: %s' % (a['displayName'], 'UPDATE ' + t['name'] if t else 'CREATE')))
                except SystemExit as ex:
                    out.append(('ERROR', str(ex)))
        return out

    def apply(self, step, publish=True):
        from devtenant.powerapps import deploy_app
        from devtenant import canvasdoc, runtime_rules
        if step['kind'] == 'provision':
            schema = json.load(open(rp(step['items'][0]['schema']), encoding='utf-8'))
            for spec in schema['lists']:
                _row, actions = self.sp.plan_list(spec)
                self.sp.apply_list(spec, actions)
        elif step['kind'] == 'flows':
            flows = self.fc.list()
            cat = self.fc.connection_catalog(flows)
            for f in step['items']:
                r = self.fc.deploy(rp(f.get('source') or f.get('package')), f['displayName'], apply=True, start=f.get('start', True),
                                   flows=flows, catalog=cat, must_exist=f.get('mustExist', False))
                print('RESULT flow %s -> %s (%s)' % (f['displayName'], r['id'], r.get('state')))
        elif step['kind'] == 'apps':
            for a in step['items']:
                target = self.pa.find(a['displayName'], None, a.get('name', ''), a.get('owner', ''))
                r = deploy_app(self.pa, self.fc, rp(a['msapp']), a['displayName'], apply=True, publish=target is None,
                               name=a.get('name', ''), owner=a.get('owner', ''), allow_create=a.get('allowCreate', False),
                               refresh_schemas=a.get('refreshTableSchemas', True), refresh_signatures=a.get('refreshFlowSignatures', True))
                for line in r.get('lines', []):
                    print(line)
                self.app_ids[a['displayName']] = r['appId']
                self.app_refs[a['displayName']] = r.get('references') or {}
                self.published[a['displayName']] = r['published']
                print('RESULT app %s -> %s (%s)' % (a['displayName'], r['appId'], 'created + published' if r['created'] else 'draft written'))
        elif step['kind'] == 'publish':
            from devtenant.powerapps import publish_and_verify
            for a in step['items']:
                if self.published.get(a['displayName']):
                    print('RESULT %s already published by its first (create) deploy' % a['displayName'])
                    continue
                aid = self.app_ids.get(a['displayName']) or self.pa.find(a['displayName'], None, a.get('name', ''), a.get('owner', ''))['name']
                for line in publish_and_verify(self.pa, aid, self.app_refs.get(a['displayName']) or {}):
                    print(line)
                print('RESULT published %s' % a['displayName'])
        elif step['kind'] == 'gate':
            bad = 0
            for a in step['items']:
                aid = self.app_ids.get(a['displayName']) or self.pa.find(a['displayName'], None, a.get('name', ''), a.get('owner', ''))['name']
                launch = self.pa.launch(aid)
                det = launch.get('listAppPackageOperationDetails') or {}
                if det.get('packageStatus') != 'Ready':
                    print('GATE %s packageStatus=%s %s' % (a['displayName'], det.get('packageStatus'), det.get('error') or ''))
                    bad += 1
                    continue
                path = os.path.join(os.path.expanduser('~'), '.pp-playbook', 'work', aid + '.live.msapp')
                os.makedirs(os.path.dirname(path), exist_ok=True)
                formulas = runtime_rules.document_formulas(canvasdoc.Msapp(self.pa.download_document(aid, path)))
                nulls = runtime_rules.scan(runtime_rules.fetch_package_js(self.http, launch), formulas)
                for c, p, s in nulls:
                    print('GATE %s NULL RULE %s.%s (%s)' % (a['displayName'], c, p, s))
                bad += 1 if nulls else 0
            if bad:
                raise SystemExit('%d app(s) failed the runtime gate' % bad)


# ------------------------------------------------------------------------------------------------ driver
def run(manifest, args, executor, confirm=input, out=print):
    steps = steps_for(manifest, args.flows, args.apps, not args.no_publish)
    ids = [s['id'] for s in steps]
    for sid in (args.skip.split(',') if args.skip else []) + ([args.resume_from] if args.resume_from else []) + ([args.stop_after] if args.stop_after else []):
        if sid and sid not in ids:
            raise SystemExit('unknown step %r; steps are: %s' % (sid, ' '.join(ids)))
    out('STEPS ' + ' '.join(ids))
    errs, warns = offline_checks(manifest)
    for w in warns:
        out('WARN  PRE ' + w)
    for e in errs:
        out('ERROR PRE ' + e)
    if errs:
        return 2
    skip = set(args.skip.split(',')) if args.skip else set()
    start = ids.index(args.resume_from) if args.resume_from else 0
    stop = ids.index(args.stop_after) if args.stop_after else len(ids) - 1
    todo = [s for i, s in enumerate(steps) if start <= i <= stop and s['id'] not in skip]
    findings = []
    for s in todo:
        for level, text in executor.plan(s):
            findings.append(level)
            out('%-5s %s %s' % (level, s['id'], text))
    if args.plan_only:
        return 1 if 'ERROR' in findings else 0
    if 'ERROR' in findings:
        out('REFUSED: the plan has ERROR lines; nothing was written')
        return 2
    if not args.yes and confirm('Apply %d step(s) [%s]? y/N ' % (len(todo), ' '.join(s['id'] for s in todo))).strip().lower() != 'y':
        out('not applied')
        return 1
    for s in todo:
        out('== %s' % s['id'])
        try:
            executor.apply(s, not args.no_publish)
        except (SystemExit, Exception) as ex:
            out('FAILED %s: %s' % (s['id'], ex))
            out('resume with: python tools/deploy.py --manifest %s --resume-from %s' % (args.manifest, s['id']))
            return 1
    if args.no_publish:
        out('NOTE drafts only: publish before anyone opens these apps in Studio (a draft opened in Studio loses its references)')
    out('DONE')
    return 0


class Tee(object):
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, 'a', encoding='utf-8')

    def __call__(self, line):
        print(line)
        self.fh.write(line + '\n')
        self.fh.flush()


def self_test():
    ok = True

    def check(cond, what):
        nonlocal ok
        print(('  ok   ' if cond else '  FAIL ') + what)
        ok = ok and cond

    m = {'flows': [{'displayName': 'FlowA'}, {'displayName': 'FlowB'}],
         'apps': [{'displayName': 'App One', 'requiredFlows': ['FlowA']}],
         'provision': [{'id': 'P1', 'schema': 'x'}],
         'stages': [{'id': 'S1', 'flows': ['FlowA'], 'apps': ['App One']}, {'id': 'S2', 'flows': ['FlowB'], 'apps': []}]}
    check([s['id'] for s in steps_for(m)] == ['P1', 'S1-FLOWS', 'S1-APPS', 'S1-PUBLISH', 'S2-FLOWS', 'GATE'], 'step table: provision, per-stage flows/apps/publish, gate')
    check([s['id'] for s in steps_for(m, publish=False)] == ['P1', 'S1-FLOWS', 'S1-APPS', 'S2-FLOWS'], 'no-publish drops publish and gate steps')
    check(manifest_errors(m) == [], 'valid manifest has no errors')
    bad = json.loads(json.dumps(m))
    bad['stages'] = [{'id': 'S1', 'flows': [], 'apps': ['App One']}, {'id': 'S2', 'flows': ['FlowA', 'FlowB'], 'apps': []}]
    check(any('requires flow' in e for e in manifest_errors(bad)), 'mutation: app staged before the flow it calls is an error')
    bad2 = json.loads(json.dumps(m))
    bad2['apps'][0]['displayName'] = 'App / One'
    bad2['stages'][0]['apps'] = ['App / One']
    check(any('display name' in e for e in manifest_errors(bad2)), 'mutation: a "/" in an app name is an error')
    bad3 = json.loads(json.dumps(m))
    bad3['flows'].append({'displayName': 'FlowA'})
    check(any('listed 2 times' in e for e in manifest_errors(bad3)), 'mutation: duplicate flow is an error')
    bad4 = json.loads(json.dumps(m))
    bad4['stages'][1]['flows'] = []
    check(any('in no stage' in e for e in manifest_errors(bad4)), 'mutation: an unstaged flow is an error')
    check([e['displayName'] for e in select(m['flows'], '=FlowA', 'flows')] == ['FlowA'], "selector '=Exact'")
    check([e['displayName'] for e in select(m['flows'], 'flow*', 'flows')] == ['FlowA', 'FlowB'], 'selector wildcard (case-insensitive)')
    check(select(m['flows'], 'none', 'flows') == [], "selector 'none'")
    try:
        select(m['flows'], 'Nope', 'flows')
        hit = False
    except SystemExit:
        hit = True
    check(hit, 'selector matching nothing is an error that lists the names')

    class Fake(object):
        def __init__(self, fail_on=None, plan_error=False):
            self.done, self.fail_on, self.plan_error = [], fail_on, plan_error

        def plan(self, s):
            return [('ERROR' if self.plan_error else 'PLAN', 'x')]

        def apply(self, s, publish):
            if s['id'] == self.fail_on:
                raise SystemExit('boom')
            self.done.append(s['id'])

    class A(object):
        manifest, flows, apps, no_publish, skip, resume_from, stop_after, plan_only, yes = 'm.json', '', '', False, '', '', '', False, True
    lines = []
    f = Fake()
    check(run(m, A(), f, out=lines.append) == 0 and f.done == ['P1', 'S1-FLOWS', 'S1-APPS', 'S1-PUBLISH', 'S2-FLOWS', 'GATE'], 'apply runs every step in order')
    f = Fake(fail_on='S1-APPS')
    lines = []
    rc = run(m, A(), f, out=lines.append)
    check(rc == 1 and f.done == ['P1', 'S1-FLOWS'] and any('--resume-from S1-APPS' in x for x in lines), 'first failure stops and prints the resume command')
    a = A()
    a.resume_from, a.stop_after, a.skip = 'S1-APPS', 'S2-FLOWS', 'S1-PUBLISH'
    f = Fake()
    run(m, a, f, out=lambda x: None)
    check(f.done == ['S1-APPS', 'S2-FLOWS'], 'resume-from / stop-after / skip select the window')
    a = A()
    a.plan_only = True
    f = Fake()
    check(run(m, a, f, out=lambda x: None) == 0 and f.done == [], 'plan-only writes nothing')
    f = Fake(plan_error=True)
    check(run(m, A(), f, out=lambda x: None) == 2 and f.done == [], 'an ERROR in the plan refuses the apply')
    a = A()
    a.yes = False
    f = Fake()
    check(run(m, a, f, confirm=lambda p: 'n', out=lambda x: None) == 1 and f.done == [], 'no confirmation, no apply')
    a = A()
    a.resume_from = 'S9'
    try:
        run(m, a, Fake(), out=lambda x: None)
        hit = False
    except SystemExit:
        hit = True
    check(hit, 'an unknown step id is refused')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--manifest')
    ap.add_argument('--plan-only', action='store_true')
    ap.add_argument('--resume-from', default='')
    ap.add_argument('--stop-after', default='')
    ap.add_argument('--skip', default='')
    ap.add_argument('--flows', default='')
    ap.add_argument('--apps', default='')
    ap.add_argument('--no-publish', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.manifest:
        ap.error('--manifest is required')
    manifest = json.load(open(rp(a.manifest), encoding='utf-8'))
    log = Tee(os.path.join(os.path.expanduser('~'), '.pp-playbook', 'logs',
                           'deploy-%s.log' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S')))
    return run(manifest, a, Live(), out=log)


if __name__ == '__main__':
    sys.exit(main())
