#!/usr/bin/env python3
"""scrub-check.py -- refuse to let deployment-identifying data into this repository.

This repository is a GENERIC playbook. It must never carry a real tenant/environment/app/flow/
connection/list GUID, a real hostname, a person, an organisation, a product name, a token, or a
model identifier. This gate scans the working tree (and, with --history, every commit's message
and diff) for:

  guid        any 8-4-4-4-12 GUID that is not an obvious placeholder and not one of the public,
              well-known Microsoft identifiers documented in ALLOWED_GUIDS below
  hex32       a bare 32-hex-digit token (de-dashed GUIDs are how connection ids and flow ids
              leak) unless it is an obvious placeholder
  email       any e-mail address outside the example domains (contoso.com, example.com, *.example,
              *.invalid, *.test) and the commit-trailer address in ALLOWED_EMAILS
  url         any http(s) URL whose host is not on the public-Microsoft / example allowlist, or
              whose host is a TENANT-SHAPED subdomain (<tenant>.sharepoint.com, <org>.crm.dynamics.com,
              <tenant>.onmicrosoft.com, ...) other than the contoso/example placeholders
  model-id    a concrete LLM model identifier (use <MODEL_ID> placeholders instead)
  denylist    every term in the EXTERNAL denylist file (--denylist PATH). The denylist lives OUTSIDE
              this repo on purpose: a list of the identifying terms would itself be a leak.
              Format: one term per line (case-insensitive substring); 're:<regex>' (case-insensitive)
              or 'rec:<regex>' (case-sensitive); '#' comments.
  forbidden   tracked files that must never be committed (packages, token caches, local configs)

Usage:
  python tools/scrub-check.py --denylist <path-outside-repo>            # working tree
  python tools/scrub-check.py --denylist <path> --history               # + every commit (message + diff)
  python tools/scrub-check.py --self-test                               # offline test of the gate itself

Exit code 0 = clean, 1 = findings, 2 = usage error. Commit author/committer metadata is NOT scanned
(it is the configured git identity, not repository content); commit MESSAGES and DIFFS are.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

# ------------------------------------------------------------------------------------------------
# Public, well-known Microsoft identifiers that the playbook may cite. Each one is documented in
# Microsoft's own tooling/docs and identifies a Microsoft first-party app or resource, not a tenant.
ALLOWED_GUIDS = {
    '9bc3ab49-b65d-410a-85ad-de819febfddc': 'SharePoint Online Management Shell public client (FOCI family)',
    '386ce8c0-7421-48c9-a1df-2a532400339f': 'Power Automate Desktop first-party public client',
    '51f81489-12ee-4a9e-aaae-a2591f45987d': 'Power Platform CLI (pac) public client',
    '04b07795-8ddb-461a-bbee-02f9e1bf7b46': 'Azure CLI public client',
    '14d82eec-204b-4c2f-b7e8-296a70dab67e': 'Microsoft Graph Command Line Tools public client',
    '689e5960-2e49-4505-98d8-369236220fc6': 'Power Apps PowerShell module public client (prod cloud)',
    '00000003-0000-0ff1-ce00-000000000000': 'SharePoint Online resource principal (well-known)',
    '00000003-0000-0000-c000-000000000000': 'Microsoft Graph resource principal (well-known)',
    '475226c6-020e-4fb2-8a90-7a972cbfc1d4': 'Power Apps service resource principal (well-known)',
}
ALLOWED_EMAILS = {
    'noreply@anthropic.com': 'Co-Authored-By commit trailer',
}
EXAMPLE_DOMAIN = re.compile(r'(^|\.)(contoso\.com|example\.com|example\.org|example|invalid|test|localhost)$', re.I)

# Hosts that are public Microsoft (or standards) endpoints. Suffix match.
PUBLIC_HOST_SUFFIXES = (
    'microsoft.com', 'microsoft.us', 'microsoftonline.com', 'microsoftonline.us', 'windows.net',
    'powerapps.com', 'powerapps.us', 'powerautomate.com', 'powerautomate.us', 'appsplatform.us',
    'powerplatform.com', 'azure.com', 'azure.us', 'usgovcloudapi.net', 'office.com', 'office365.us',
    'aka.ms', 'svc.ms', 'dynamics.com', 'microsoftdynamics.us', 'sharepoint.com', 'sharepoint.us',
    'sharepoint-mil.us', 'onmicrosoft.com', 'azure-apim.net', 'azure-apihub.us',
    'w3.org', 'json-schema.org', 'yaml.org', 'openxmlformats.org', 'playwright.dev', 'python.org',
)
# Suffixes whose SUBDOMAIN names a tenant/org/connection. Only placeholder subdomains may appear.
TENANT_SHAPED_SUFFIXES = (
    'sharepoint.com', 'sharepoint.us', 'sharepoint-mil.us', 'onmicrosoft.com', 'crm.dynamics.com',
    'crm9.dynamics.com', 'crm.microsoftdynamics.us', 'crm.appsplatform.us', 'api.crm.appsplatform.us',
    'azure-apim.net', 'azure-apihub.us',
)
PLACEHOLDER_SUBDOMAIN = re.compile(r'^(contoso|contoso-my|contoso-admin|example|yourtenant|tenant|org|yourorg)$', re.I)
# Service hosts that LOOK tenant-shaped but are Microsoft service endpoints.
SERVICE_SUBDOMAINS = {'www', 'login', 'graph', 'api', 'static', 'admin'}

GUID_RE = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
HEX32_RE = re.compile(r'(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])')
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
URL_RE = re.compile(r'https?://([A-Za-z0-9.-]+)', re.I)
MODEL_RE = re.compile(
    r'\b(claude-(?:opus|sonnet|haiku|instant)[-.\w]*|gpt-(?:\d|o\d)[-.\w]*|o\d-mini\b|gemini-\d[-.\w]*|'
    r'text-embedding-[-.\w]+|llama-?\d[-.\w]*|mistral-(?:large|medium|small)[-.\w]*)', re.I)

FORBIDDEN_FILES = re.compile(
    r'(\.msapp|\.msapr|\.zip|\.token\.json|token-cache[^/]*\.json|\.local\.json|credentials[^/]*\.json|\.key|'
    r'(^|/)config/environment\.json)$', re.I)
TEXT_EXT = ('.md', '.py', '.json', '.yaml', '.yml', '.html', '.txt', '.css', '.js', '.gitignore', '.cfg', '.toml', '.ini', '.xml', '.csv')


def is_placeholder_guid(g):
    h = g.replace('-', '').lower()
    if len(set(h)) == 1:
        return True                                   # 00000000-... / ffffffff-...
    if re.fullmatch(r'0{20,31}[0-9a-f]{1,12}', h):      # 00000000-0000-0000-0000-00000000000N
        return True
    groups = g.lower().split('-')
    if all(len(set(x)) == 1 for x in groups):           # 11111111-2222-3333-4444-555555555555
        return True
    return False


def load_denylist(path):
    subs, regs = [], []
    with open(path, encoding='utf-8') as fh:
        for raw in fh:
            line = raw.rstrip('\n').rstrip('\r')
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            if line.startswith('rec:'):
                regs.append((re.compile(line[4:]), line))
            elif line.startswith('re:'):
                regs.append((re.compile(line[3:], re.I), line))
            else:
                subs.append(line.strip().lower())
    return subs, regs


def host_ok(host):
    host = host.lower().rstrip('.')
    if not host or EXAMPLE_DOMAIN.search(host):
        return True
    for suf in TENANT_SHAPED_SUFFIXES:
        if host.endswith('.' + suf):
            sub = host[: -len(suf) - 1]
            first = sub.split('.')[0]
            if PLACEHOLDER_SUBDOMAIN.match(first) or first in SERVICE_SUBDOMAINS:
                return True
            return False
    return any(host == s or host.endswith('.' + s) for s in PUBLIC_HOST_SUFFIXES)


def scan_text(text, where, deny, findings):
    subs, regs = deny
    low = text.lower()
    lines = text.split('\n')
    lowlines = low.split('\n')
    for n, line in enumerate(lines, 1):
        for m in GUID_RE.finditer(line):
            g = m.group(0).lower()
            if g in ALLOWED_GUIDS or is_placeholder_guid(g):
                continue
            findings.append((where, n, 'guid', m.group(0)))
        for m in HEX32_RE.finditer(line):
            h = m.group(0).lower()
            if len(set(h)) == 1 or re.fullmatch(r'0{20,31}[0-9a-f]{1,12}', h):
                continue
            if any(h == a.replace('-', '') for a in ALLOWED_GUIDS):
                continue
            findings.append((where, n, 'hex32', m.group(0)))
        for m in EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            dom = e.split('@', 1)[1]
            if e in ALLOWED_EMAILS or EXAMPLE_DOMAIN.search(dom):
                continue
            findings.append((where, n, 'email', m.group(0)))
        for m in URL_RE.finditer(line):
            if not host_ok(m.group(1)):
                findings.append((where, n, 'url', m.group(0)))
        for m in MODEL_RE.finditer(line):
            findings.append((where, n, 'model-id', m.group(0)))
        ll = lowlines[n - 1]
        for s in subs:
            if s in ll:
                findings.append((where, n, 'denylist', s))
        for rx, src in regs:
            if rx.search(line):
                findings.append((where, n, 'denylist', src))


def repo_root():
    return subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                          check=True).stdout.strip()


def working_tree_files(root):
    out = subprocess.run(['git', '-C', root, 'ls-files', '-co', '--exclude-standard'],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    return [f for f in out if f]


def read_text(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    if b'\x00' in data[:8192]:
        return None
    return data.decode('utf-8', errors='replace')


def scan_tree(root, deny, findings):
    count = 0
    for rel in working_tree_files(root):
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        if FORBIDDEN_FILES.search(rel.replace('\\', '/')):
            findings.append((rel, 0, 'forbidden', 'this file type must never be committed'))
            continue
        # the scrub gate itself documents the allowed GUIDs; everything else still applies to it
        text = read_text(p)
        if text is None:
            findings.append((rel, 0, 'binary', 'binary file in the repo -- review by hand'))
            continue
        count += 1
        scan_text(text, rel, deny, findings)
    return count


def scan_history(root, deny, findings):
    log = subprocess.run(['git', '-C', root, 'log', '--all', '-p', '--no-color', '--format=@@COMMIT %H%n%B'],
                         capture_output=True, text=True, encoding='utf-8', errors='replace', check=True).stdout
    commits = 0
    current, buf = None, []

    def flush():
        if current is not None:
            scan_text('\n'.join(buf), 'history:' + current[:10], deny, findings)

    for line in log.split('\n'):
        if line.startswith('@@COMMIT '):
            flush()
            current, buf = line.split(' ', 1)[1], []
            commits += 1
            continue
        if line.startswith('diff --git') or line.startswith('index ') or line.startswith('--- ') or line.startswith('+++ '):
            buf.append(line)
            continue
        # only ADDED lines and message lines can introduce content; removed lines were added earlier
        if line.startswith('-') and not line.startswith('---'):
            continue
        buf.append(line)
    flush()
    return commits


def report(findings):
    for where, n, kind, what in findings:
        loc = '%s:%d' % (where, n) if n else where
        print('SCRUB %-9s %s  %s' % (kind, loc, what))


def self_test():
    ok = True

    def check(cond, what):
        nonlocal ok
        print(('  ok   ' if cond else '  FAIL ') + what)
        ok = ok and cond

    with tempfile.TemporaryDirectory() as d:
        dl = os.path.join(d, 'deny.txt')
        with open(dl, 'w', encoding='utf-8') as fh:
            fh.write('# test denylist\nfabrikam-secret-unit\nre:\\bZORG\\b\nrec:\\bQQX\\b\n')
        deny = load_denylist(dl)

        def kinds(text):
            f = []
            scan_text(text, 't', deny, f)
            return [k for _w, _n, k, _x in f]

        # fixtures are assembled at run time so this file passes its own gate
        g = '-'.join(['1b4e28ba', '2fa1', '11d2', '883f', '0016d3cca427'])
        check(kinds('flow %s here' % g) == ['guid'], 'real-looking GUID flagged')
        check(kinds('00000000-0000-0000-0000-000000000000') == [], 'all-zero GUID allowed')
        check(kinds('00000000-0000-0000-0000-000000000007') == [], 'sequential placeholder GUID allowed')
        check(kinds('11111111-2222-3333-4444-555555555555') == [], 'repeated-digit placeholder GUID allowed')
        check(kinds('client 9bc3ab49-b65d-410a-85ad-de819febfddc') == [], 'documented public client allowed')
        check(kinds('conn ' + g.replace('-', '')) == ['hex32'], 'de-dashed GUID flagged')
        check(kinds('mail someone' + '@' + 'realcorp.co') == ['email'], 'real-looking email flagged')
        check(kinds('mail pat@contoso.com') == [], 'contoso email allowed')
        check(kinds('Co-Authored-By: X <noreply@anthropic.com>') == [], 'commit trailer allowed')
        check(kinds('https://' + 'realcorp.sharepoint.com/sites/x') == ['url'], 'tenant SharePoint host flagged')
        check(kinds('https://contoso.sharepoint.com/sites/HelpDesk') == [], 'contoso SharePoint host allowed')
        check(kinds('https://graph.microsoft.com/v1.0/me') == [], 'public Microsoft host allowed')
        check(kinds('https://login.microsoftonline.us/x') == [], 'sovereign login host allowed')
        check(kinds('https://' + 'git.internal.corp/repo') == ['url'], 'unknown host flagged')
        check(kinds('https://<SITE_URL>/_api/web') == [], 'placeholder URL allowed')
        check(kinds('https://' + 'orgabc123.crm.dynamics.com') == ['url'], 'Dataverse org host flagged')
        check(kinds('model ' + 'gpt' + '-4o-mini') == ['model-id'], 'model id flagged')
        check(kinds('model <MODEL_ID>') == [], 'model placeholder allowed')
        check(kinds('the Fabrikam-Secret-Unit roster') == ['denylist'], 'denylist substring (case-insensitive)')
        check(kinds('zorg team') == ['denylist'], 'denylist re: (case-insensitive word)')
        check(kinds('qqx rule') == [] and kinds('QQX rule') == ['denylist'], 'denylist rec: is case-sensitive')
        check(FORBIDDEN_FILES.search('example/dist/App.msapp') is not None, 'msapp is a forbidden file')
        check(FORBIDDEN_FILES.search('x/minted-flow.token.json') is not None, 'token cache is a forbidden file')
        check(FORBIDDEN_FILES.search('config/environment.json') is not None, 'real environment config is forbidden')
        check(FORBIDDEN_FILES.search('config/environment.example.json') is None, 'example config is allowed')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--denylist', help='path to the external denylist file (must be OUTSIDE the repo)')
    ap.add_argument('--history', action='store_true', help='also scan every commit message and diff')
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.denylist:
        print('usage error: --denylist PATH is required (keep the list outside this repo)', file=sys.stderr)
        return 2
    root = repo_root()
    dl = os.path.abspath(a.denylist)
    if os.path.commonpath([dl.lower(), os.path.abspath(root).lower()]) == os.path.abspath(root).lower():
        print('usage error: the denylist must live OUTSIDE the repository (it would itself be a leak)', file=sys.stderr)
        return 2
    deny = load_denylist(dl)
    findings = []
    files = scan_tree(root, deny, findings)
    commits = scan_history(root, deny, findings) if a.history else 0
    report(findings)
    print('scrub-check: %d file(s)%s scanned against %d denylist term(s): %s' % (
        files, (', %d commit(s)' % commits) if a.history else '', len(deny[0]) + len(deny[1]),
        'CLEAN' if not findings else '%d FINDING(S)' % len(findings)))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
