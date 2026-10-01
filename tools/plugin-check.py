#!/usr/bin/env python3
"""plugin-check.py -- keep the Claude Code plugin packaging of this repo valid and in sync with AGENTS.md.

The repo is used two ways: as a TEMPLATE (clone it; the agent reads AGENTS.md / CLAUDE.md) and as a Claude Code
PLUGIN (a plugin's root CLAUDE.md is not loaded, so the rules travel as the skill `playbook-rules`). This tool:

  * renders skills/playbook-rules/SKILL.md from AGENTS.md (deliverable statement, non-negotiable rules, routing,
    lifecycle), rewriting repo paths to ${CLAUDE_PLUGIN_ROOT}/... -- Claude Code substitutes that variable in skill
    text, so a plugin user's agent sees absolute paths into the installed copy;
  * checks the rendered skill is what is committed (the two texts cannot drift);
  * checks CLAUDE.md == AGENTS.md, the plugin and marketplace manifests, and every skill's front matter.

    python tools/plugin-check.py            # check (exit 1 on any finding)
    python tools/plugin-check.py --write    # regenerate skills/playbook-rules/SKILL.md from AGENTS.md
    python tools/plugin-check.py --self-test
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SECTIONS = ('## Non-negotiable rules', '## Routing -- which skill', '## The lifecycle')
PATH_RX = re.compile(r'`((?:python3? )?)((?:tools|reference|skills|config|example|docs)/[^`\s]*)')
NAME_RX = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
FRONT = """---
name: playbook-rules
description: The non-negotiable rules, skill routing and 13-phase lifecycle of the Power Platform agent playbook. Load FIRST for any work on canvas Power Apps, Power Automate cloud flows, SharePoint lists behind them, .msapp or flow import packages, or a Power Platform dev/test tenant -- before writing formulas, flow definitions, packages or tenant scripts.
---

<!-- GENERATED from AGENTS.md by tools/plugin-check.py --write. Do not edit by hand. -->

# Power Platform agent playbook -- rules (plugin form)

Installed as a Claude Code plugin, the playbook lives at `${CLAUDE_PLUGIN_ROOT}`; every path below points there.
Run its tools with that prefix (`python ${CLAUDE_PLUGIN_ROOT}/tools/run-self-tests.py`). Tenant values go in
`~/.pp-playbook/environment.json` (or set `PP_PLAYBOOK_CONFIG`), never inside the plugin folder -- an update
replaces it. Used as a template instead, the same text is `AGENTS.md` at the repo root.

"""


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def split_sections(text):
    """'## heading' -> body text (up to the next '## ')."""
    out, cur, buf = {}, None, []
    for line in text.split('\n'):
        if line.startswith('## '):
            if cur:
                out[cur] = '\n'.join(buf).rstrip() + '\n'
            cur, buf = line.strip(), [line]
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = '\n'.join(buf).rstrip() + '\n'
    return out


def lead(text):
    """The bold deliverable paragraph at the top of AGENTS.md."""
    m = re.search(r'^\*\*The deliverable.*?(?=\n\n)', text, re.S | re.M)
    return m.group(0) if m else ''


def plugin_paths(text):
    return PATH_RX.sub(lambda m: '`' + m.group(1) + '${CLAUDE_PLUGIN_ROOT}/' + m.group(2), text)


def render(agents_text):
    secs = split_sections(agents_text)
    picked, missing = [], []
    for want in SECTIONS:
        hit = [k for k in secs if k == want or k.startswith(want + ' ')]
        (picked if hit else missing).append(secs[hit[0]] if hit else want)
    if missing:
        raise SystemExit('AGENTS.md lacks section(s): %s' % ', '.join(missing))
    body = [lead(agents_text), ''] + picked
    return FRONT + plugin_paths('\n'.join(body)).rstrip() + '\n'


def front_matter(text):
    m = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            fm[k.strip()] = v.strip()
    return fm


def check(root):
    problems = []
    agents = read(os.path.join(root, 'AGENTS.md'))
    if os.path.isfile(os.path.join(root, 'CLAUDE.md')) and read(os.path.join(root, 'CLAUDE.md')) != agents:
        problems.append('CLAUDE.md differs from AGENTS.md (they are one text; copy AGENTS.md over it)')
    rules_path = os.path.join(root, 'skills', 'playbook-rules', 'SKILL.md')
    try:
        want = render(agents)
        if not os.path.isfile(rules_path) or read(rules_path) != want:
            problems.append('skills/playbook-rules/SKILL.md is out of sync with AGENTS.md: run tools/plugin-check.py --write')
    except SystemExit as ex:
        problems.append(str(ex))
    for fname, required in (('plugin.json', ('name',)), ('marketplace.json', ('name', 'owner', 'plugins'))):
        p = os.path.join(root, '.claude-plugin', fname)
        if not os.path.isfile(p):
            problems.append('.claude-plugin/%s missing' % fname)
            continue
        try:
            doc = json.loads(read(p))
        except ValueError as ex:
            problems.append('.claude-plugin/%s is not JSON: %s' % (fname, ex))
            continue
        for k in required:
            if not doc.get(k):
                problems.append('.claude-plugin/%s: "%s" is required' % (fname, k))
        if doc.get('name') and not NAME_RX.match(doc['name']):
            problems.append('.claude-plugin/%s: name %r must be kebab-case letters/digits/.-_' % (fname, doc['name']))
        if fname == 'marketplace.json':
            if not (doc.get('owner') or {}).get('name'):
                problems.append('marketplace owner.name is required')
            for i, e in enumerate(doc.get('plugins') or []):
                src = e.get('source')
                if isinstance(src, str) and not (src == '.' or src.startswith('./')):
                    problems.append('plugins[%d].source %r must start with ./' % (i, src))
                if isinstance(src, str) and '..' in src:
                    problems.append('plugins[%d].source contains ..' % i)
                if not e.get('name') or not NAME_RX.match(e['name']):
                    problems.append('plugins[%d].name invalid' % i)
    sk = os.path.join(root, 'skills')
    for d in sorted(os.listdir(sk)) if os.path.isdir(sk) else []:
        p = os.path.join(sk, d, 'SKILL.md')
        if not os.path.isfile(p):
            continue
        fm = front_matter(read(p))
        if not fm:
            problems.append('skills/%s/SKILL.md has no front matter' % d)
        elif fm.get('name') != d or not fm.get('description'):
            problems.append('skills/%s/SKILL.md: front matter needs name: %s and a description' % (d, d))
    return problems


def write(root):
    p = os.path.join(root, 'skills', 'playbook-rules', 'SKILL.md')
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(render(read(os.path.join(root, 'AGENTS.md'))))
    return p


def self_test():
    results = []

    def ok(cond, what):
        results.append(bool(cond))
        print(('  ok   ' if cond else '  FAIL ') + what)

    ok(plugin_paths('run `tools/flowcheck.py` and `skills/lifecycle`') ==
       'run `${CLAUDE_PLUGIN_ROOT}/tools/flowcheck.py` and `${CLAUDE_PLUGIN_ROOT}/skills/lifecycle`', 'repo paths get the plugin root')
    ok(plugin_paths('`AGENTS.md` and `python x`') == '`AGENTS.md` and `python x`', 'non-repo code spans untouched')
    ok(plugin_paths('`python example/run_e2e.py`') == '`python ${CLAUDE_PLUGIN_ROOT}/example/run_e2e.py`', 'python <path> spans too')
    ok(check(REPO) == [], 'this repository: rules skill in sync, manifests and skills valid')
    with tempfile.TemporaryDirectory() as d:
        for n in ('AGENTS.md', 'CLAUDE.md', '.claude-plugin', 'skills'):
            s = os.path.join(REPO, n)
            (shutil.copytree if os.path.isdir(s) else shutil.copy)(s, os.path.join(d, n))
        with open(os.path.join(d, 'AGENTS.md'), 'a', encoding='utf-8') as fh:
            fh.write('\n## Routing -- which skill\n| x | y |\n')
        probs = check(d)
        ok(any('CLAUDE.md differs' in p for p in probs), 'mutation: AGENTS.md edited alone -> CLAUDE.md drift found')
        ok(any('out of sync' in p for p in probs), 'mutation: rules text changed -> playbook-rules drift found')
        mp = os.path.join(d, '.claude-plugin', 'marketplace.json')
        doc = json.loads(read(mp))
        doc['plugins'][0]['source'] = 'plugins/x'
        doc['owner'] = {}
        with open(mp, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        probs = check(d)
        ok(any('must start with ./' in p for p in probs), 'mutation: relative source without ./ found')
        ok(any('owner' in p for p in probs), 'mutation: missing owner.name found')
    passed = all(results)
    print('self-test: ' + ('PASS' if passed else 'FAIL'))
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.write:
        print('wrote %s' % write(REPO))
    probs = check(REPO)
    for p in probs:
        print('FAIL ' + p)
    print('plugin-check: %s' % ('CLEAN' if not probs else '%d finding(s)' % len(probs)))
    return 1 if probs else 0


if __name__ == '__main__':
    sys.exit(main())
