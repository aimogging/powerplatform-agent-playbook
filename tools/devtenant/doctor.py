"""python -m devtenant doctor -- a READ-ONLY first-run check of tools, configuration, sign-in and permissions.

Every probe prints one line: PASS / FAIL / SKIP, a category, and how to fix it. Categories keep three different
problems apart:
  TOOL        something is not installed on this machine
  CONFIG      config/environment.json is missing or still has placeholders
  SIGN-IN     no usable token for that API yet (run the login command shown)
  PERMISSION  signed in, but the account lacks a right in the tenant/site/environment (ask an admin)
  SERVICE     the service answered unexpectedly (read the detail)
Nothing is created or changed except the local token cache. Exit code 1 if any FAIL.
"""
import importlib.util
import json
import os
import shutil
import sys

from . import auth as authmod
from . import config as configmod
from . import http as httpmod
from .http import HttpError

RESULTS = []


def line(status, category, what, fix=''):
    RESULTS.append((status, category, what, fix))
    print('%-4s %-10s %s%s' % (status, category, what, ('\n                -> ' + fix) if fix and status != 'PASS' else ''))


def classify(ex):
    text = str(ex)
    if 'AADSTS65002' in text or 'not preauthorized' in text:
        return 'SIGN-IN', 'this sign-in client cannot get that audience; set clients.<key> in the config (reference/dev-tenant-auth.md)'
    if 'no cached token' in text or 'device-code' in text:
        return 'SIGN-IN', 'run: cd tools && python -m devtenant login <key>'
    if isinstance(ex, HttpError) and ex.status in (401,):
        return 'SIGN-IN', 'the token was refused; sign in again: python -m devtenant login <key>'
    if isinstance(ex, HttpError) and ex.status in (403,):
        return 'PERMISSION', 'the account lacks this right; ask the environment/site admin'
    if isinstance(ex, HttpError) and ex.status == 404:
        return 'CONFIG', 'not found -- check the ids/URLs in config/environment.json'
    return 'SERVICE', text[:200]


def run(argv=None):
    print('== tools')
    line('PASS' if sys.version_info >= (3, 9) else 'FAIL', 'TOOL', 'Python %d.%d' % sys.version_info[:2], 'install Python 3.9 or newer')
    for mod, why in (('yaml', 'stricter canvas-lint and the layout renderer: pip install pyyaml'),
                     ('playwright', 'browser tests of published apps: pip install playwright && playwright install')):
        ok = importlib.util.find_spec(mod) is not None
        line('PASS' if ok else 'FAIL', 'TOOL', 'Python package %s' % mod, why)
    line('PASS' if shutil.which('pac') else 'SKIP', 'TOOL', 'Power Platform CLI (pac) -- optional',
         'only needed for the pac packing route; install from Microsoft (dotnet tool install --global Microsoft.PowerApps.CLI.Tool)')

    print('== configuration')
    try:
        cfg = configmod.load()
    except SystemExit as ex:
        line('FAIL', 'CONFIG', 'config/environment.json', str(ex))
        return summary()
    missing = [k for k in ('tenantId', 'environmentId', 'siteUrl', 'operatorEmail') if not cfg.get(k) or '<' in str(cfg.get(k))]
    line('FAIL' if missing else 'PASS', 'CONFIG', 'required values filled', 'fill in: %s' % ', '.join(missing))
    if missing:
        return summary()

    http = httpmod.Http(max_attempts=2, log=lambda *a: None)
    au = authmod.Auth(cfg, http, out=lambda *a: None)
    print('== sign-in (one token per API)')
    tokens = {}
    for key, host in (('sharepoint', cfg.site_origin), ('flow', 'service.flow'), ('graph', 'graph.microsoft'),
                      ('powerApps', 'service.powerapps'), ('apihub', 'apihub.azure')):
        try:
            tok = au.token(key, interactive=False)
            aud = str(authmod.claims(tok).get('aud', ''))
            ok = key == 'sharepoint' or host in aud
            tokens[key] = tok
            line('PASS' if ok else 'FAIL', 'SIGN-IN', '%s token (aud %s)' % (key, aud), 'wrong audience -- check clients.%s' % key)
        except (SystemExit, Exception) as ex:
            cat, fix = classify(ex)
            line('FAIL', cat, '%s token' % key, fix.replace('<key>', key))

    print('== permissions (read-only probes)')
    from .sharepoint import SharePoint
    from .flows import FlowClient
    from .powerapps import PowerAppsClient
    if 'sharepoint' in tokens:
        sp = SharePoint(cfg, au, http)
        try:
            web = sp.get('web?$select=Title,Url')
            line('PASS', 'PERMISSION', 'read the site (%s)' % web.get('Title'))
            for label, low in (('create lists on the site (ManageLists)', 2048), ('add items (AddListItems)', 2)):
                r = sp.get("web/DoesUserHavePermissions(@v)?@v={'High':'0','Low':'%d'}" % low)
                ok = bool((r or {}).get('value'))
                line('PASS' if ok else 'FAIL', 'PERMISSION', label, 'ask the site owner for Edit/Design (or Owner) on this site')
        except (SystemExit, Exception) as ex:
            cat, fix = classify(ex)
            line('FAIL', cat, 'read the site %s' % cfg.get('siteUrl'), fix)
    if 'flow' in tokens:
        fc = FlowClient(cfg, au, http)
        try:
            flows = fc.list()
            line('PASS', 'PERMISSION', 'list flows in the environment (%d visible)' % len(flows))
        except (SystemExit, Exception) as ex:
            cat, fix = classify(ex)
            line('FAIL', cat, 'list flows in the environment', fix + ' (Environment Maker role needed to create flows)')
    if 'powerApps' in tokens:
        pa = PowerAppsClient(cfg, au, http)
        try:
            apps = pa.apps()
            line('PASS', 'PERMISSION', 'list canvas apps (%d visible)' % len(apps))
        except (SystemExit, Exception) as ex:
            cat, fix = classify(ex)
            line('FAIL', cat, 'list canvas apps', fix)
        try:
            conns = pa.connections()
            for api, name in (('shared_sharepointonline', 'SharePoint'), ('shared_office365', 'Office 365 Outlook')):
                line('PASS' if conns.get(api) else 'FAIL', 'PERMISSION', '%s connection exists (Connected)' % name,
                     'make.powerautomate.com -> Data -> Connections -> New connection -> %s (the API cannot create one)' % name)
        except (SystemExit, Exception) as ex:
            cat, fix = classify(ex)
            line('FAIL', cat, 'list connections', fix)
        try:
            r = pa.http.request('POST', '%s/generateResourceStorage?api-version=2016-11-01' % pa.bap, headers=pa._h(), body={},
                                allow_write_retry=True)
            ok = r.status == 200 and 'sharedAccessSignature' in r.text
            line('PASS' if ok else 'FAIL', 'PERMISSION' if r.status == 403 else 'SERVICE',
                 'package import storage (needed to import a canvas package)', 'HTTP %d -- Environment Maker role needed' % r.status)
        except (SystemExit, Exception) as ex:
            cat, fix = classify(ex)
            line('FAIL', cat, 'package import storage', fix)
    prof = os.path.expanduser((cfg.get('browser') or {}).get('profileDir') or '.pp-playbook/pw-profile')
    prof = prof if os.path.isabs(prof) else os.path.join(os.path.expanduser('~'), prof)
    line('PASS' if os.path.isdir(prof) else 'SKIP', 'TOOL', 'browser profile for app tests (%s)' % prof,
         'run the app stage of example/run_e2e.py once with --headed and sign in when the browser opens')
    line('SKIP', 'PERMISSION', 'create/publish an app, trigger a flow, read run history',
         'these need a write -- example/run_e2e.py proves them end to end and cleans up')
    return summary()


def summary():
    fails = [r for r in RESULTS if r[0] == 'FAIL']
    print('\ndoctor: %d PASS, %d FAIL, %d SKIP' % (sum(r[0] == 'PASS' for r in RESULTS), len(fails), sum(r[0] == 'SKIP' for r in RESULTS)))
    if fails:
        by = {}
        for _s, cat, what, _f in fails:
            by.setdefault(cat, []).append(what)
        for cat, items in by.items():
            print('  %s: %s' % (cat, '; '.join(items)))
    return 1 if fails else 0
