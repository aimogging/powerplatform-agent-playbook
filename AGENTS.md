# AGENTS.md -- Power Platform agent playbook

You develop and test **canvas Power Apps** and **Power Automate cloud flows**, backed by SharePoint and Microsoft 365
connectors, sometimes calling an LLM gateway. Two environments, two different jobs:

- **Dev/test tenant** (commercial Microsoft 365): yours to automate -- provisioning, headless deploys, runs, fixtures.
- **Production tenant**: locked down. A person imports the packages you build, by hand, and reports back. You never
  touch it and no script runs there.

`CLAUDE.md` is an identical copy of this file for agents that read that name. Edit both together.

## Non-negotiable rules

1. **Never invent** a connector, operationId, parameter, control property, template version or WDL function. Use
   `reference/attested-connector-shapes.md`, controls already in the app, or a shape read from the tenant (designer
   Peek code, connector swagger, a probe flow). Say "unattested" when you cannot.
2. **Check before you hand over.** Flows: `tools/flowcheck.py`. Canvas: `tools/canvas-lint.py`. Packages come only
   from `tools/build-flow-package.py` and `tools/msapp-tool.py`, which run those checks.
3. **Report the tier reached** for every artifact: offline-checked / dev-tenant proven / production proven (reported
   by the person). Never present a lower tier as a higher one. Mark UNVERIFIED claims as such.
4. **Ask before guessing requirements**: trigger, data source, who calls what, licensing (premium or not). One or two
   crisp questions beat a wrong build.
5. **Standard licensing by default**: no premium connector referenced by an app; built-in HTTP inside an app-called
   flow is fine (trap C-41).
6. **Identity is stable**: update flows and apps in place; never delete-and-recreate (apps bind flows by GUID); a
   revision is imported with *Update*, never *Create as new*.
7. **No tenant data in the repo**: no GUIDs, hostnames, emails, tokens, names. Tenant values live in the git-ignored
   `config/environment.json`; caches and logs under `~/.pp-playbook/`. Run `tools/scrub-check.py` before committing.
8. **Dev-tenant tools never target production.** They are the agent's instruments, not deliverables.
9. **Measure, don't infer**: size any migration or cleanup from a read-only census, not from old logs.
10. **Never pipe a test into `tail`/`head` before a commit decision** -- check the real exit code.

## Routing -- which skill

| The task | Skill |
|---|---|
| Write/fix a canvas app (Power Fx, controls, screens, data, flow calls) | `skills/canvas-authoring` |
| Get YAML into a `.msapp` a person imports; "my edits don't show" | `skills/canvas-packaging` |
| Write/fix a cloud flow definition (WDL, connectors, expressions) | `skills/cloud-flow-authoring` |
| Build a flow import package + import steps | `skills/cloud-flow-packaging` |
| Prove it works; read App checker / run history; diagnose a symptom | `skills/verify` |
| Model calls from flows/apps, tool loops, RAG, token budgets | `skills/llm-from-flows` |
| Dev-tenant sign-in, SharePoint provisioning/fixtures, running flows, approvals, browser tests | `skills/dev-tenant-automation` |
| Deploy flows + apps to the dev tenant through the APIs | `skills/deploy-headless` |

Symptom-first lookup: `reference/platform-traps.md` (140 traps, each with symptom, cause, fix and proof).

## The loop

```
 1 AUTHOR    flows/<Flow>/definition.json, canvas Src/*.pa.yaml, SharePoint schema JSON
 2 CHECK     flowcheck, canvas-lint (offline, seconds)                          -> tier 0
 3 DEV TEST  deploy.py (provision -> flows -> app -> publish -> runtime gate),
             drive flows (Http twin), drive the app (Playwright), read runs,
             clean fixtures                                                   -> tier 1
 4 PACKAGE   build-flow-package.py -> <Flow>.zip ; msapp-tool.py stamp -> <App>.msapp
 5 HANDOFF   numbered import steps + a test checklist with expected results
 6 PROD      the person imports (Update, pick connections, turn on; Save as ->
             Replace existing, publish), runs the checklist, reports      -> tier 2
 7 REPORT    what passed where, what is unverified, what to watch
```

Loop back from 3 or 6 with the exact error text. A failure in production that the dev tenant did not show is usually
an importer difference (P-02), a schema/policy difference (C-02, C-28, L-15), or an old build still running (X-05).

## One-time setup per app

The first time an app exists, a person (or you, in the dev tenant) creates the shell in Studio: blank app, screens
named as in the YAML, data sources and flows added, Save, *Download a copy*. That file is the **base** every build is
stamped into (bindings are minted by the tenant; P-08). Redo it when screens or data sources change.

## Repository map

```
AGENTS.md / CLAUDE.md   this file (mirror)
README.md               for humans
skills/<name>/SKILL.md  procedures (YAML front matter: name, description)
reference/              platform-traps, attested-connector-shapes, dev-tenant-auth, glossary
tools/                  Python tools (README.md lists them); every one has --self-test
config/                 environment.example.json (copy to environment.json, never commit it)
example/                Contoso Help Desk: invented end-to-end demo (README walkthrough)
docs/                   optional HTML for humans (dark, copy buttons)
```

## Working conventions

- Run `python tools/run-self-tests.py` after changing any tool; add a mutation test with every new checker rule.
- Deliverables for people: numbered, copy/pasteable steps; commands run from the repo root; each step says what
  success looks like. Human-facing docs are HTML (dark theme unconditionally, `tools/add-copy-buttons.py` on every page
  with code blocks); working notes can stay Markdown.
- Canvas: batch related changes, then iterate on App checker output. Flows: precise definitions plus a designer-build
  fallback, because nothing checks them before import.
- Once a plan is approved, move between its phases without asking again; still confirm anything destructive or out
  of plan.
- Prefer one worker at a time over wide parallel fan-outs (rate limits killed several parallel edits mid-way); if a
  worker dies, resume it and re-check its working tree.
- Edit files with a file-writing tool, not regex/sed pipelines (X-03). Commit only when asked; never push anywhere
  unless asked.
- Live tests that spend LLM tokens: state the cost and cap the number of questions first (L-11).
