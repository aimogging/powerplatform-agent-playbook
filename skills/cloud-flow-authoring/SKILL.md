---
name: cloud-flow-authoring
description: Author or fix a Power Automate cloud flow as a Workflow Definition Language (WDL) definition.json -- triggers, connector actions, @-expressions, app-called flows with Respond-to-PowerApp, list-triggered flows, SharePoint/Outlook/Teams/Approvals actions. Use whenever writing or editing a flow definition, choosing a connector operation, writing an expression, or debugging InvalidTemplate, import failures, runs stuck in Running, or double-firing triggers.
---

# Cloud-flow authoring (WDL)

There is NO compiler for cloud flows. Import is slow and run-time errors appear only after a trigger fires. So:
attested shapes only, `flowcheck.py` always, and say what you could not verify.

## Non-negotiables

1. **Never invent a connector, operationId, parameter name or expression function.** Use
   `reference/attested-connector-shapes.md`; otherwise get the shape from the designer's Peek code in the target
   tenant or a probe flow + the connector swagger (F-18). WDL functions: only those in `flowcheck.py`'s list
   (Microsoft's function reference). `createObject()` and `filter()` do not exist (F-06, F-07).
2. **Run `python tools/flowcheck.py <flow dir>`** until 0 errors; read the warnings.
3. **Report verification status** per flow: checked offline / deployed to a dev tenant / imported and run in the
   target. A green API deploy does not prove importability (P-02).

## Source layout (this repo's convention)

```
flows/<FlowName>/definition.json   {"properties": {"displayName": "<FlowName>", "definition": {...}, "connectionReferences": {...}}}
flows/<FlowName>/flow.json         {"displayName", "description", "connectors": {"shared_x": {"displayName"}}}
```

Display name = folder name = package file name, compact, no spaces; the one in `definition.json` is the one that
counts (F-16). Never keep a hand-edited `definition_pretty.json` as the source.

## App-called flows (PowerApp trigger)

- ONE input: `payload`, a JSON string (F-04). Parse once at the top:
  `@json(if(empty(triggerBody()?['payload']), '{}', triggerBody()?['payload']))`.
- Every trigger input: `x-ms-dynamically-added: true` + `description` (F-03). No `["string","null"]` types (C-04).
- Respond outputs: `{"title", "x-ms-dynamically-added": true, "type": "string"}` (F-02). Return ONE `result_json`
  string with a `status` the app switches on (`ok` / `error` / `started`).
- Answer on failure too: a second Respond after a Scope's `Failed/TimedOut/Skipped`, or Respond with all four states
  in `runAfter` (flowcheck warns otherwise). Otherwise the app waits ~2 minutes for nothing.
- Work longer than ~2 minutes: Respond early (`status: started`), then continue and write the result somewhere the app
  can poll (C-14).
- Caller identity: the platform headers `x-ms-user-email` / `x-ms-client-principal-name`, never a payload field.
- Blank app inputs arrive as `""`: `if(empty(x), default, x)`, not `coalesce` (F-01).
- Remember the flow runs its connections as the CALLER (C-40) and must not need a premium connector (C-41).

## List-triggered flows

- `GetOnUpdatedItems` + `splitOn: @triggerOutputs()?['body/value']` + a trigger CONDITION on a status column; the
  flow's own write fires it again -- the condition makes that run a no-op (F-14).
- `runtimeConfiguration.concurrency.runs: 1` when two runs on one item would conflict; for claims, etag + `IF-MATCH`;
  re-read the item before acting.
- After any update (import or API) the trigger re-baselines: test with a fresh change (F-15).

## Connector hygiene

- `retryPolicy` exponential, count 3, PT10S..PT1M on every connector action (F-12).
- No `inputs.authentication` on OpenApiConnection actions; every connector used is in `connectionReferences` (P-02).
- SharePoint: OData v3 filters (S-01); guard values SharePoint cannot store (S-02); read `ListItemEntityTypeFullName`
  (S-06); items via PostItem/PatchItem, lists/fields/folders via HttpRequest (S-09); server- vs library-relative paths
  (S-05); folder creation is single-level (S-04); CopyFolder is flat (S-03).
- Mail/groups/Teams/approvals facts: traps M-01..M-06.

## Expressions

- Membership test: Select -> `join(body('Select'), '|')` -> `contains(...)` (F-07). Empty array: `json('[]')` (F-05).
- `SetVariable` never reads its own variable (F-08). An action references only actions on its runAfter path (F-09).
- Keep each expression under 8192 characters (F-10); no Terminate inside loops (F-11).
- `first()`/`skip()` with `if()` guards instead of `[0]`; never `take(x, 0)` (F-20). Write UTC with `Z` (F-21).
- Inside a single-quoted expression, a literal apostrophe is doubled (`''`).

## Output contract when you deliver a flow

1. The definition (source folder) + `flowcheck` result.
2. The package (`cloud-flow-packaging`).
3. A designer-build fallback: numbered steps -- trigger, each action with the exact field values and expressions --
   for someone who cannot import.
4. What to verify in the designer: connections to pick, anything unattested, the first run to watch.

Examples: `example/flows/`.
