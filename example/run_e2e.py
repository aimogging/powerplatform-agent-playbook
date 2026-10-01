#!/usr/bin/env python3
"""FIRST-RUN CHECK: the whole lifecycle on the Contoso Help Desk demo in YOUR dev/test tenant, then verified cleanup.

    python -m devtenant doctor                                   # (from tools/) read-only; fix every FAIL first
    python example/run_e2e.py --template-msapp <any Studio download>.msapp
    python example/run_e2e.py --from drive-app                   # resume at a stage (ids come from the state file)
    python example/run_e2e.py --keep                             # leave the demo in the tenant (no cleanup)
    python example/run_e2e.py --only cleanup                     # just remove what an earlier --keep run left

Stages (each prints PASS/FAIL/SKIP and the tier it proves; the run stops at the first FAIL and says how to resume):

  preflight     read-only: config filled, a token per API, connections exist           (read-only)
  provision     list + columns via SharePoint REST, then a re-audit that must say "nothing to do"
  build         example/build.py with YOUR site/operator values -> flow packages, flowcheck + canvas-lint
  deploy        both flows through the Flow API (update in place by name), started
  bind          app document bound to the live list + flow (schema + signature), imported, references written
  publish       publish, then the player's runtime package must be Ready with 0 NULL rules
  drive-flows   Submit through an Http twin -> {"status":"ok"}; Notify triages the row exactly once
  drive-app     the published app in a real browser: fill, Submit, the new row appears in the gallery
  verify        run history: the app's Submit run Succeeded, one Notify run per ticket, no re-fire, rows correct
  handoff       the DELIVERY build of the SAME sources with target values; leak-checked; differs only in tokens
  cleanup       delete everything the demo created (rows, twin, flows, app, list + recycle bin) and verify it is gone

Everything is named for the Contoso demo; nothing else in the tenant is touched. Notification mail goes only to
operatorEmail. The app stages need a control-template source: any .msapp saved by Studio in this tenant that uses a
Text label, Text input, Button, Rectangle, Vertical gallery and the modern Drop down (pass several files if one app
lacks some). Without it the app stages SKIP and the rest still runs.

State (ids, stage results) lives in ~/.pp-playbook/e2e/contoso-state.json; logs and screenshots in ~/.pp-playbook/e2e/.
Both hold tenant values: they stay outside the repo.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, 'tools')
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

WORK = os.path.join(os.path.expanduser('~'), '.pp-playbook', 'e2e')
STATE = os.path.join(WORK, 'contoso-state.json')
APP_NAME = 'Contoso Help Desk'
LIST = 'ContosoHelpDeskTickets'
SUBMIT, NOTIFY, PROVISION = 'ContosoHelpDeskSubmitTicket', 'ContosoHelpDeskNotifyNewTicket', 'ContosoHelpDeskProvisionList'
TWIN = SUBMIT + ' [http twin]'
TEST_PREFIXES = ('E2E-', '[fixture]')
SCHEMA = os.path.join(HERE, 'sharepoint', 'helpdesk.schema.json')
SRC = os.path.join(HERE, 'canvas', 'Src')
# pa.yaml Control -> the Studio template name that must exist in the package (C-19)
TEMPLATE_FOR = {'Label': 'label', 'Classic/TextInput': 'text', 'Classic/Button': 'button', 'Rectangle': 'rectangle',
                'Gallery': 'gallery', 'ModernDropdown': 'modernDropdown'}

STAGES = [
    ('preflight', 'read-only'), ('provision', 'dev-tenant proven'), ('build', 'offline-checked'),
    ('deploy', 'dev-tenant proven'), ('bind', 'dev-tenant proven'), ('publish', 'dev-tenant proven'),
    ('drive-flows', 'dev-tenant proven'), ('drive-app', 'dev-tenant proven'), ('verify', 'dev-tenant proven'),
    ('handoff', 'offline-checked'), ('cleanup', 'dev-tenant proven'),
]
NAMES = [s for s, _ in STAGES]


class Skip(Exception):
    pass


class Fail(Exception):
    def __init__(self, what, fix=''):
        Exception.__init__(self, what)
        self.fix = fix


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load_state():
    if os.path.isfile(STATE):
        return json.load(open(STATE, encoding='utf-8'))
    return {'ids': {}, 'stages': {}, 'created': {}}


def save_state(st):
    os.makedirs(WORK, exist_ok=True)
    with open(STATE, 'w', encoding='utf-8') as fh:
        json.dump(st, fh, indent=1)


class Run(object):
    def __init__(self, args):
        self.args = args
        self.st = load_state()
        from devtenant import auth as authmod, config as configmod, http as httpmod
        self.cfg = configmod.load(args.config)
        self.http = httpmod.Http(log=self.log_quiet)
        self.auth = authmod.Auth(self.cfg, self.http, out=print)
        self._sp = self._fc = self._pa = None
        self.tag = self.st.get('tag') or ('E2E-' + time.strftime('%Y%m%d%H%M'))
        self.st['tag'] = self.tag
        self.dist = os.path.join(WORK, 'dist')

    # ------------------------------------------------------------------ plumbing
    def log_quiet(self, *a):
        with open(os.path.join(WORK, 'http.log'), 'a', encoding='utf-8') as fh:
            fh.write(' '.join(str(x) for x in a)[:2000] + '\n')

    @property
    def sp(self):
        if self._sp is None:
            from devtenant.sharepoint import SharePoint
            self._sp = SharePoint(self.cfg, self.auth, self.http)
        return self._sp

    @property
    def fc(self):
        if self._fc is None:
            from devtenant.flows import FlowClient
            self._fc = FlowClient(self.cfg, self.auth, self.http)
        return self._fc

    @property
    def pa(self):
        if self._pa is None:
            from devtenant.powerapps import PowerAppsClient
            self._pa = PowerAppsClient(self.cfg, self.auth, self.http, log=self.log_quiet)
        return self._pa

    def ids(self):
        return self.st['ids']

    def flow_exact(self, name):
        return self.fc.find('=' + name)

    def app_exact(self):
        hits = [a for a in self.pa.apps() if (a.get('properties') or {}).get('displayName') == APP_NAME]
        if len(hits) > 1:
            pinned = self.ids().get('app')
            hits = [a for a in hits if a['name'] == pinned] or hits
        return hits[0] if hits else None

    # ------------------------------------------------------------------ stages
    def s_preflight(self):
        missing = [k for k in ('tenantId', 'environmentId', 'siteUrl', 'operatorEmail') if not self.cfg.get(k) or '<' in str(self.cfg.get(k))]
        if missing:
            raise Fail('config/environment.json still has placeholders: %s' % ', '.join(missing), 'fill them in, then rerun')
        for key in ('sharepoint', 'flow', 'powerApps', 'apihub'):
            try:
                self.auth.token(key, interactive=False)
            except (SystemExit, Exception) as ex:
                raise Fail('no usable %s sign-in (%s)' % (key, str(ex)[:160]), 'cd tools && python -m devtenant login %s' % key)
        conns = self.pa.connections()
        for api, label in (('shared_sharepointonline', 'SharePoint'), ('shared_office365', 'Office 365 Outlook')):
            if not conns.get(api):
                raise Fail('no %s connection in the environment' % label,
                           'make.powerautomate.com -> Data -> Connections -> New connection -> %s (the API cannot create one)' % label)
        return 'config filled; sharepoint/flow/powerApps/apihub tokens; SharePoint + Outlook connections exist'

    def s_provision(self):
        schema = json.load(open(SCHEMA, encoding='utf-8'))
        spec = schema['lists'][0]
        existed = self.sp.find_list(spec['title']) is not None
        row, actions = self.sp.plan_list(spec)
        if actions:
            self.sp.apply_list(spec, actions)
        if not existed:
            self.st['created']['list'] = True
        row, again = self.sp.plan_list(spec)
        if again:
            raise Fail('re-audit still wants: %s' % again, 'read the field definitions in %s' % SCHEMA)
        return '%s: %d action(s) applied, re-audit clean%s' % (spec['title'], len(actions), '' if existed else ' (list created by this run)')

    def s_build(self):
        import build as example_build
        cfg_path = self.args.config or os.path.join(REPO, 'config', 'environment.json')
        outs = example_build.build(self.dist, cfg_path, quiet=True)
        for name in (SUBMIT, NOTIFY):
            text = open(os.path.join(self.dist, 'src', name, 'definition.json'), encoding='utf-8').read()
            if '__SITE_URL__' in text or '__OPERATOR_EMAIL__' in text or 'contoso.sharepoint.com' in text:
                raise Fail('%s was not built with this tenant\'s values' % name, 'check siteUrl/operatorEmail in the config')
        return '%d package(s) built and flowchecked; canvas-lint clean' % len(outs)

    def s_deploy(self):
        out = []
        for name in (SUBMIT, NOTIFY):
            before = self.flow_exact(name)
            r = self.fc.deploy(os.path.join(self.dist, 'src', name), name, apply=True, start=True)
            if r.get('state') != 'Started':
                raise Fail('%s deployed but state=%s' % (name, r.get('state')), 'open it in make.powerautomate.com and read the error')
            self.ids()[name] = r['id']
            if before is None:
                self.st['created'][name] = True
            out.append('%s %s (%s)' % (name, 'updated in place' if before else 'created', r['state']))
        return '; '.join(out)

    def template_sources(self):
        srcs = list(self.args.template_msapp or [])
        if not srcs:
            raise Skip('no --template-msapp given: the app stages need one Studio-saved .msapp from this tenant '
                       '(see the header of example/run_e2e.py)')
        for s in srcs:
            if not os.path.isfile(s):
                raise Fail('template file not found: %s' % s)
        return srcs

    def s_bind(self):
        from devtenant import canvasdoc
        from devtenant.powerapps import deploy_app
        srcs = self.template_sources()
        os.makedirs(WORK, exist_ok=True)
        shell = os.path.join(WORK, 'contoso-dev.msapp')
        doc = canvasdoc.new_shell(srcs[0], APP_NAME, shell, srcs[1:])
        have = {t.get('Name') for t in doc.json('References/Templates.json').get('UsedTemplates', [])}
        used = set()
        for fn in os.listdir(SRC):
            used.update(re.findall(r'Control:\s*([A-Za-z/]+)@', open(os.path.join(SRC, fn), encoding='utf-8').read()))
        lacking = sorted(c for c in used if TEMPLATE_FOR.get(c) and TEMPLATE_FOR[c] not in have)
        if lacking:
            raise Fail('the template .msapp(s) lack control templates for: %s' % ', '.join(lacking),
                       'add one of each to any app in Studio, Save, Download a copy, pass it as another --template-msapp')
        lst = self.sp.find_list(LIST)
        conns = self.pa.connections().get('shared_sharepointonline') or []
        if not conns:
            raise Fail('no SharePoint connection', 'create one in the maker portal')
        canvasdoc.add_sharepoint_source(doc, LIST, self.cfg['siteUrl'], lst['Id'], conns[0], self.pa.connector('shared_sharepointonline'))
        flow_id = self.ids().get(SUBMIT) or self.flow_exact(SUBMIT)['name']
        status, wadl = self.fc.list_wadl(flow_id)
        if status != 200:
            raise Fail('listWadl for %s answered HTTP %s' % (SUBMIT, status))
        canvasdoc.add_flow_source(doc, SUBMIT, flow_id, wadl)
        canvasdoc.put_src(doc, SRC)
        doc.save(shell)
        self.st['srcHash'] = src_hash(SRC)
        before = self.app_exact()
        r = deploy_app(self.pa, self.fc, shell, APP_NAME, apply=True, publish=True,
                       name=(before or {}).get('name', ''), allow_create=before is None, work_dir=WORK, log=self.log_quiet)
        self.ids()['app'] = r['appId']
        if before is None:
            self.st['created']['app'] = True
        warn = [l for l in r.get('lines', []) if l.startswith('WARN')]
        return 'app %s (%s); references verified live%s' % (
            'created' if before is None else 'updated in place', r['appId'][:8] + '...',
            ('; ' + '; '.join(w[:120] for w in warn)) if warn else '')

    def s_publish(self):
        from devtenant import canvasdoc, runtime_rules
        aid = self.ids().get('app')
        if not aid:
            raise Skip('no app deployed (bind skipped)')
        launch = self.pa.launch(aid)
        det = launch.get('listAppPackageOperationDetails') or {}
        if det.get('packageStatus') != 'Ready':
            raise Fail('runtime package %s %s' % (det.get('packageStatus'), det.get('error') or ''),
                       'reference/platform-traps.md D-22/C-10 (client version) and C-05 (AccessibleLabel on a Classic button)')
        doc_path = os.path.join(WORK, 'published.msapp')
        formulas = runtime_rules.document_formulas(canvasdoc.Msapp(self.pa.download_document(aid, doc_path)))
        nulls = runtime_rules.scan(runtime_rules.fetch_package_js(self.http, launch), formulas)
        if nulls:
            raise Fail('%d NULL rule(s): %s' % (len(nulls), ', '.join('%s.%s' % (c, p) for c, p, _s in nulls)),
                       'those formulas compile to nothing -- reference/platform-traps.md C-01 (UTCNow), C-02 (missing column), C-46 (untyped variable)')
        return 'runtime package Ready, 0 NULL rules'

    def s_drive_flows(self):
        flow_id = self.ids().get(SUBMIT) or self.flow_exact(SUBMIT)['name']
        twin = self.fc.http_twin(flow_id, TWIN)
        self.ids()['twin'] = twin
        self.st['created']['twin'] = True
        subject = '%s twin %s' % (self.tag, time.strftime('%H%M%S'))
        self.st['driveFlowsStart'] = now_iso()
        payload = {'payload': json.dumps({'subject': subject, 'details': 'Created by run_e2e through the Http twin.',
                                          'category': 'Hardware', 'priority': 'High'})}
        status, body = self.fc.invoke(self.fc.callback_url(twin), payload, {'x-ms-user-email': self.cfg['operatorEmail']})
        result = json.loads(body.get('result_json', '{}')) if isinstance(body, dict) else {}
        if status != 200 or result.get('status') != 'ok':
            raise Fail('twin answered HTTP %s %s' % (status, str(body)[:300]), 'python -m devtenant flow-runs "=%s"' % TWIN)
        item_id = result['id']
        self.st.setdefault('tickets', {})['twin'] = {'id': item_id, 'subject': subject}
        row = self.wait_row(item_id, lambda r: (r.get('Status') or '') == 'Triaged', 180)
        if row.get('RequesterEmail') != self.cfg['operatorEmail'].lower():
            raise Fail('RequesterEmail %r, expected the lower-cased caller' % row.get('RequesterEmail'))
        return 'twin -> {"status":"ok","id":%s}; row created with Status New, then Triaged by %s' % (item_id, NOTIFY)

    def wait_row(self, item_id, pred, timeout):
        t0, row = time.time(), None
        while time.time() - t0 < timeout:
            rows = self.sp.items(LIST, select='Id,Title,Status,RequesterEmail,TriagedAt', filt='Id eq %d' % int(item_id))
            row = rows[0] if rows else None
            if row and pred(row):
                return row
            time.sleep(10)
        raise Fail('row %s never reached the expected state (last: %s)' % (item_id, row),
                   'python -m devtenant flow-runs "=%s" (the list trigger polls once a minute)' % NOTIFY)

    def s_drive_app(self):
        from devtenant import appdriver
        aid = self.ids().get('app')
        if not aid:
            raise Skip('no app deployed (bind skipped)')
        try:
            import playwright  # noqa: F401
        except ImportError:
            raise Fail('Playwright is not installed', 'pip install playwright && playwright install msedge')
        subject = '%s app %s' % (self.tag, time.strftime('%H%M%S'))
        self.st['driveAppStart'] = now_iso()
        p, ctx = appdriver.browser(self.cfg)
        try:
            app = appdriver.App(ctx, appdriver.play_url(self.cfg, aid))
            deadline = time.time() + 300
            while 'login.microsoftonline' in app.page.url and time.time() < deadline:
                print('      sign in in the browser window (first run only; the profile remembers it)')
                time.sleep(10)
            app.ready('txtSubject', timeout=180)
            app.fill('txtSubject', subject)
            app.fill('txtDetails', 'Created by run_e2e through the published app.')
            app.click('btnSubmit', settle=2)
            t0 = time.time()
            while time.time() - t0 < 120:
                if app.frame.locator('[data-control-name="lblRowSubject"]', has_text=subject).count() > 0:
                    break
                time.sleep(2)
            else:
                shot = app.shot(os.path.join(WORK, 'drive-app-fail.png'))
                raise Fail('the new ticket never appeared in the gallery (screenshot %s)' % shot,
                           'python -m devtenant flow-runs "=%s"' % SUBMIT)
            app.shot(os.path.join(WORK, 'drive-app.png'))
        finally:
            ctx.close()
            p.stop()
        rows = self.sp.items(LIST, select='Id,Title,Status,RequesterEmail', filt="Title eq '%s'" % subject.replace("'", "''"))
        if len(rows) != 1:
            raise Fail('expected exactly one row titled %r, found %d' % (subject, len(rows)))
        self.st.setdefault('tickets', {})['app'] = {'id': rows[0]['Id'], 'subject': subject}
        self.wait_row(rows[0]['Id'], lambda r: (r.get('Status') or '') == 'Triaged', 180)
        return 'browser: filled + Submit -> gallery shows the row; row %s Triaged (screenshot in %s)' % (rows[0]['Id'], WORK)

    def s_verify(self):
        tickets = self.st.get('tickets') or {}
        if not tickets:
            raise Skip('no tickets were created')
        window = int(self.args.refire_window)
        print('      waiting %ds so a re-fire of the list trigger would show up...' % window)
        time.sleep(window)
        since = self.st.get('driveFlowsStart') or self.st.get('driveAppStart')
        notify_id = self.ids().get(NOTIFY) or self.flow_exact(NOTIFY)['name']
        runs = [r for r in self.fc.runs(notify_id, 50) if (r.get('properties') or {}).get('startTime', '') >= since]
        bad = [r['name'] for r in runs if (r.get('properties') or {}).get('status') != 'Succeeded']
        if bad:
            raise Fail('%s run(s) not Succeeded: %s' % (NOTIFY, bad), 'python -m devtenant flow-run "=%s" %s' % (NOTIFY, bad[0]))
        if len(runs) != len(tickets):
            raise Fail('%s ran %d time(s) for %d ticket(s) (re-fire or missed trigger)' % (NOTIFY, len(runs), len(tickets)),
                       'reference/platform-traps.md F-14 (a flow\'s own write re-firing its trigger)')
        detail = '%s: %d run(s) for %d ticket(s), all Succeeded, no re-fire in %ds' % (NOTIFY, len(runs), len(tickets), window)
        if 'app' in tickets:
            submit_id = self.ids().get(SUBMIT) or self.flow_exact(SUBMIT)['name']
            sruns = [r for r in self.fc.runs(submit_id, 20) if (r.get('properties') or {}).get('startTime', '') >= self.st['driveAppStart']]
            if not sruns or any((r.get('properties') or {}).get('status') != 'Succeeded' for r in sruns):
                raise Fail('the app-called %s run is missing or failed' % SUBMIT, 'python -m devtenant flow-runs "=%s"' % SUBMIT)
            acts = self.fc.run_actions(submit_id, sruns[0]['name'])
            resp = [a for a in acts if a['name'] == 'Respond_Ok' and (a.get('properties') or {}).get('status') == 'Succeeded']
            if not resp:
                raise Fail('the app-called run did not answer through Respond_Ok')
            detail += '; app-called %s run Succeeded via Respond_Ok' % SUBMIT
        for kind, t in tickets.items():
            row = self.sp.items(LIST, select='Id,Title,Status,RequesterEmail', filt='Id eq %d' % int(t['id']))[0]
            if row['Status'] != 'Triaged' or row['RequesterEmail'] != self.cfg['operatorEmail'].lower():
                raise Fail('row %s (%s) is %s / %s' % (t['id'], kind, row['Status'], row['RequesterEmail']))
        return detail + '; rows Triaged with the lower-cased caller'

    def s_handoff(self):
        import build as example_build
        dev_cfg = self.args.config or os.path.join(REPO, 'config', 'environment.json')
        target = {'siteUrl': 'https://contoso.sharepoint.com/sites/HelpDesk', 'operatorEmail': 'helpdesk-owner@contoso.com'}
        out = os.path.join(WORK, 'handoff')
        with tempfile.TemporaryDirectory() as d:
            tcfg = os.path.join(d, 'target.json')
            json.dump(target, open(tcfg, 'w', encoding='utf-8'))
            example_build.build(out, tcfg, quiet=True, handoff=dev_cfg)
        dev = json.load(open(dev_cfg, encoding='utf-8-sig'))
        for name in (SUBMIT, NOTIFY):
            a = open(os.path.join(self.dist, 'src', name, 'definition.json'), encoding='utf-8').read()
            b = open(os.path.join(out, 'src', name, 'definition.json'), encoding='utf-8').read()
            a = a.replace(dev['siteUrl'], '__SITE_URL__').replace(dev['operatorEmail'], '__OPERATOR_EMAIL__')
            b = b.replace(target['siteUrl'], '__SITE_URL__').replace(target['operatorEmail'], '__OPERATOR_EMAIL__')
            if a != b:
                raise Fail('%s: the handoff build differs from the tested build beyond the declared tokens' % name)
        detail = 'flow packages rebuilt with target values, leak-checked, identical to the tested build except the 2 tokens'
        if self.st.get('srcHash'):
            if src_hash(SRC) != self.st['srcHash']:
                raise Fail('example/canvas/Src changed since it was tested -- rerun from bind', 'python example/run_e2e.py --from bind')
            detail += '; canvas Src byte-identical to the tested app'
            shell = os.path.join(WORK, 'contoso-dev.msapp')
            if os.path.isfile(shell):
                from importlib import util
                spec = util.spec_from_file_location('msapp_tool', os.path.join(TOOLS, 'msapp-tool.py'))
                mt = util.module_from_spec(spec)
                spec.loader.exec_module(mt)
                print('      negative test: the dev shell must be REFUSED as a handoff base (LEAK lines below are expected)')
                try:
                    mt.cmd_stamp(shell, SRC, os.path.join(out, 'must-not-exist.msapp'), handoff=dev_cfg)
                    raise Fail('stamping into the DEV shell with --handoff was NOT refused -- the leak gate is broken')
                except SystemExit:
                    detail += '; stamping into the dev shell with --handoff is refused (gate works)'
        return detail + ' -> %s' % out

    def s_cleanup(self):
        if self.args.keep:
            raise Skip('--keep: the demo stays in the tenant; remove it later with --only cleanup')
        done = []
        lst = self.sp.find_list(LIST)
        if lst:
            rows = self.sp.items(LIST, select='Id,Title')
            only_tests = all(str(r.get('Title') or '').startswith(TEST_PREFIXES) for r in rows)
            if self.st['created'].get('list') or only_tests:
                done += self.sp.delete_list(LIST)
            else:
                n = 0
                for r in rows:
                    if str(r.get('Title') or '').startswith(TEST_PREFIXES):
                        self.sp.delete_item(LIST, r['Id'])
                        n += 1
                done.append('%d test row(s) deleted; list kept (it holds rows this run did not create)' % n)
        else:
            done += self.sp.delete_list(LIST)   # purge a recycled copy left by an interrupted cleanup
        for name in (TWIN, SUBMIT, NOTIFY, PROVISION):
            f = self.flow_exact(name)
            if f:
                self.fc.delete(f['name'])
                done.append('flow %s deleted' % name)
        app = self.app_exact()
        if app:
            self.pa.delete_app(app['name'])
            done.append('app deleted')
        # verify
        left = []
        time.sleep(5)
        for name in (TWIN, SUBMIT, NOTIFY, PROVISION):
            if self.flow_exact(name):
                left.append('flow ' + name)
        if self.app_exact():
            left.append('app ' + APP_NAME)
        if self.sp.find_list(LIST) and (self.st['created'].get('list') or not self.sp.items(LIST, select='Id')):
            left.append('list ' + LIST)
        if not self.sp.find_list(LIST) and self.sp.recycle_bin(LIST):
            left.append('recycle-bin entry ' + LIST)
        if left:
            raise Fail('still present after cleanup: %s' % ', '.join(left), 'rerun: python example/run_e2e.py --only cleanup')
        self.st['ids'], self.st['created'], self.st['tickets'] = {}, {}, {}
        self.st.pop('tag', None)
        return '; '.join(done) + '; verified: none of them exists any more' if done else 'nothing to remove; verified clean'


def src_hash(folder):
    h = hashlib.sha256()
    for fn in sorted(os.listdir(folder)):
        h.update(fn.encode())
        h.update(open(os.path.join(folder, fn), 'rb').read())
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description='Contoso demo end to end in the dev tenant, then verified cleanup.')
    ap.add_argument('--template-msapp', action='append', help='a Studio-saved .msapp from this tenant (control templates); repeatable')
    ap.add_argument('--config', default=None, help='dev config (default config/environment.json)')
    ap.add_argument('--from', dest='start', choices=NAMES, help='resume at this stage')
    ap.add_argument('--only', choices=NAMES, help='run just this stage')
    ap.add_argument('--keep', action='store_true', help='skip cleanup (leave the demo in the tenant)')
    ap.add_argument('--refire-window', default=120, type=int, help='seconds to wait for a trigger re-fire in verify (default 120)')
    ap.add_argument('--debug', action='store_true', help='print tracebacks')
    args = ap.parse_args(argv)
    os.makedirs(WORK, exist_ok=True)
    try:
        run = Run(args)
    except SystemExit as ex:
        print('FAIL  preflight: %s' % ex)
        return 1
    todo = [args.only] if args.only else NAMES[NAMES.index(args.start):] if args.start else NAMES
    results = []
    failed = False
    for name, tier in STAGES:
        if name not in todo:
            continue
        if failed and name != 'cleanup':
            results.append((name, 'NOT RUN', tier, 'an earlier stage failed'))
            continue
        if failed and name == 'cleanup':
            results.append((name, 'NOT RUN', tier, 'kept for diagnosis; remove with --only cleanup'))
            continue
        print('== %s' % name)
        t0 = time.time()
        try:
            detail = getattr(run, 's_' + name.replace('-', '_'))()
            status = 'PASS'
        except Skip as ex:
            status, detail = 'SKIP', str(ex)
        except Fail as ex:
            status, detail = 'FAIL', str(ex) + (('\n        fix: ' + ex.fix) if ex.fix else '')
            failed = True
        except (SystemExit, Exception) as ex:
            status, detail = 'FAIL', '%s: %s' % (type(ex).__name__, str(ex)[:400])
            failed = True
            if args.debug:
                traceback.print_exc()
        run.st['stages'][name] = {'status': status, 'detail': detail, 'at': now_iso()}
        save_state(run.st)
        print('   %s (%ds) %s' % (status, time.time() - t0, detail))
        results.append((name, status, tier, detail))
        if status == 'FAIL':
            print('   resume after fixing: python example/run_e2e.py --from %s%s' % (
                name, ''.join(' --template-msapp "%s"' % t for t in (args.template_msapp or []))))
    print('\n%-12s %-8s %-18s %s' % ('stage', 'result', 'tier', 'evidence'))
    for name, status, tier, detail in results:
        print('%-12s %-8s %-18s %s' % (name, status, tier, detail.split('\n')[0][:160]))
    return 1 if any(r[1] == 'FAIL' for r in results) else 0


if __name__ == '__main__':
    sys.exit(main())
