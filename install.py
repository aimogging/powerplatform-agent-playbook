#!/usr/bin/env python3
"""Install (or update) the Power Platform agent playbook -- no git needed.

    curl -fsSL https://raw.githubusercontent.com/aimogging/powerplatform-agent-playbook/main/install.py | python -
    curl -fsSL https://raw.githubusercontent.com/aimogging/powerplatform-agent-playbook/main/install.py | python - --update

(use python3 instead of python on macOS/Linux if that is your command; on Windows `py` works too). Options go after
the lone dash:

    --dir PATH       where to put it (default ./powerplatform-agent-playbook)
    --update         refresh an existing install in place; keeps config/environment.json and the .venv
    --force          install into a folder that is not empty (your config/environment.json is still kept)
    --no-venv        skip the Python virtual environment and packages
    --no-browser     skip the Playwright browser download
    --zip URL|PATH   install from another archive (a mirror or a local file)
    --self-test      offline test of this installer

What it does: downloads the main-branch archive, unpacks it, creates .venv with the optional packages (PyYAML,
Playwright) and a Playwright browser, checks for the Power Platform CLI (optional), copies the example config to
config/environment.json, tells you what to fill in, and runs the read-only doctor once the config is filled.
Standard library only. Nothing outside the install folder is changed except pip/Playwright caches.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

ZIP_URL = 'https://github.com/aimogging/powerplatform-agent-playbook/archive/refs/heads/main.zip'
DEFAULT_DIR = 'powerplatform-agent-playbook'
KEEP = ('config/environment.json',)
KEEP_DIRS = ('.venv',)
MANIFEST = '.pp-installed-files'
PACKAGES = ['pyyaml', 'playwright']
REQUIRED_KEYS = ('tenantId', 'environmentId', 'siteUrl', 'operatorEmail')


class InstallError(Exception):
    pass


def say(msg=''):
    print(msg, flush=True)


# ------------------------------------------------------------------------------------------------ download + unpack
def fetch(src):
    if os.path.isfile(src):
        with open(src, 'rb') as fh:
            return fh.read()
    say('downloading %s' % src)
    try:
        req = urllib.request.Request(src, headers={'User-Agent': 'pp-playbook-installer'})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as ex:
        hint = ' (is the repository public? a private repository cannot be downloaded this way)' if ex.code == 404 else ''
        raise InstallError('download failed: HTTP %d%s' % (ex.code, hint))
    except (urllib.error.URLError, OSError) as ex:
        raise InstallError('download failed: %s -- check the network or a proxy, or pass --zip <local file>' % getattr(ex, 'reason', ex))


def release_files(data):
    """zip bytes -> {relative path: bytes}, stripping the single top folder GitHub puts in archives."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InstallError('the download is not a zip archive (a proxy or login page?)')
    names = [n for n in z.namelist() if not n.endswith('/')]
    tops = {n.split('/', 1)[0] for n in names}
    strip = len(tops) == 1 and all('/' in n for n in names)
    out = {}
    for n in names:
        rel = n.split('/', 1)[1] if strip else n
        if not rel or rel.startswith('/') or '..' in rel.split('/'):
            raise InstallError('refusing an archive entry outside the install folder: %s' % n)
        out[rel] = z.read(n)
    if 'AGENTS.md' not in out:
        raise InstallError('the archive does not look like the playbook (no AGENTS.md at its top)')
    return out


def is_empty(path):
    return not os.path.exists(path) or (os.path.isdir(path) and not os.listdir(path))


def write_release(target, files, update):
    old = set()
    mpath = os.path.join(target, MANIFEST)
    if update and os.path.isfile(mpath):
        old = {l.strip() for l in open(mpath, encoding='utf-8') if l.strip()}
    written = 0
    for rel, data in sorted(files.items()):
        if rel in KEEP and os.path.isfile(os.path.join(target, rel)):
            continue
        p = os.path.join(target, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'wb') as fh:
            fh.write(data)
        written += 1
    removed = 0
    for rel in sorted(old - set(files)):
        if rel in KEEP:
            continue
        p = os.path.join(target, *rel.split('/'))
        if os.path.isfile(p):
            os.remove(p)
            removed += 1
    with open(mpath, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(sorted(files)) + '\n')
    return written, removed


# ------------------------------------------------------------------------------------------------ environment
def venv_python(target):
    for rel in (('Scripts', 'python.exe'), ('bin', 'python')):
        p = os.path.join(target, '.venv', *rel)
        if os.path.isfile(p):
            return p
    return None


def run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or '').strip().splitlines()[-3:]
        raise InstallError('%s failed:\n    %s' % (what, '\n    '.join(tail)))
    return r


def setup_venv(target, browser):
    py = venv_python(target)
    if not py:
        say('creating .venv (Python virtual environment)')
        run([sys.executable, '-m', 'venv', os.path.join(target, '.venv')], 'creating the virtual environment')
        py = venv_python(target)
        if not py:
            raise InstallError('the virtual environment has no python -- install the venv module (e.g. python3-venv) and rerun')
    say('installing packages: %s' % ', '.join(PACKAGES))
    run([py, '-m', 'pip', 'install', '--quiet', '--upgrade'] + PACKAGES, 'pip install')
    if browser:
        say('installing the Playwright browser (chromium; a few hundred MB, once)')
        try:
            run([py, '-m', 'playwright', 'install', 'chromium'], 'playwright install')
            say('  note: the config defaults to browser.channel "msedge" (your installed Edge). Without Edge, set '
                '"channel": "" in config/environment.json to use this chromium.')
        except InstallError as ex:
            say('  WARNING: %s\n  Browser tests of published apps need it; rerun later: %s -m playwright install chromium' % (ex, py))
    return py


def check_pac():
    if shutil.which('pac'):
        say('Power Platform CLI (pac): found (optional)')
    else:
        say('Power Platform CLI (pac): not found -- optional. Only the pac packing route needs it. To get it: install the\n'
            '  .NET SDK, then: dotnet tool install --global Microsoft.PowerApps.CLI.Tool')


def config_state(target):
    cfg = os.path.join(target, 'config', 'environment.json')
    example = os.path.join(target, 'config', 'environment.example.json')
    created = False
    if not os.path.isfile(cfg) and os.path.isfile(example):
        shutil.copyfile(example, cfg)
        created = True
    try:
        data = json.load(open(cfg, encoding='utf-8-sig'))
    except (OSError, ValueError) as ex:
        return cfg, created, ['config/environment.json is unreadable: %s' % ex]
    todo = [k for k in REQUIRED_KEYS if not data.get(k) or '<' in str(data.get(k))]
    return cfg, created, todo


# ------------------------------------------------------------------------------------------------ main
def install(argv=None):
    ap = argparse.ArgumentParser(prog='install.py', description='Install or update the Power Platform agent playbook.')
    ap.add_argument('--dir', default=DEFAULT_DIR)
    ap.add_argument('--update', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--no-venv', action='store_true')
    ap.add_argument('--no-browser', action='store_true')
    ap.add_argument('--zip', default=os.environ.get('PP_PLAYBOOK_ZIP_URL') or ZIP_URL)
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if sys.version_info < (3, 9):
        raise InstallError('Python 3.9 or newer is needed (this is %d.%d)' % sys.version_info[:2])
    target = os.path.abspath(a.dir)
    if a.update and not os.path.isfile(os.path.join(target, 'AGENTS.md')):
        raise InstallError('%s is not an existing install -- run without --update to install' % target)
    if not a.update and not a.force and not is_empty(target):
        raise InstallError('%s is not empty. Use --update to refresh an install there, --force to install anyway, '
                           'or --dir <another folder>.' % target)
    files = release_files(fetch(a.zip))
    os.makedirs(target, exist_ok=True)
    written, removed = write_release(target, files, a.update)
    say('%s %d file(s) in %s%s' % ('updated' if a.update else 'installed', written, target,
                                    (' (%d obsolete removed)' % removed) if removed else ''))
    py = None
    if not a.no_venv:
        py = setup_venv(target, not a.no_browser)
    check_pac()
    cfg, created, todo = config_state(target)
    run_py = py or sys.executable
    say('')
    if todo:
        say('%s config/environment.json. Fill in these values for YOUR dev/test tenant:' % ('Created' if created else 'Check'))
        for k in todo:
            say('  - %s' % k)
        say('  (file: %s; README "First run: prove your setup" explains each one)' % cfg)
        say('\nThen check your setup (read-only):')
        say('  cd "%s"' % os.path.join(target, 'tools'))
        say('  "%s" -m devtenant doctor' % run_py)
    else:
        say('Config is filled in: running the read-only doctor ...')
        r = subprocess.run([run_py, '-m', 'devtenant', 'doctor'], cwd=os.path.join(target, 'tools'))
        if r.returncode != 0:
            say('\nThe doctor found problems; each FAIL line above says how to fix it.')
    say('\nDone. Start with %s' % os.path.join(target, 'README.md'))
    return 0


def main(argv=None):
    try:
        return install(argv)
    except InstallError as ex:
        say('install: %s' % ex)
        return 1
    except KeyboardInterrupt:
        say('install: interrupted')
        return 130


# ------------------------------------------------------------------------------------------------ self-test
def self_test():
    results = []

    def ok(cond, what):
        results.append(bool(cond))
        say(('  ok   ' if cond else '  FAIL ') + what)

    def fixture(path, extra=None, drop=()):
        files = {'AGENTS.md': '# agents\n', 'README.md': '# readme\n', 'tools/a.py': 'x = 1\n', 'tools/old.py': 'y = 1\n',
                 'config/environment.example.json': json.dumps({'tenantId': '<TENANT_ID>', 'environmentId': '<ENV>',
                                                                'siteUrl': 'https://contoso.sharepoint.com/sites/x',
                                                                'operatorEmail': '<YOUR_UPN>'})}
        files.update(extra or {})
        with zipfile.ZipFile(path, 'w') as z:
            for rel, text in files.items():
                if rel not in drop:
                    z.writestr('powerplatform-agent-playbook-main/' + rel, text)

    def call(args):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = main(args)
        finally:
            sys.stdout = old
        return rc, buf.getvalue()

    with tempfile.TemporaryDirectory() as d:
        z1, z2, bad = os.path.join(d, 'r1.zip'), os.path.join(d, 'r2.zip'), os.path.join(d, 'bad.zip')
        fixture(z1)
        fixture(z2, {'tools/a.py': 'x = 2\n', 'tools/new.py': 'z = 1\n'}, drop=('tools/old.py',))
        with open(bad, 'wb') as fh:
            fh.write(b'<html>login</html>')
        target = os.path.join(d, 'pb')
        base = ['--dir', target, '--no-venv']
        rc, out = call(base + ['--zip', z1])
        ok(rc == 0 and os.path.isfile(os.path.join(target, 'tools', 'a.py')), 'fresh install unpacks the archive (top folder stripped)')
        ok(os.path.isfile(os.path.join(target, 'config', 'environment.json')), 'example config copied to environment.json')
        ok('tenantId' in out and 'operatorEmail' in out and 'siteUrl' not in out.split('Fill in')[1].split('(file')[0],
           'tells which values to fill (placeholders only)')
        rc, out = call(base + ['--zip', z1])
        ok(rc == 1 and 'not empty' in out and 'Traceback' not in out, 'refuses a non-empty folder, plain message, no traceback')
        cfg = os.path.join(target, 'config', 'environment.json')
        with open(cfg, 'w', encoding='utf-8') as fh:
            fh.write('{"tenantId": "mine"}')
        rc, out = call(base + ['--zip', z2, '--update'])
        ok(rc == 0 and open(os.path.join(target, 'tools', 'a.py')).read() == 'x = 2\n', '--update refreshes files')
        ok(open(cfg).read() == '{"tenantId": "mine"}', '--update keeps config/environment.json')
        ok(not os.path.exists(os.path.join(target, 'tools', 'old.py')) and os.path.isfile(os.path.join(target, 'tools', 'new.py')),
           '--update removes files dropped from the release, adds new ones')
        rc, out = call(base + ['--zip', z1, '--force'])
        ok(rc == 0 and open(cfg).read() == '{"tenantId": "mine"}', '--force installs over a non-empty folder, config still kept')
        rc, out = call(['--dir', os.path.join(d, 'other'), '--no-venv', '--zip', bad])
        ok(rc == 1 and 'not a zip' in out and 'Traceback' not in out, 'a non-zip download is a plain error')
        rc, out = call(['--dir', os.path.join(d, 'nope'), '--no-venv', '--update', '--zip', z1])
        ok(rc == 1 and 'not an existing install' in out, '--update on a folder without an install is refused')
        rc, out = call(['--dir', os.path.join(d, 'net'), '--no-venv', '--zip', 'http://localhost:9/none.zip'])
        ok(rc == 1 and 'download failed' in out and 'Traceback' not in out, 'network failure is a plain error')
        evil = os.path.join(d, 'evil.zip')
        with zipfile.ZipFile(evil, 'w') as z:
            z.writestr('top/AGENTS.md', 'x')
            z.writestr('top/../../escape.txt', 'x')
        rc, out = call(['--dir', os.path.join(d, 'evil'), '--no-venv', '--zip', evil])
        ok(rc == 1 and not os.path.exists(os.path.join(d, 'escape.txt')), 'archive entries outside the folder are refused')
    passed = all(results)
    say('self-test: ' + ('PASS' if passed else 'FAIL'))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
