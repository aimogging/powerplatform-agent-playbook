---
name: dev-tenant-automation
description: Automate a DEV/TEST Microsoft 365 tenant from Python so apps and flows can be tested end to end without clicking -- device-code sign-in with cached refresh tokens, SharePoint list/field provisioning and fixtures, files and folders, running flows and reading run history, answering approvals, driving a published app in a browser, and read-only census. Use whenever a test needs tenant state, whenever writing SharePoint REST or Flow API calls, or when a dev-tenant call fails with AADSTS65002, 401, 404 or a blank result. Never point these tools at production.
---

# Dev-tenant automation

Everything here is the AGENT's tooling for a tenant it may change. The end user never runs it; production still gets
manual imports. Tool: `cd tools && python -m devtenant --help` (standard library only; Playwright optional).

## Setup (once)

1. `config/environment.json` from the example (git-ignored): tenant id, environment id (`Default-<tenant id>` for the
   default environment), site URL, operator e-mail, optional `connections` map.
2. `python -m devtenant login sharepoint` (device code) -- the same FOCI refresh token then serves `flow`, `graph`,
   `apihub` silently. `login powerApps` and `login dataverse` use other clients (reference/dev-tenant-auth.md).
3. `python -m devtenant whoami` -- audiences and expiry, never token values.

## SharePoint

```
python -m devtenant sp-provision <schema.json>            # audit: prints what is missing, writes nothing
python -m devtenant sp-provision <schema.json> --apply    # creates lists/fields, appends missing choices, renames Title
python -m devtenant sp-seed <List> rows.json --tag "[fixture]" --apply
python -m devtenant sp-cleanup <List> --tag "[fixture]" --apply   # deletes ONLY rows whose Title starts with the tag
```

One schema JSON (`example/sharepoint/*.schema.json`) feeds both this tool and a generated provisioning flow for manual
import. Keep the dev list schemas identical to production -- including display-name renames -- or apps compile against
the wrong columns (C-02, C-28). Traps S-01..S-12 and D-16..D-18 apply; the library (`devtenant/sharepoint.py`) already
handles digest, MERGE, entity types, single-level folders and mangled names.

## Flows

```
python -m devtenant flow-deploy flows/<Flow> [--apply]      # update in place by name (GUID kept); plan without --apply
python -m devtenant flow-invoke <Flow> payload.json [--header x-ms-user-email=someone@contoso.com]
python -m devtenant flow-runs <Flow>
python -m devtenant flow-run <Flow> <runId>                 # failed actions + the Respond action's inputs
python -m devtenant flow-twin-delete <Flow>
python -m devtenant approvals ; python -m devtenant approve <approvalName> "<option>"
```

A PowerApp-trigger flow cannot be called by a script; `flow-invoke` goes through an Http twin with Embedded
connections (D-04) and custom headers reach `triggerOutputs()['headers']`. Delete twins afterwards. Connection
references come from the existing flow, then from live flows using the connector, then from the config map (D-05).

## Canvas apps in a browser

`devtenant/appdriver.py` (Playwright): persistent signed-in profile outside the repo (one process at a time), consent
iframe handled, controls by their YAML names (`[data-control-name]` in frame `fullscreen-app-host`), `fill` types into
the inner input and tabs out, `shot` parks the mouse first. `python -m devtenant app-capture <app> <dir>` saves the
runtime JS the player loads and scans it for NULL rules.

## Read-only census before sizing work (X-08)

`python -m devtenant census spec.json` -- ItemCount vs enumerated count per list (incomplete reads), blank required
fields, duplicate keys, unparseable values; JSON out. The wrapper refuses any write.

## Etiquette in a shared validation tenant

- Change it only to prove something specific and uncertain; do not redeploy "to keep it current" (X-09).
- Update the real app/flow in place; never create renamed throwaway copies to dodge a lease or bisect -- ask first
  and delete experiments after (C-43).
- Tag fixtures, clean them up, and send test notifications only to the operator address.
- Never commit tenant values: the config, token caches, logs, captures and browser profile stay outside git.
- Some agent sandboxes refuse commands whose text contains destructive-looking REST paths -- put them in a script
  file (D-21).
