---
name: lifecycle
description: The whole development lifecycle of a canvas app + cloud-flow solution, phase by phase -- requirements, data model, UX mocks, architecture, develop, check, package, dev-tenant test harness, publish gate, drive and test, screenshots and user docs, handoff and production run, maintenance -- with entry/exit criteria, artifacts, tools, gates and the traps that bite in each phase. Read this FIRST for any new solution or any change bigger than a one-line fix; the other skills are the detail for individual phases.
---

# Lifecycle

> Paths here are relative to the playbook root: the repo root when this repo is your project (template), or
> `${CLAUDE_PLUGIN_ROOT}` when it is installed as a Claude Code plugin -- e.g. `python ${CLAUDE_PLUGIN_ROOT}/tools/flowcheck.py`.

**The deliverable is an importable `.msapp` plus cloud-flow packages that a person imports into another environment.**
The dev tenant and its headless deploys are a TEST HARNESS for automated functional testing -- never the delivery
mechanism. Two corollaries:

- **Build once, test it, ship that.** The package handed over comes from the same sources and the same build that
  passed dev-tenant testing; only the declared tenant values (site URL, recipients) differ between the dev build and
  the handoff build, and nothing else is edited after testing.
- **No dev scaffolding in the handoff.** Http-trigger twins, fixtures, dev connection names and dev tenant ids stay
  in the dev tenant. Build handoff packages with `--handoff <dev config>` so `tools/leakcheck.py` refuses any leak.

Each phase: **Entry** (what must exist) · **Do** · **Artifacts** · **Gate / exit** · **Traps**.

## 1 Requirements and analysis
- Entry: a request.
- Do: ask the crisp questions -- what triggers it, where the data lives, who calls what (app, schedule, list change),
  licensing (must it stay standard? then no premium connector on the app), who imports it and into what environment,
  what the person can and cannot do there (no scripts? no DevTools?). Measure existing data with a read-only census
  (`python -m devtenant census`) before sizing anything.
- Artifacts: a short requirements note with the answers and the census JSON.
- Gate: every open question answered or explicitly assumed. Traps: X-07, X-08, C-41.

## 2 Data model design
- Do: lists, columns (internal name without spaces, display name as users see it), Choice values, Lookups, indexes
  on filtered columns, the 5000-item view threshold, which columns flows filter on (never multi-line text).
- Artifacts: `sharepoint/<name>.schema.json` (drives both the dev provisioner and the generated provisioning flow).
- Gate: schema reviewed; names checked against mangling. Traps: S-01, S-02, S-07, C-20, C-22.

## 3 UX mocks (design gate)
- Do: a static mock of every screen in the app's design language -- `tools/canvas-render/render-screen.py` over draft
  `.pa.yaml` (`--theme-file` for the palette, `--png` for images, `--set` for states such as an open dialog), with
  deliberately long sample text to prove truncation. Design rules: single-line text truncates with an ellipsis and
  shows the full text in a tooltip; working dialogs are sized as work surfaces; spacing on one scale.
- Gate: **the user approves the mocks before any canvas change.** Show renders only at design gates, never after
  implementation (the real app is then the artifact). Traps: C-30 (properties the mock must not use).

## 4 Architecture
- Do: decide what lives in Power Fx vs. flows (writes that need a privileged connection or run long -> flows);
  app-called flows (one JSON payload in, `result_json` out) vs. list-triggered flows (trigger condition, concurrency);
  respond-early for anything near 2 minutes; the LLM gateway call if any; premium avoidance.
- Artifacts: a one-page design: flows and their contracts, data flow, which connector each flow needs (the app will
  carry them -- C-40).
- Gate: every flow contract written down. Traps: C-14, C-40, C-41, F-04, F-14, L-15.

## 5 Develop
- Do: canvas YAML (`canvas-authoring`), flow WDL (`cloud-flow-authoring`), attested shapes only; render screens as you
  go for a fast visual check (layout only -- behaviour is proven later).
- Artifacts: `Src/*.pa.yaml`, `flows/<Flow>/definition.json` + `flow.json`. Traps: the C and F sections.

## 6 Lint and check offline
- Do: `flowcheck.py`, `canvas-lint.py --strict`; every new checker rule gets a mutation test.
- Gate: 0 errors -> tier 0. Traps: X-04.

## 7 Package
- Do: `build-flow-package.py` per flow; `msapp-tool.py stamp` of the Src into a Studio-saved base (bindings come from
  the tenant). The same step produces the dev-tenant build now and the handoff build in phase 12.
- Traps: C-17, P-01, P-02, P-08.

## 8 Dev-tenant deploy = the test harness
- Do: `deploy.py` (plan, confirm, apply): provision -> flows (update in place) -> app (signature and schema refresh,
  connection binding, draft references) -> publish. This exists to TEST the build end to end; it delivers nothing.
- Traps: D-04..D-12, C-39.

## 9 Publish gate
- Do: the GATE step -- launch `packageStatus` must be `Ready`, compiled runtime JS must hold 0 NULL rules.
- Gate: pass -> the app is testable. Traps: C-01..C-10, D-13, D-14.

## 10 Drive and test
- Do: call flows through Http twins, read run history and Respond inputs, drive the published app in a browser
  (`devtenant.appdriver`), read App checker (SARIF from a downloaded copy), approvals without clicks; seed tagged
  fixtures and clean them up; cap LLM-token tests.
- Gate: the functional checklist passes -> tier 1. Traps: F-12..F-15, L-06, L-11.

## 11 Screenshots and user docs
- Do: screenshots of the REAL published app with Playwright (mouse parked, representative data, no tenant values in
  frame), then HTML user guides: a quick tour first, then task-by-task steps; dark theme unconditionally; copy buttons
  on every code block (`tools/add-copy-buttons.py`).
- Gate: docs contain no tenant identifiers (scrub before committing).

## 12 Handoff = the actual delivery
- Do: rebuild the SAME sources with the target's values (`example/build.py --config <target config> --base <target
  base> --handoff config/environment.json` pattern), give the person numbered import steps (Update vs. Create as new,
  connection slots, Save as -> Replace existing, publish) and the test checklist with expected results; they run it.
- Gate: their report -> tier 2. Report per artifact which tier it reached. Traps: P-02..P-05, X-01.

## 13 Iterate and maintain
- Do: update in place (flow GUIDs and app ids are identity); after a flow trigger/Respond change refresh the app's
  flow signature; after a schema change refresh the data source; keep dev and production schemas identical; roll back
  through app Versions -> Restore -> Publish, flows through the previous package.
- Traps: C-02, C-03, C-39, F-15, F-16, X-05.

## The Contoso demo through the lifecycle

1-2: `example/sharepoint/helpdesk.schema.json` · 3: `python tools/canvas-render/render-screen.py
example/canvas/Src/scrHelpDesk.pa.yaml --png out/helpdesk.png` · 4: two flows -- app-called submit, list-triggered
notify (`example/README.md` table) · 5-6: `example/flows/*`, `example/canvas/Src/*`, `python example/build.py --check`
· 7: `python example/build.py --base <download>` · 8-10: `python tools/deploy.py --manifest example/deploy.manifest.json`,
then `flow-invoke`, `flow-runs`, the browser driver · 12: `example/README.md` Part A · 13: re-run 7-10 after each change.

All of 2 and 6-12 in one command, with verified cleanup -- also the first-run check of a new tenant:
`python example/run_e2e.py --template-msapp <Studio-saved .msapp>` (stage table with tiers; `--from`, `--keep`,
`--only cleanup`). Its handoff stage rebuilds the flow packages with TARGET values from the same sources and proves
they differ from the tested build only in the declared tokens, and that the dev shell is refused as a handoff base.
Its studio-import and flow-import stages then do phase 12 the way the person will -- in the dev tenant, through the
maker portals in a browser (`devtenant/portal.py`): open the `.msapp` in Studio, App checker, Save as > Replace
existing, Publish, play; Import Package (Legacy), pick connections, Turn on, run. A green API deploy never proves
importability (P-02, P-10); these two stages do.
