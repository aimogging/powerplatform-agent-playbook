---
name: verify
description: Verify canvas apps and cloud flows in three tiers -- offline checkers, a dev/validation tenant, and the human-run production check -- and report exactly what was proven where. Use before claiming anything works, when reading App checker output or flow run history, when a symptom needs a diagnosis (black screen, dead button, Not connected, run stuck, flow never fired), and when writing the test checklist for a person.
---

# Verify

> Paths here are relative to the playbook root: the repo root when this repo is your project (template), or
> `${CLAUDE_PLUGIN_ROOT}` when it is installed as a Claude Code plugin -- e.g. `python ${CLAUDE_PLUGIN_ROOT}/tools/flowcheck.py`.

## The reporting rule

Every deliverable states, per artifact, the highest tier it passed:

| Tier | Means | How |
|---|---|---|
| 0 offline | checkers green | `flowcheck`, `canvas-lint`, `msapp-tool lint`, self-tests |
| 1 dev tenant | deployed and exercised in a test tenant | `deploy-headless`, `dev-tenant-automation` |
| 2 target | imported and run by a person in the real tenant, result reported back | the checklist below |

Never present tier 0 or 1 as tier 2 (X-01): the importer is stricter than the API (P-02), schemas and policies differ
(C-02, C-28, L-15). Say what is unverified and what the person must look at.

## Tier 0 -- offline (free, always)

```
python tools/run-self-tests.py                      # the tools themselves
python tools/flowcheck.py <flow dirs>               # every flow
python tools/canvas-lint.py <Src dir> --strict      # every app
python tools/msapp-tool.py info <app.msapp>         # packed.json present? App checker errors inside a base?
```

When you add a checker rule, mutation-test it: a deliberately broken input must fail (X-04).

## Tier 1 -- dev tenant (automated; see dev-tenant-automation and deploy-headless)

- Deploy through `tools/deploy.py`; the GATE step requires runtime `packageStatus=Ready` and **0 NULL rules**
  (C-01..C-10 are invisible anywhere else).
- Drive flows headless (`flow-invoke` through an Http twin), read runs (`flow-runs`, `flow-run`), check list rows.
- Drive the published app in a browser (`devtenant.appdriver`) for click-through tests and screenshots.
- Answer approvals without a click (`approve`).
- Clean up tagged fixtures. Cap any test that spends LLM tokens (L-11).

## Tier 2 -- the person in the target tenant

Give them numbered steps with the expected result of each, e.g. `example/README.md` A7. Ask them to report per step:
pass/fail, the exact error text, and for flows the run's failed action name. They may have no DevTools: import errors
(`PA2xxx`) are copyable text in the import dialog; App checker can be screenshotted.

## Reading the evidence

**App checker.** Two capture routes: the panel's outerHTML -> `tools/parse-appcheck.py` (never read the raw HTML);
or save the app in Studio, download it, `tools/appchecker-sarif.py <app.msapp>` (the SARIF is the only
machine-readable copy; Solution Checker merely echoes it). Errors CASCADE from one root: an unresolved data source or
flow types something as Error, then every `.field`, comparison and Collect fails. Fix the least-frequent / first
message (e.g. `'Run' is an unknown function` = the flow is not a data source). Runtime errors (e.g. `Index` on an empty
table) never appear in any checker.

**Flow run history.** Power Automate -> the flow -> 28-day run history -> the run: the first red action and its
inputs/outputs. For an app-called flow, open the Respond action's INPUTS to see exactly what the app received.
Headless: `python -m devtenant flow-run <flow> <runId>`.

## Symptom -> first suspects

| Symptom | Look at |
|---|---|
| Black/unthemed until "Run OnStart" | C-01/C-02 NULL OnStart; OnStart parse error (C-11) |
| Button does nothing, no error | NULL rule (C-01, C-02, C-03, C-09) -- runtime gate or `app-launch-check` |
| Publish OK, app never loads / "couldn't be published" | package hang (C-05..C-08), client version (C-10) |
| Imported app shows none of my edits | no `packed.json` (C-17) |
| Studio refuses to open the package | YAML (C-15/C-16), YAML-only screen (C-18) |
| `PA2108 Unknown property` | C-30 |
| Flow "Not connected" / FlowNotFound | GUID changed (C-39), headless binding layers (D-10), draft opened before publish (D-09) |
| `InvokerConnectionOverrideFailed` | app lacks a connector the flow uses (C-40) |
| Users get `InsufficientPlanForApp` | premium reference on the app (C-41) |
| Flow returns nothing to the app / app waits ~2 min | no Respond on failure (F-02 flowcheck warning), long flow (C-14) |
| `'field' isn't recognized` after `.Run()` | Respond output lost its flag (F-02), stale signature (C-03) |
| Designer "dynamicallyAddedInfo" on Save | trigger input flags (F-03) |
| Import `WorkflowRunActionInputsInvalidProperty` / `connectionReferenceName` | P-02 |
| Run "Running" for hours | 5xx under default retry (F-12, S-02) |
| List flow ran twice / never for a change | own/other writes (F-14), re-baselined trigger (F-15) |
| Blank default instead of the default value | `coalesce` on `""` (F-01) |
| `InvalidTemplate` at save | runAfter path (F-09), unknown function (F-06/F-07) |
| `InvalidTemplate` at run | zero-arg `createArray()` (F-05), SetVariable self-reference (F-08) |
| Outlook action fails on a team address | it is a group (M-01) |
| LLM step "succeeds" with garbage | HTTP 200 error body (L-06), fenced JSON (L-01) |

When a re-import "broke" something that used to work, first confirm which build was actually running (X-05).
