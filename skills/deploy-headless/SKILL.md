---
name: deploy-headless
description: Deploy cloud flows and canvas apps into a DEV/TEST environment through the APIs (Flow Web API; BAP package import for apps), with a plan/confirm/apply manifest orchestrator, draft connection references, embedded schema and flow-signature refresh, and a runtime NULL-rule gate. Use when iterating on apps/flows in a dev tenant, when a headless-deployed app shows "Not connected", when an import answers InternalServerError, or when writing a deployment manifest. Production still receives manual imports.
---

# Headless deploy (dev tenant)

> Paths here are relative to the playbook root: the repo root when this repo is your project (template), or
> `${CLAUDE_PLUGIN_ROOT}` when it is installed as a Claude Code plugin -- e.g. `python ${CLAUDE_PLUGIN_ROOT}/tools/flowcheck.py`.

**This is a test harness, not a delivery route.** It puts the build into a dev tenant so it can be tested end to end
automatically. The deliverable is the importable package built from the SAME sources (`canvas-packaging`,
`cloud-flow-packaging`) with `--handoff` so no dev-tenant value or test scaffolding leaks into it.

```
python tools/deploy.py --manifest <m.json> --plan-only       # PRE (offline) + PLAN (read-only)
python tools/deploy.py --manifest <m.json>                   # ... + confirm + APPLY
python tools/deploy.py --manifest <m.json> --resume-from S1-APPS
python tools/deploy.py --manifest <m.json> --stop-after S1-FLOWS --skip P1 --flows '=MyFlow' --apps none --yes
```

Step table: `P<n>` provisioners -> per stage `<S>-FLOWS`, `<S>-APPS`, `<S>-PUBLISH` -> `GATE`. It stops at the first
failure and prints the exact resume command; a plan with ERROR lines refuses to apply. Log: `~/.pp-playbook/logs`.
Manifest example: `example/deploy.manifest.json`.

## Order and identity (why the manifest rules exist)

1. **Flows before apps**, and flows UPDATED in place: apps bind flows by GUID and embed their Run() signature, both
   read from the live flow at app-deploy time (C-39, C-03). The orchestrator refuses an app staged before a flow it
   `requiredFlows`.
2. **Find by display name** (exact, else the one match ignoring spaces/case, renamed in place; two near-misses stop).
   `=Name` selects exactly. Apps with duplicate names need a pinned `name` (app id) or `owner`.
3. **`mustExist: true`** on list-triggered flows once live: a name miss must never CREATE a second copy (F-14, P-03).
4. **`allowCreate: true`** only for an app's first deploy.

## What the app step does (and why) -- `devtenant/powerapps.py`, `devtenant/canvasdoc.py`

1. Refresh the embedded flow signatures from `listWadl` (D-12) and SharePoint schemas from `$metadata.json` (D-11).
2. Canonicalise flow bindings and rebuild per-flow dependency wiring (D-10); never wire a premium connector and refuse a
   premium data source (C-41); never synthesize an entry without displayName + iconUri (left for Studio instead).
3. Build the package around the environment's MINTED key pair (D-07; minted once by exporting any app, cached in
   `~/.pp-playbook/package-keys.json`), upload, `listImportParameters`, `importPackage` with the re-staged link (D-06).
4. Write the DRAFT's connection references with a leased full-definition PUT (D-09) -- four-part client versions (C-10).
5. Publish (api-version 2017-05-01, D-08); verify the live references; PATCH only as a repair.
6. GATE: the player's launch call must say `Ready` and the compiled JS must hold 0 NULL rules (D-13, D-14).

Never open a `--no-publish` draft in Studio before publishing it: Studio opens the draft and, if it lacks references,
wipes them on its first save (D-09).

## Limits and status

- Connections cannot be created by API; the first connection per connector is made once by hand (D-05, P-04).
- Proven live with THIS Python (validation tenant, `example/run_e2e.py`): create and update-in-place imports, draft
  references, publish, the launch gate (Ready, 0 NULL rules) and deletion. Traps found on the way: D-22 (a create
  stored client version 0.0.0.0 -> package stuck InProgress), D-23 (ISO `retryAfter`, gzip blobs), C-46 (untyped
  variable -> NULL rules). The `deploy.py` manifest wrapper itself has only its offline self-test.
- The app shell: normally a Studio "Download a copy" with the data sources and flows added (P-08). For TESTS it can be
  built without Studio: `canvasdoc.new_shell(<any Studio-saved .msapp>)` takes Microsoft's control templates from it,
  `add_sharepoint_source` / `add_flow_source` add the bindings (list id, connection, `listWadl`), `put_src` the YAML;
  the service compiled such a document to a Ready runtime package and the app ran in the browser. Every control type
  used needs its template in the source document(s) (C-19). The DELIVERY package is still stamped into the target
  tenant's own Studio download.
- Package import cannot carry connections or edited export manifests (D-07). Direct `/apps` writes are dead (D-06).
