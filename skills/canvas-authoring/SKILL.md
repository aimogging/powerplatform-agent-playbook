---
name: canvas-authoring
description: Author or edit a Power Apps canvas app as .pa.yaml source (Power Fx formulas, controls, screens, data sources, flow calls). Use whenever writing or changing a canvas app, debugging a formula, wiring an app to SharePoint or to a cloud flow, or when an app shows a black screen, a dead button, "Not connected", or App checker errors. Carries the Power Fx, YAML and runtime-compiler rules that are invisible to pac and only surface after a slow import.
---

# Canvas authoring

Canvas apps are authored here as **Source Code layout** YAML: `Src/App.pa.yaml` (App-level `OnStart`, `Formulas`)
and one `Src/<screen>.pa.yaml` per screen. Tenant bindings (which list, which flow, which connection) are NOT in the
YAML -- they live in the Studio-saved package the YAML is stamped into (see `canvas-packaging`).

## Before you write anything

- Know the data: list column INTERNAL names, DISPLAY names and TYPES (Choice? Lookup? Text?). Power Fx uses display
  names in single quotes; the built-in Title is always `Title` (C-20, C-22).
- Know every flow's Run() contract: one JSON text input in, `result_json` text out (F-04). The flow must be deployed
  before the app is packed against it (C-39).
- Copy control types/versions from controls that already exist in the app or from the examples here:
  `Label@2.5.1`, `Classic/Button@2.2.0`, `Classic/TextInput@2.3.2`, `Classic/DropDown@2.3.1`, `Gallery@2.15.0`
  (`Variant: Vertical`), `GroupContainer@1.5.0`, `Rectangle@2.3.0`, `ModernButton@1.0.0`, `ModernTextInput@1.1.1`,
  `ModernDropdown@1.0.2`, `HtmlViewer@2.1.0`, `Timer@2.1.0`. Never invent a property name (PA2108 at import, C-30).

## Rules (each is a trap that cost real time; details in reference/platform-traps.md)

YAML
- Any formula containing `: ` (records, `With({x: ...})`, `Patch(..., {Title: ...})`, text like `"Note: x"`) or ` #`
  is a block scalar: `Prop: |-` then `=formula` on the next line, two spaces deeper (C-15, C-16). Spaces only, no tabs.
- Control names are unique across the whole app.

Runtime compiler (NULL rules and hung packages -- nothing reports these but the player)
- Never `UTCNow()` (C-01). Use `Now()` / `TimeZoneOffset(Now())`.
- Never `AccessibleLabel` on `Classic/Button` (C-05); never `Hover*` on Label/Rectangle (C-06); no `LayoutOverflowY`
  on a ManualLayout container (C-07); keep `;` out of string literals in control properties (C-08).
- `Concat(Filter(t, cond), col, sep)` -- not the `As` alias form inside Concat (C-09).
- Every column a formula uses must exist in the EMBEDDED schema; every flow call must match the EMBEDDED signature
  (C-02, C-03). After changing a list or a flow's trigger/Respond, refresh them (Studio, or `deploy-headless`).

Power Fx
- Lookup/Person writes: `{Id: x, Value: y}` (C-21). Choice: `.Value`.
- `Search(T, text, Col1, Col2)` -- bare identifiers (C-23).
- Empty typed table: `Filter(T, false)`, never `[]` in a branch (C-24).
- Empty text test: `IsBlank(x)`, never `Coalesce(x, "") = ""` (C-25).
- Seed every variable with a typed value; never only `Set(v, Blank())` (C-26).
- `JSON()` only of selected text fields, not a whole SharePoint record (C-27).
- Optional flow inputs: avoid them; one JSON text input (C-29, F-04).
- `varTheme`, never `Theme` (C-34). `As` aliases go on the aggregate's table argument (C-35).
- ModernDropdown: `ItemDisplayText: =ThisItem.Value`; `Default` is a record from Items (C-32).
- Fonts: `Font.'Segoe UI'`, `Font.'Courier New'` for monospace (C-31).

Events and boot
- `Select()` is queued; never loop `Set(v, x); Select(worker)` -- let the worker pop its own queue (C-13).
- OnStart seeds state and ends with `Set(varOnStartDone, true)`; the first screen boots from a Timer gated on it, never
  from `OnVisible` + `Select()` (C-12). A parse error anywhere in OnStart means none of it runs (C-11).
- Every `Flow.Run()` inside `IfError(...)`, with a busy flag and a visible error; a synchronous call waits ~2 min
  (C-14). Parse the result: `ParseJSON(MyFlow.Run(JSON({...}, JSONFormat.Compact)).result_json)`.

Bindings and licensing
- Apps bind flows by workflow GUID: update flows in place, never delete-and-recreate (C-39).
- A PowerApp-trigger flow uses the CALLER's connections: the app must carry every connector the flow calls (C-40).
- Never add a premium connector as a data source; built-in HTTP inside a flow is fine (C-41).
- App display names: none of `. \ / : * ? " < > |` (C-42).

## Layout and design (preferences that worked)

- One consistent spacing scale (4/8/12/16), 32-40 px inputs, 40 px list rows; single-line text truncates with an
  ellipsis and exposes the full text in a Tooltip -- never wrap or overflow inside a row.
- Working dialogs (pickers, editors) are sized as work surfaces relative to the screen
  (`Min(1200, Parent.Width*0.62)` x `Min(760, Parent.Height*0.72)`); only confirmations stay small.
- Classic control `Size` is in points (px = pt x 4/3), modern controls in pixels; `canvas-lint` checks text heights.
- Studio REFLOWS `Parent.Width/Height` to the viewport; anchor deliberately.
- Show mockups/renders only at design decision points, before building -- after implementation the real app is the
  artifact.

## Workflow

1. Edit `Src/*.pa.yaml`.
2. `python tools/canvas-lint.py <Src dir> --strict` until clean.
3. Package (`canvas-packaging`) and import; open App checker (`verify`).
4. Big batched canvas changes are fine: App checker gives fast structured feedback. Fix the ROOT error first -- one
   unresolved data source or flow cascades into dozens of rows (X-10).

Example: `example/canvas/Src/`.
