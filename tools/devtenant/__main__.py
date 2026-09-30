"""python -m devtenant <command> ...   (run from the tools/ folder, or with tools/ on PYTHONPATH)

Dev/test-tenant commands for the agent. Reads config/environment.json. Anything that writes needs --apply;
without it the command prints its plan. Nothing here is meant for the production tenant.

  login <sharepoint|flow|graph|apihub|powerApps|dataverse>   device-code sign-in (once); later calls refresh silently
  whoami                                                     audience / user / expiry of each cached token (no secrets)
  sp-provision <schema.json> [--apply]                      idempotent lists + fields (audit, then apply)
  sp-seed <list> <rows.json> --tag TAG [--apply]            fixture rows, Title prefixed with TAG
  sp-cleanup <list> --tag TAG [--apply]                     delete ONLY rows whose Title starts with TAG
  flow-deploy <flow-dir|package.zip> [--name N] [--apply] [--no-start] [--must-exist]
  flow-runs <flow> [--top N]                                recent runs (flow = display name, '=Exact', or id)
  flow-run <flow> <runId>                                   failed actions + the Respond action's inputs
  flow-invoke <flow> <payload.json> [--header K=V ...]      call a flow; a PowerApp-trigger flow goes through an Http twin
  flow-twin-delete <flow>                                   remove the Http twin
  approvals                                                 approvals waiting for the signed-in user
  approve <approvalName> <response> [--comments TEXT]       answer one without a click
  app-deploy <app.msapp> --name "Display Name" [--apply] [--no-publish] [--allow-create] [--pin APP_ID]
  app-download <app> <out.msapp> [--draft]                  'Download a copy' (carries Studio's App checker result)
  app-launch-check <app>                                    runtime package status + NULL-rule scan (launch route)
  app-capture <app> <out-dir>                               same scan from what the browser player downloads
  census <spec.json>                                        read-only list census (JSON to stdout)
  self-test                                                 offline tests of every module (no network)
"""
import argparse
import json
import os
import sys

from . import auth as authmod
from . import config as configmod
from . import http as httpmod


def _ctx():
    cfg = configmod.load()
    http = httpmod.Http()
    return cfg, http, authmod.Auth(cfg, http)


def _flow(fc, ref):
    if len(ref) == 36 and ref.count('-') == 4:
        return fc.get(ref)
    f = fc.find(ref)
    if f is None:
        raise SystemExit('no flow %r' % ref)
    return f


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    if argv[0] == 'self-test':
        from . import selftest
        return selftest.run()
    ap = argparse.ArgumentParser(prog='devtenant')
    ap.add_argument('cmd')
    ap.add_argument('args', nargs='*')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--tag')
    ap.add_argument('--name')
    ap.add_argument('--pin', default='')
    ap.add_argument('--top', type=int, default=5)
    ap.add_argument('--no-start', action='store_true')
    ap.add_argument('--must-exist', action='store_true')
    ap.add_argument('--no-publish', action='store_true')
    ap.add_argument('--allow-create', action='store_true')
    ap.add_argument('--draft', action='store_true')
    ap.add_argument('--comments', default='')
    ap.add_argument('--header', action='append', default=[])
    a = ap.parse_args(argv)
    cfg, http, auth = _ctx()
    c, args = a.cmd, a.args

    if c == 'login':
        auth.device_code(args[0])
        print('signed in for %s; token cache: %s' % (args[0], cfg.token_cache))
        return 0
    if c == 'whoami':
        for k, slot in sorted(auth.cache.data.get('access', {}).items()):
            cl = authmod.claims(slot.get('token', ''))
            print('%-10s aud=%s upn=%s exp=%s' % (k, cl.get('aud'), cl.get('upn') or cl.get('unique_name'), cl.get('exp')))
        return 0
    if c.startswith('sp-') or c == 'census':
        from .sharepoint import SharePoint
        sp = SharePoint(cfg, auth, http)
        if c == 'sp-provision':
            schema = json.load(open(args[0], encoding='utf-8'))
            for spec in schema['lists']:
                row, actions = sp.plan_list(spec)
                for kind, name, _arg in actions:
                    print('PLAN %-15s %s: %s' % (kind, spec['title'], name))
                if not actions:
                    print('OK   %s: nothing to do' % spec['title'])
                if a.apply and actions:
                    sp.apply_list(spec, actions)
                    print('DONE %s' % spec['title'])
            return 0
        if c == 'sp-seed':
            rows = json.load(open(args[1], encoding='utf-8'))
            if not a.tag:
                raise SystemExit('--tag is required (fixtures must be findable for cleanup)')
            print('PLAN seed %d row(s) into %s tagged %r' % (len(rows), args[0], a.tag))
            if a.apply:
                sp.seed(args[0], rows, a.tag)
            return 0
        if c == 'sp-cleanup':
            if not a.tag:
                raise SystemExit('--tag is required')
            hits = sp.items(args[0], select='Id,Title', filt="startswith(Title,'%s')" % a.tag.replace("'", "''"))
            print('PLAN delete %d row(s) from %s tagged %r' % (len(hits), args[0], a.tag))
            if a.apply:
                print('deleted %d' % sp.cleanup(args[0], a.tag))
            return 0
        from .census import census, dumps
        print(dumps(census(sp, json.load(open(args[0], encoding='utf-8')))))
        return 0
    from .flows import FlowClient
    fc = FlowClient(cfg, auth, http)
    if c == 'flow-deploy':
        r = fc.deploy(args[0], a.name, apply=a.apply, start=not a.no_start, must_exist=a.must_exist)
        print(json.dumps(r, indent=1))
        return 0
    if c == 'flow-runs':
        f = _flow(fc, args[0])
        for r in fc.runs(f['name'], a.top):
            p = r.get('properties') or {}
            print('%s %-10s %s %s' % (r['name'], p.get('status'), p.get('startTime'), (p.get('error') or {}).get('code', '')))
        return 0
    if c == 'flow-run':
        f = _flow(fc, args[0])
        for row in fc.failed_actions(f['name'], args[1]):
            print('FAILED %s %s %s %s' % row)
        for act in fc.run_actions(f['name'], args[1]):
            if (act.get('properties') or {}).get('inputsLink') and 'respond' in act['name'].lower():
                print('RESPOND %s inputs: %s' % (act['name'], json.dumps(fc.read_link(act['properties']['inputsLink']))[:4000]))
        return 0
    if c == 'flow-invoke':
        f = _flow(fc, args[0])
        full = fc.get(f['name'])
        trig = (full['properties']['definition'].get('triggers') or {}).get('manual') or {}
        target = f['name']
        if trig.get('kind') in ('PowerApp', 'PowerAppV2'):
            target = fc.http_twin(f['name'], '%s [http twin]' % full['properties']['displayName'])
            print('NOTE invoking through Http twin %s (delete it with flow-twin-delete)' % target)
        hdrs = dict(h.split('=', 1) for h in a.header)
        status, body = fc.invoke(fc.callback_url(target), json.load(open(args[1], encoding='utf-8')), hdrs)
        print('HTTP %s\n%s' % (status, json.dumps(body, indent=1) if not isinstance(body, str) else body))
        return 0
    if c == 'flow-twin-delete':
        f = fc.get(args[0]) if len(args[0]) == 36 else fc.find(args[0])
        twin = fc.find('=%s [http twin]' % f['properties']['displayName'])
        if twin:
            fc.delete(twin['name'])
            print('deleted %s' % twin['name'])
        return 0
    if c == 'approvals':
        for v in fc.approvals_waiting():
            p = v.get('properties') or {}
            print('%s %s %s' % (v.get('name'), p.get('title') or (p.get('approval') or {}).get('title'), p.get('status')))
        return 0
    if c == 'approve':
        print(json.dumps(fc.answer_approval(args[0], args[1], a.comments), indent=1)[:2000])
        return 0
    from . import canvasdoc, runtime_rules
    from .powerapps import PowerAppsClient, deploy_app
    pa = PowerAppsClient(cfg, auth, http)

    def app_id(ref):
        if len(ref) == 36 and ref.count('-') == 4:
            return ref
        hit = pa.find(ref)
        if hit is None:
            raise SystemExit('no app %r' % ref)
        return hit['name']
    if c == 'app-deploy':
        if not a.name:
            raise SystemExit('--name "Display Name" is required')
        r = deploy_app(pa, fc, args[0], a.name, apply=a.apply, publish=not a.no_publish, name=a.pin, allow_create=a.allow_create)
        for line in r.pop('lines', []):
            print(line)
        print(json.dumps(r, indent=1))
        return 0
    if c == 'app-download':
        print(pa.download_document(app_id(args[0]), args[1], a.draft))
        return 0
    if c in ('app-launch-check', 'app-capture'):
        aid = app_id(args[0])
        doc_path = os.path.join(os.path.expanduser('~'), '.pp-playbook', 'work', aid + '.live.msapp')
        os.makedirs(os.path.dirname(doc_path), exist_ok=True)
        formulas = runtime_rules.document_formulas(canvasdoc.Msapp(pa.download_document(aid, doc_path)))
        if c == 'app-launch-check':
            launch = pa.launch(aid)
            det = launch.get('listAppPackageOperationDetails') or {}
            print('packageStatus=%s error=%s' % (det.get('packageStatus'), det.get('error')))
            if det.get('packageStatus') != 'Ready':
                return 1
            js = runtime_rules.fetch_package_js(http, launch)
        else:
            from . import appdriver
            js = [open(p, encoding='utf-8', errors='replace').read() for p in appdriver.capture_runtime(cfg, aid, args[1])]
        nulls = runtime_rules.scan(js, formulas)
        for ctl, prop, shape in nulls:
            print('NULL RULE %s.%s (%s)' % (ctl, prop, shape))
        print('%d null rule(s) in %d script(s)' % (len(nulls), len(js)))
        return 1 if nulls else 0
    raise SystemExit('unknown command %r (python -m devtenant --help)' % c)


if __name__ == '__main__':
    sys.exit(main())
