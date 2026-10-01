---
name: playbook-rules
description: The non-negotiable rules, skill routing and 13-phase lifecycle of the Power Platform agent playbook. Load FIRST for any work on canvas Power Apps, Power Automate cloud flows, SharePoint lists behind them, .msapp or flow import packages, or a Power Platform dev/test tenant -- before writing formulas, flow definitions, packages or tenant scripts.
---

<!-- GENERATED from AGENTS.md by tools/plugin-check.py --write. Do not edit by hand. -->

# Power Platform agent playbook -- rules (plugin form)

Installed as a Claude Code plugin, the playbook lives at `${CLAUDE_PLUGIN_ROOT}`; every path below points there.
Run its tools with that prefix (`python ${CLAUDE_PLUGIN_ROOT}/tools/run-self-tests.py`). Tenant values go in
`~/.pp-playbook/environment.json` (or set `PP_PLAYBOOK_CONFIG`), never inside the plugin folder -- an update
replaces it. Used as a template instead, the same text is `AGENTS.md` at the repo root.

**The deliverable is a functional, importable `.msapp` plus cloud-flow packages that a person imports into another
environment.** Headless deployment to the dev tenant exists ONLY as a test harness for automated end-to-end and
functional testing -- never as the delivery mechanism. Build once, test that build, ship that build; no dev-tenant
scaffolding (test twins, fixtures, dev ids) may reach the handoff (`--handoff` builds refuse it).

## Non-negotiable rules

1. **Never invent** a connector, operationId, parameter, control property, template version or WDL function. Use
   `${CLAUDE_PLUGIN_ROOT}/reference/attested-connector-shapes.md`, controls already in the app, or a shape read from the tenant (designer
   Peek code, connector swagger, a probe flow). Say "unattested" when you cannot.
2. **Check before you hand over.** Flows: `${CLAUDE_PLUGIN_ROOT}/tools/flowcheck.py`. Canvas: `${CLAUDE_PLUGIN_ROOT}/tools/canvas-lint.py`. Packages come only
   from `${CLAUDE_PLUGIN_ROOT}/tools/build-flow-package.py` and `${CLAUDE_PLUGIN_ROOT}/tools/msapp-tool.py`, which run those checks.
3. **Report the tier reached** for every artifact: offline-checked / dev-tenant proven / production proven (reported
   by the person). Never present a lower tier as a higher one. Mark UNVERIFIED claims as such.
4. **Ask before guessing requirements**: trigger, data source, who calls what, licensing (premium or not). One or two
   crisp questions beat a wrong build.
5. **Standard licensing by default**: no premium connector referenced by an app; built-in HTTP inside an app-called
   flow is fine (trap C-41).
6. **Identity is stable**: update flows and apps in place; never delete-and-recreate (apps bind flows by GUID); a
   revision is imported with *Update*, never *Create as new*.
7. **No tenant data in the repo**: no GUIDs, hostnames, emails, tokens, names. Tenant values live in the git-ignored
   `${CLAUDE_PLUGIN_ROOT}/config/environment.json`; caches and logs under `~/.pp-playbook/`. Run `${CLAUDE_PLUGIN_ROOT}/tools/scrub-check.py` before committing.
8. **Dev-tenant tools never target production.** They are the agent's instruments, not deliverables.
9. **Measure, don't infer**: size any migration or cleanup from a read-only census, not from old logs.
10. **Never pipe a test into `tail`/`head` before a commit decision** -- check the real exit code.

## Routing -- which skill

| The task | Skill |
|---|---|
| **Anything new or bigger than a one-line fix: start here** | `${CLAUDE_PLUGIN_ROOT}/skills/lifecycle` |
| Write/fix a canvas app (Power Fx, controls, screens, data, flow calls) | `${CLAUDE_PLUGIN_ROOT}/skills/canvas-authoring` |
| Get YAML into a `.msapp` a person imports; "my edits don't show" | `${CLAUDE_PLUGIN_ROOT}/skills/canvas-packaging` |
| Write/fix a cloud flow definition (WDL, connectors, expressions) | `${CLAUDE_PLUGIN_ROOT}/skills/cloud-flow-authoring` |
| Build a flow import package + import steps | `${CLAUDE_PLUGIN_ROOT}/skills/cloud-flow-packaging` |
| Prove it works; read App checker / run history; diagnose a symptom | `${CLAUDE_PLUGIN_ROOT}/skills/verify` |
| Model calls from flows/apps, tool loops, RAG, token budgets | `${CLAUDE_PLUGIN_ROOT}/skills/llm-from-flows` |
| Dev-tenant sign-in, SharePoint provisioning/fixtures, running flows, approvals, browser tests | `${CLAUDE_PLUGIN_ROOT}/skills/dev-tenant-automation` |
| Deploy flows + apps to the dev tenant through the APIs | `${CLAUDE_PLUGIN_ROOT}/skills/deploy-headless` |

Symptom-first lookup: `${CLAUDE_PLUGIN_ROOT}/reference/platform-traps.md` (144 traps, each with symptom, cause, fix and proof).

## The lifecycle (detail: `${CLAUDE_PLUGIN_ROOT}/skills/lifecycle`)

1. Requirements -- crisp questions; read-only census of existing data.
2. Data model -- list schema JSON; naming, Choice/Lookup, indexes.
3. UX mocks -- render every screen; the user approves before any canvas change.
4. Architecture -- Power Fx vs. flows, flow contracts, respond-early, no premium on the app.
5. Develop -- canvas YAML, flow WDL, attested shapes only.
6. Check offline -- flowcheck, canvas-lint -> tier 0.
7. Package -- flow zips, `.msapp` stamped into a Studio-saved base.
8. Dev-tenant deploy -- the TEST HARNESS (`deploy.py`), never the delivery.
9. Publish gate -- runtime package Ready, 0 NULL rules.
10. Drive and test -- flows via twins, app via browser, run history, fixtures cleaned -> tier 1.
11. Screenshots and user docs -- real app, HTML guides.
12. Handoff = THE DELIVERY -- same build, target values, leak-checked; the person imports and reports -> tier 2.
13. Iterate -- update in place, refresh signatures/schemas, roll back through versions.

First run in a new dev tenant: `python -m devtenant doctor` (from `${CLAUDE_PLUGIN_ROOT}/tools/`, read-only), then `python ${CLAUDE_PLUGIN_ROOT}/example/run_e2e.py`
-- the whole loop on the Contoso demo, with verified cleanup (README "First run").

Loop back from 10 or 12 with the exact error text. A failure in production that the dev tenant did not show is usually
an importer difference (P-02), a schema/policy difference (C-02, C-28, L-15), or an old build still running (X-05).
