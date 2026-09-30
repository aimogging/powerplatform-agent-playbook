---
name: deploy-headless
description: Deploy cloud flows and canvas apps into a DEV/TEST environment through the APIs (Flow Web API; BAP package import for apps), with a plan/confirm/apply manifest orchestrator, draft connection references, embedded schema and flow-signature refresh, and a runtime NULL-rule gate. Use when iterating on apps/flows in a dev tenant, when a headless-deployed app shows "Not connected", when an import answers InternalServerError, or when writing a deployment manifest. Production still receives manual imports.
---

# Headless deploy (dev tenant)

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
- The Power Apps-audience token route in Python is UNVERIFIED (reference/dev-tenant-auth.md); every protocol step above
  was MEASURED with a different implementation. Treat the first live run as verification and read every line.
- The app shell (data sources + flows added once in Studio, then "Download a copy") is still required: the document's
  data-source entries are minted by the tenant (P-08).
- Package import cannot carry connections or edited export manifests (D-07). Direct `/apps` writes are dead (D-06).
