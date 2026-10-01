---
name: cloud-flow-packaging
description: Build a legacy import package (.zip) from a cloud-flow source folder and hand it over with exact manual import steps (Update vs Create as new, connection slots, turning it on). Use whenever a flow must reach a tenant by import, when an import fails (MissingPackageManifest, WorkflowRunActionInputsInvalidProperty, host.connectionReferenceName missing), or when a re-import created a duplicate flow.
---

# Cloud-flow packaging for manual import

> Paths here are relative to the playbook root: the repo root when this repo is your project (template), or
> `${CLAUDE_PLUGIN_ROOT}` when it is installed as a Claude Code plugin -- e.g. `python ${CLAUDE_PLUGIN_ROOT}/tools/flowcheck.py`.

```
python tools/flowcheck.py flows/<FlowName>
python tools/build-flow-package.py flows/<FlowName> --out dist/<FlowName>.zip [--template <export.zip>]
```

`build-flow-package.py` refuses a definition that fails `flowcheck`, writes the verified legacy layout (P-01), derives
stable GUIDs from the flow name (rebuilds are identical), and never packages `definition_pretty.json`. Tenant values
(site URL, recipients) are substituted at BUILD time from `config/environment.json` (see `example/build.py`) so the
committed source stays tenant-free.

`--template`: an exported package from the target tenant; its api/connection resources (with the tenant's icon URIs)
are copied instead of synthesized. Without it the resources carry no `iconUri`; such a package (no template) imported
through the portal in a validation tenant (MEASURED by `example/run_e2e.py` stage flow-import). The folder under
`Microsoft.Flow/flows/` is named by the flow's resource key -- two different GUIDs hang the importer forever (P-10).

## Import rules the importer enforces (P-01, P-02)

- `Microsoft.Flow/flows/manifest.json` must exist (else `MissingPackageManifest`).
- No `inputs.authentication` on any OpenApiConnection action (`WorkflowRunActionInputsInvalidProperty`).
- Every connector an action names must be in `properties.connectionReferences`
  (`Property 'host.connectionReferenceName' is missing`).
`flowcheck` fails all three. The API deploy path accepts the second -- a dev-tenant API deploy is not proof.

## Handoff steps (paste these to the person importing)

1. Power Automate -> **My flows -> Import -> Import Package (Legacy)** -> upload `<FlowName>.zip`.
2. For the flow resource: **first time -> Create as new; every later revision -> Update** and pick the existing flow.
   Never Create as new for a revision: two list-triggered copies both fire, and a re-created app-called flow gets a new
   GUID the app does not know (P-03, C-39).
3. For each connection slot: **Select during import** -> pick the existing connection (or create it once).
4. Import; wait for "All package resources were successfully imported". Open the flow -> **Turn on** (an imported
   flow arrives off, P-12). If the page stays at "Importing your package" for more than a few minutes, the package
   is malformed (P-10) -- cancel and send the agent the package name.
5. First run: for a list trigger, make a FRESH change to an item (the import re-baselined the trigger, F-15); for an
   app-called flow, call it from the app. Check the run history (see `verify`).
6. If the flow is app-called and its trigger inputs or Respond outputs changed: in Studio, Power Automate pane -> the
   flow -> Refresh, then save and publish the app (C-03).

## A solution instead?

Solutions (`pac solution import`, maker portal Solutions -> Import) also carry flows and apps, with connection
references and environment variables. This playbook's proven handoff is the legacy package; if the owner mandates
solutions, build and test that path in the dev tenant first -- it is UNVERIFIED here.
