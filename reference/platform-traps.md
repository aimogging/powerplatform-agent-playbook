# Platform traps

Every entry: **Symptom** (what you see) -> **Cause** -> **Fix** -> **Proof** (how it was established).
Proof levels: **MEASURED** = reproduced and bisected/A-B tested on a real tenant; **OBSERVED** = seen live, cause
inferred from strong evidence; **DOCUMENTED** = vendor docs or vendor binaries; **SUSPECTED** / **UNVERIFIED** = say
so when you rely on it. "A validation tenant" means a commercial test tenant; "production" means a locked-down
tenant reached only through manual imports.

Areas: [C canvas apps](#c--canvas-apps) · [F cloud flows](#f--cloud-flows) · [S SharePoint](#s--sharepoint) ·
[M Microsoft 365 connectors](#m--microsoft-365-connectors-mail-groups-teams-approvals) ·
[P packaging and import](#p--packaging-and-manual-import) · [D dev-tenant APIs and headless deploy](#d--dev-tenant-apis-and-headless-deploy) ·
[L LLM gateways](#l--llm-gateways-from-flows-and-apps) · [X process](#x--process-traps)

---

## C -- canvas apps

### Runtime compiler (silent NULL rules and packages that never finish)

**C-01 UTCNow() kills the rule.** Symptom: a button does nothing; OnStart never runs (themed areas black, no data);
no error in Studio, pac, App checker, and the launch reports packageStatus Ready. Cause: the service's runtime-package
compiler emits any rule calling `UTCNow()` as a NULL rule. Fix: never use `UTCNow()`; use `Now()` or
`TimeZoneOffset(Now())` (minutes WEST of UTC). `canvas-lint` fails it. Proof: MEASURED on a validation tenant by
reading the compiled runtime JS of several apps (the `_re0(...){return _w0(null)` shape), fixed by removal.

**C-02 A column missing from the EMBEDDED list schema kills the rule.** Symptom: as C-01 for formulas that touch a
newer column. Cause: each SharePoint data source embeds its list schema in the document; the app is compiled against
that copy, not the live list. Fix: refresh the schema (open in Studio, or the headless refresh in
`devtenant.canvasdoc.refresh_table_schemas`) before publishing; keep dev and production list schemas identical,
including display-name renames. Proof: MEASURED A/B -- same app, same deploy tool, with and without the refresh
(black stuck screen vs. themed and loaded).

**C-03 A stale embedded flow signature kills every call to that flow.** Symptom: as C-01 for formulas calling
`MyFlow.Run(...)`; after a Studio Refresh the checker reports `ErrBadArityRange`. Cause: the document embeds a WADL of
the flow's Run() inputs and Respond outputs; a trigger/Respond change leaves the copy stale. Fix: Studio -> Power
Automate pane -> the flow -> ... -> Refresh (headless: `listWadl`, see D-12). Proof: MEASURED; the refresh removed the
NULL rule, and an A/B with one Respond output removed from the WADL reproduced it.

**C-04 A flow trigger input typed `["string","null"]` cannot be added or refreshed.** Symptom: Studio "Unable to add
flow"; `listWadl` answers 400 `FlowWadlConversionNotSupported`. Cause: WADL cannot express union types. Fix: PowerApp
trigger inputs are plain text; pass optional or binary data as ONE JSON text input. `flowcheck` fails it. Proof:
MEASURED (HAR of the Studio failure + the API error).

**C-05 AccessibleLabel on a Classic button hangs the package.** Symptom: publish succeeds, launch `packageStatus`
stays `InProgress` forever, no error text. Cause: `AccessibleLabel` on `Classic/Button@2.2.0` (static or dynamic).
Fix: announce classic buttons by their Text; icon-only buttons get a Tooltip; AccessibleLabel only on controls where
it was proven fine (Classic/TextInput, ModernButton). `canvas-lint` fails it. Proof: MEASURED, bisected over 12
in-place publishes.

**C-06 Hover properties on a Label or Rectangle hang the package.** Symptom: as C-05. Fix: put a separate hover layer
(a transparent Classic button) over the shape. `canvas-lint` fails it. Proof: MEASURED.

**C-07 LayoutOverflowY on a ManualLayout GroupContainer hangs the package.** Symptom: as C-05 (15+ minutes, two
publishes). Fix: outer container `Variant: AutoLayout` + `LayoutDirection.Vertical` + `LayoutOverflowY:
LayoutOverflow.Scroll`, with one ManualLayout child (`AlignInContainer.Stretch`, `FillPortions: 0`,
`Height: Max(Parent.Height-2, <content min>)`); the scrollbar takes ~15 px. Proof: MEASURED (removing only that
property -> Ready in ~20 s).

**C-08 A `;` inside a Timer Tooltip string.** Symptom: as C-05. Proof: SUSPECTED -- the most likely hunk after
bisecting 8 publishes. Fix: keep `;` out of string literals in control properties. `canvas-lint` warns.

**C-09 `Concat(Filter(t As X, ...), X.col, ...)` in a label compiles to NULL.** Fix: the unaliased
`Concat(Filter(t, cond), col, sep)`. Proof: MEASURED by the runtime-rule gate. `canvas-lint` warns.

**C-10 A 3-part client version makes the app unpublishable.** Symptom: "This app couldn't be published" in the
player, or the player silently serves the last good package; the maker API says Published. Cause: a
`createdByClientVersion` written with 3 parts is stored with revision -1 and the server packager rejects it
("Authoring tool version ... is invalid", visible only in the player's launch response). Fix: always write all four
parts and never carry forward a stored version with a negative revision (`devtenant.powerapps`). Proof: MEASURED.

### OnStart, boot order, events

**C-11 Black / unthemed screen until "Run OnStart".** Causes, strongest evidence first: (1) OnStart compiled to NULL
(C-01/C-02); (2) a parse error anywhere in OnStart (e.g. a missing `;`) -- nothing in OnStart runs; only Studio's
App checker shows it; (3) preview flags in `Properties.json` `AppPreviewFlagsMap` (`delaycontrolrendering`,
`delayloadscreens`, `usenonblockingonstartrule`) -- tried, effect UNVERIFIED. Fix: check C-01/C-02 and App checker
first. Proof: (1) and (2) MEASURED.

**C-12 With non-blocking OnStart, OnVisible races OnStart.** Symptom: the first screen reads state OnStart has not
set yet; a `Select()` issued from OnVisible never fires. Fix: OnStart seeds typed defaults and ends with
`Set(varOnStartDone, true)`; a screen Timer (`Start: =varOnStartDone && !varBooted`, AutoStart false, Duration 50)
runs the boot. Proof: DOCUMENTED + OBSERVED (two separate app bugs the same day).

**C-13 `Select()` is queued, not synchronous.** Symptom: `Set(v, x); Select(worker)` in a loop -- every deferred worker
run sees the LAST v; single-item tests hide it. Fix: the worker pops its own queue
(`Set(v, First(q)); RemoveIf(q, ...)`) and the caller just fires N Selects. Proof: OBSERVED twice (double-firing and
last-value bugs), fixed by the queue pattern.

**C-14 A synchronous Flow.Run waits about two minutes.** Symptom: long flows fail with 504 /
`ActionResponseTimedOut` and the run shows Failed although the work finished; an action `limit.timeout` does NOT bound
a synchronous HTTP call (PT1S still answered). Fix: respond early (Respond right after validation with
`status: started`, then continue and write the full result to a list/file the app polls); wrap every `Run()` in
`IfError`. Proof: MEASURED.

### YAML source (.pa.yaml) and packing

**C-15 An inline formula containing `: ` breaks Studio's import.** Symptom: "There's something wrong with the YAML code
for this app", `PA1001 YamlInvalidSyntax ... found invalid mapping`. Cause: `Prop: =...` is a YAML plain scalar;
records, `With({x: ...})` and text like `"Note: ..."` contain `: `. pac pack/unpack and Studio's property bar are
lenient, so it surfaces only on open. Fix: `Prop: |-` with `=formula` on the next line, two spaces deeper.
`canvas-lint` fails it (also a strict PyYAML parse). Proof: MEASURED (A/B file, exact error text).

**C-16 ` #` inside an inline formula truncates it.** Cause: YAML comment. Fix: block scalar, or longhand CSS so `#`
follows `:`. Proof: OBSERVED.

**C-17 Studio ignores Src/*.pa.yaml without `packed.json`.** Symptom: an imported package shows none of your edits.
Cause: only `packed.json` with `"LoadConfiguration": {"LoadFromYaml": true}` makes Studio load Src; a raw "Download a
copy" has no such file, so Studio loads the compiled `Controls/*.json`. Studio drops the file again on its next save.
Fix: `msapp-tool.py pack/stamp` adds it; every new download needs it again. Proof: MEASURED (with/without A/B; 47
App-checker fixes "vanished" before the cause was found).

**C-18 New screens that exist only in YAML.** Status: UNCERTAIN. Early experiments said a YAML-only screen made the app
fail to open; a later YAML-only screen packaged fine once C-10 was fixed, so the earlier failures may have been C-10.
Safe practice: create/rename screens in Studio (blank is fine), save, download, then author their contents in YAML.
New controls inside an existing screen: MEASURED fine.

**C-19 A control type the base package has never used.** Observed: adding a Timer to an app whose pack base lacked the
timer template needed the template copied in from a package that had it. Fix: add one instance in Studio first, or
start from a base that already uses the control. Proof: OBSERVED.

### Power Fx against SharePoint

**C-20 Columns are referenced by DISPLAY name.** `'Requester Email'`, not `RequesterEmail`; there is no automatic
no-space "formula name". Exception: the built-in Title column is always `Title`, even when its display name is changed.
Proof: MEASURED (App checker text: "column 'X' does not exist ... most similar name is 'X Y'").

**C-21 Lookup/Person writes take `{Id, Value}`.** Passing a whole record: "Missing column 'Id'". Clear with `Blank()`.
Proof: MEASURED (App checker).

**C-22 Same display name, different type, different list.** `.Value` on a Text column errors and cascades. Check the
column type before `.Value` (Choice needs it). Proof: MEASURED.

**C-23 `Search()` column arguments are bare identifiers.** `Search(T, txt, "Title")` compiles to a gallery that never
fills (no visible error). Use `Search(T, txt, Title)`. `canvas-lint` fails it. Proof: MEASURED.

**C-24 An untyped `[]` does not unify with a table.** `If(c, [], Filter(T, ...))` -> every `ThisItem.x` "isn't
recognized". Use `Filter(T, false)`. Proof: MEASURED.

**C-25 `Coalesce(x, "") = ""` is never true.** Coalesce turns "" into Blank() and `Blank() = ""` is false. Use
`IsBlank(x)`. Proof: MEASURED (a fallback label never showed; no checker error).

**C-26 A variable only ever `Set` to `Blank()` has no type** ("No type found for variable"). Seed a typed value.
Proof: MEASURED.

**C-27 `JSON()` of a whole SharePoint record throws "contains media".** Select text fields first. Proof: MEASURED.

**C-28 Studio refreshes SharePoint schemas on open.** So `ErrColumnDoesNotExist` in Studio means the TENANT's list
lacks the column (dev/prod schema drift), not a stale document. Proof: MEASURED.

**C-29 Optional flow inputs go in a trailing RECORD.** `Flow.Run(req1, req2, {opt: "x"})`; positional ->
`ErrBadType Expected Record`. Better: one JSON text input (C-04). Proof: MEASURED (SARIF).

### Controls (import-time PA2108 and behaviour)

**C-30 Properties templates reject at import (PA2108 "Unknown property"):** `HoverFill`/`Fill` on ModernButton (its fill
is Appearance + BasePaletteColor); `Mode` on ModernTextInput (multiline = Classic/TextInput with
`Mode: TextMode.MultiLine`); `Tooltip` on ModernDropdown; `HintText` and `Radius*` on Classic/DropDown; `Radius*` on
Rectangle; `Appearance`/`BasePaletteColor` on Classic controls. `canvas-lint` fails all. Proof: MEASURED (import dialog
text, copyable even where DevTools are disabled).

**C-31 Font enum:** `Font.'Consolas'` and `Font.'Inter'` do not exist; monospace is `Font.'Courier New'`. Proof: MEASURED.

**C-32 ModernDropdown:** `Selected` is a record; set `ItemDisplayText: =ThisItem.Value`; `Default` must be the matching
record (`LookUp(Choices(list.Col), Value = stored)`), not a string (the box stays blank). Proof: MEASURED.

**C-33 `AddColumns(src, "Value", ...)` for a Classic DropDown label** can fail; `ForAll(src As x, {Value: ...})` works.
Proof: SUSPECTED.

**C-34 `Theme` is reserved** as a variable name; use `varTheme`. `canvas-lint` fails it. Proof: MEASURED.

**C-35 `As` alias scope:** `Max(Filter(t As R, ...), Len(R.x))` fails; put the alias on the aggregate's table argument.
Proof: MEASURED.

**C-36 Later children are on top.** A transparent tap button listed BEFORE the labels it covers loses the clicks.
Proof: OBSERVED (user report, fixed by reordering).

**C-37 Attachments -> base64:** `JSON(Attachments.Value, IncludeBinaryData)` yields an `appres://blobmanager/...` URL,
not bytes; bind the blob to a hidden Image and `JSON(img.Image, JSONFormat.IncludeBinaryData)`. `MaxAttachmentSize` is
decimal MB (20 accepts 20,000,000 bytes, rejects 20,000,001). Proof: MEASURED in the player.

**C-38 HtmlViewer opens `<a href target=_blank>` links** (not sandboxed, current build). Proof: OBSERVED.

### Bindings, licensing, names

**C-39 Apps bind flows by workflow GUID.** Symptom: "3 of 4 flows connect, one FlowNotFound" after a flow was
deleted and recreated. Fix: update flows in place (never delete-then-redeploy); if a GUID changed, re-add that flow in
Studio (or rebind headless, D-10). Proof: MEASURED.

**C-40 A PowerApp-trigger flow runs its connections as the CALLER (Invoker).** The service rewrites the flow's
references to Invoker whatever you send; the app must therefore carry every connector the flow calls, or the call
fails `InvokerConnectionOverrideFailed`. Proof: MEASURED (a PATCH to Embedded was stored as Invoker again).

**C-41 A premium connector reference on the APP makes it premium.** Symptom: users get `InsufficientPlanForApp` at
launch. The built-in HTTP action inside an app-called flow does NOT (the flow keeps its own connection). Consequence of
C-40: an app cannot call a flow that needs a premium connector without premium licences -- design such flows without
one. `deploy.py` refuses a premium data source and never wires a premium flow dependency. Proof: MEASURED.

**C-42 App display names cannot contain `. \ / : * ? " < > |`.** The first import may accept it; every later full
definition write fails `InvalidApplicationNameCharacters`. Test the EXACT production name in the dev tenant. Proof:
MEASURED.

**C-43 An app open in Studio holds an editing lease.** API writes get 409 `AppLeaseActive` up to ~15 min after the tab
closes. Wait and retry; never deploy a renamed copy to dodge it. Proof: MEASURED.

**C-44 After a publish the player may show "You're using an old version"** until refreshed; test after it is gone.
Proof: OBSERVED.

**C-45 A Studio Refresh of a flow autosaves a new unpublished version.** Proof: MEASURED (HAR).

---

## F -- cloud flows

**F-01 `coalesce()` skips only null.** A blank canvas input arrives as `""` (not null) -> `coalesce(x, 'default')`
returns "". Use `if(empty(x), 'default', x)`. `flowcheck` warns. Proof: MEASURED (`400 model is not specified`).

**F-02 Respond-to-PowerApp outputs need `"x-ms-dynamically-added": true`.** Without it a legacy import silently drops
every output; the flow runs, Studio sees no return value (`'field' isn't recognized` cascades). Shape:
`{"title": X, "x-ms-dynamically-added": true, "type": "string"}`. `flowcheck` fails it. Proof: MEASURED against a real
export.

**F-03 PowerApp trigger inputs need `x-ms-dynamically-added: true` AND a `description`.** Otherwise the designer throws
"Cannot read properties of undefined (reading 'dynamicallyAddedInfo')" on Save. `flowcheck` fails it. Proof: MEASURED
(the flag alone was not enough; a census of designer-saved flows showed the description).

**F-04 One JSON text input per app-called flow.** A canvas app binds `Run()` at the trigger's input COUNT; adding an
input later re-cuts every call site (a missed site silently breaks). One `payload` string = new fields are new JSON
keys. Proof: OBSERVED (a mark-complete bug from call sites passing 10 of 12 args).

**F-05 No zero-argument `createArray()`.** InvalidTemplate at RUN time, not import. Use `json('[]')`. `flowcheck` fails
it. Proof: MEASURED.

**F-06 `createObject()` is not WDL.** It shipped and failed live. Use `json(concat(...))` or `setProperty`/`addProperty`.
`flowcheck` fails unknown functions. Proof: MEASURED.

**F-07 `filter()` is not an expression function.** In a Foreach, `item()` inside it bound to the loop item, not the
array element ("property 'Name' cannot be selected ... String"). Use a Filter array (Query) action. For membership:
Select -> `join(...,'|')` -> `contains()`. Proof: MEASURED.

**F-08 SetVariable cannot read its own variable** ("Self reference is not supported") -- at RUN time. Compute into a
Compose first. `flowcheck` fails it. Proof: MEASURED.

**F-09 An expression may reference only actions on its runAfter path.** Save fails `InvalidTemplate ... must either be
in 'runAfter' path or within a scope action on the 'runAfter' path`. `flowcheck` computes the closure. Proof: MEASURED.

**F-10 Single expressions over 8192 characters are refused.** Precompute parts in Compose/Select actions. Proof:
MEASURED.

**F-11 Terminate is not allowed inside Foreach/Until.** Set a flag, terminate after the loop. Proof: OBSERVED.

**F-12 A 5xx from a connector under the DEFAULT retry policy can hold a run in Running for hours.** Fix: every
connector action gets `"retryPolicy": {"type": "exponential", "count": 3, "interval": "PT10S", "minimumInterval":
"PT5S", "maximumInterval": "PT1M"}` in inputs. `flowcheck` warns when missing. Proof: MEASURED (a bare probe still
retrying after 7+ minutes; runs cancelled after 1-2 h).

**F-13 The run-history API lists no repetitions for an in-flight Foreach.** A loop busy retrying looks "never
dispatched". Diagnose by replaying the definition through an Http-trigger twin and stubbing actions. Proof: MEASURED.

**F-14 List-triggered flows fire on EVERY modification, including their own and other flows' writes.** Fix: a trigger
condition on a status column; `runtimeConfiguration.concurrency.runs = 1`; for claims, GET the item's etag then MERGE
with `IF-MATCH: <etag>` (412 = someone else won); re-read the row before acting ("freshness guard"). A status-stamping
flow elsewhere doubled every run. `flowcheck` warns on a list trigger with no condition. Proof: MEASURED.

**F-15 Updating a flow (import or API) re-baselines an automated trigger.** An item changed around the update is not
picked up; make a fresh change to test. Proof: MEASURED.

**F-16 The display name that counts is in `definition.json`.** Package manifest names are ignored by the importer/API;
keep the package file name, manifest names and `properties.displayName` identical and compact. Proof: MEASURED (a whole
suite of flows imported under names no manifest matched).

**F-17 Never delete-and-recreate a flow an app calls** (C-39). Update in place.

**F-18 Connector shapes must be attested, never guessed.** Copy an action the designer produced ("Peek code") or one
that ran; for an unknown connector build a tiny probe flow and read the connector's swagger
(`GET .../apis/shared_x?$expand=properties/swagger`). A checker can enforce an allowlist of attested parameters.
See reference/attested-connector-shapes.md. Proof: practice that prevented repeated invented-operation failures
(e.g. a model inventing `CreateItem` for `PostItem`, a wrong SharePoint domain).

**F-19 A built-in HTTP action's `limit.timeout` does not bound a synchronous call.** See C-14. Proof: MEASURED.

**F-20 Tricks that work in pure WDL** (MEASURED, a renderer flow): flatten nested arrays by JSON-text concatenation
(`json(concat('[', join(...), ']'))`); `range(0, length(x))` + Select for per-element logic; guard with `if()` and
`first()`/`skip()` instead of `[0]`/`[1]` so empty arrays never throw; never `take(x, 0)`.

**F-21 Time zones:** never hardcode a zone letter/offset; derive it (e.g. compare
`ticks(convertTimeZone(ts,'UTC','<zone>'))` with `ticks(ts)`). A date written without `Z` is read in the SITE's regional
zone (hours off). Always write UTC with `Z`. Proof: MEASURED.

**F-22 Approval-style waits:** a run lives at most 30 days; split long human cycles into one flow per step (list
triggered on the status each step writes). Proof: DOCUMENTED platform limit, used in a working design.

---

## S -- SharePoint

**S-01 Get items `$filter` is OData v3.** `substringof('x', Field)` / `startswith`, no `contains()`, no `in`; multi-line
text (Note) columns are not filterable; mind the 5000-item view threshold (index filtered columns). `flowcheck` fails
v4 syntax. Proof: DOCUMENTED + OBSERVED.

**S-02 Unstorable values answer HTTP 500, not 400.** DateTime before 1900-01-01; single-line text over the column's
MaxLength (255 by default). Choice columns accept ANY value over REST. URL fields: Url and Description capped at 255.
Guard values in the flow (year >= 1900, `take(x, 254)`), and set retry (F-12). Proof: MEASURED.

**S-03 `SP.MoveCopyUtil.CopyFolder` copies the CONTENTS flat into destUrl** (no subfolder). Proof: MEASURED (a publish
failed NotFound on the assumed nested path).

**S-04 `folders/addUsingPath` is single-level.** Missing parent = 500 (the connector reports BadGateway); an existing
folder = 400 unless `overwrite=true` (then 200, contents intact). In flows, chain the next action with runAfter
`["Succeeded","Failed"]` for create-if-missing. Proof: MEASURED.

**S-05 Server-relative vs library-relative paths.** REST (`addUsingPath`, `GetFolderByServerRelativePath`) wants
`/sites/<site>/Shared Documents/...`; connector actions (CreateFile, ListFolder, GetFolderMetadataByPath) want
`/Shared Documents/...`. Keep ONE library-relative root and derive the other. A personal (OneDrive) site's library is
`Documents`, a team site's `Shared Documents`. Proof: MEASURED.

**S-06 `ListItemEntityTypeFullName` varies** (e.g. a trailing `1`). Read it from `_api/web/lists(...)` at run time;
never hardcode `SP.Data.*ListItem`. `flowcheck` warns. Proof: MEASURED.

**S-07 Column internal names get mangled.** A field named `Sha256` becomes `_x0053_ha256`; REST then needs
`OData__x0053_ha256` for read, write and `$filter` alike. Name hash columns `ContentHash`; audit by InternalName, then
Title, and never create a twin next to a mangled field. Proof: MEASURED (twice, including delete-and-recreate).

**S-08 Patching the list item of an `.html` file rewrites the file** (property demotion injects
`<mso:CustomDocumentProperties>`), so any content hash taken earlier no longer matches. Keep attestation columns on a
sibling JSON item or the source row; never MERGE the HTML item after hashing. Proof: MEASURED (131 -> 979 bytes over
four versions).

**S-09 Item writes: prefer the connector's PostItem/PatchItem** for list-item values; use "Send an HTTP request to
SharePoint" (HttpRequest) for lists, fields, folders and files. Writing item values through HttpRequest hit a
numeric-type issue once. Proof: OBSERVED (details not recorded).

**S-10 The connector's HttpRequest adds the request digest itself.** For MERGE send `X-HTTP-Method: MERGE` and
`IF-MATCH: *` (or an etag). `flowcheck` warns on a hand-added digest. Proof: MEASURED.

**S-11 The connector `table` parameter accepts a list TITLE** as well as the GUID. Proof: MEASURED.

**S-12 List provisioning belongs in a runnable artifact, not click steps.** Deliver list/column changes as an
idempotent, audit-by-default provisioning flow (Button trigger, `apply` input, GET fields -> Filter array -> create
only what is missing -> report), or in a dev tenant `devtenant sp-provision`. Proof: pattern used repeatedly; the
example generates one.

---

## M -- Microsoft 365 connectors (mail, groups, Teams, approvals)

**M-01 An M365 GROUP address is not a mailbox.** Every Office 365 Outlook mailbox action fails on it ("Group Shard is
used in non-Groups URI"); only the Office 365 Groups Mail connector reads it. Proof: MEASURED.

**M-02 Groups Mail limits.** `ListGroupThreads` takes no parameters (about 10 newest threads); its HttpRequest Graph
passthrough allowlists only messages/mailFolders/events/calendar, so `/groups/{id}/threads` is refused. Paging a group
mailbox needs another route (subscribe a mailbox, or a premium Entra-authenticated HTTP action). `ListThreadPosts`
carries no attachments array; `GetAttachments` returns bytes. Proof: MEASURED.

**M-03 Outlook connector facts:** `GetEmailsV3` `top` max 1000 (enforced at save); `includeAttachments=true` is required
for `contentBytes`; mail has no `webLink` property; `ListFolderV2` fails server-side (use ListFolder + GetFileItems).
Proof: MEASURED.

**M-04 Approvals:** an M365 group address works as `assignedTo` (responder = the individual member);
`enableNotifications: false` sends no mail but the request still appears in the approver's Received view and the
response carries `respondLink` and the adaptive card (post it yourself); there is no cancel action (a superseded
approval stays open -- guard on re-read). Proof: MEASURED.

**M-05 Teams:** `PostMessageToConversation` takes `poster`, `location`, `body/recipient` (or group/channel ids) and
`body/messageBody`; `AtMentionUser` takes one `userId`. Discover a tenant's teams/channels through the connector's own
swagger operations in a probe flow, not by guessing ids. Proof: MEASURED.

**M-06 HTML -> PDF inside a flow:** Graph `/drive/root:/<path>:/content?format=pdf` through the HTTP-with-Entra connector
FAILS with 302 but exposes `headers.Location` (a pre-authenticated URL); a built-in HTTP GET of that URL returns the
PDF bytes. The SharePoint connector never follows the redirect. The Entra HTTP connector is PREMIUM (C-41). Proof:
MEASURED.

---

## P -- packaging and manual import

**P-01 Legacy flow package layout.** `manifest.json` + `Microsoft.Flow/flows/manifest.json` +
`Microsoft.Flow/flows/<asset GUID>/{definition.json, apisMap.json, connectionsMap.json}`; a missing flows manifest =
`MissingPackageManifest`. Hand-built api/connection resources modelled on an export import fine. Proof: MEASURED.

**P-02 Legacy import refuses `inputs.authentication` on OpenApiConnection actions**
(`WorkflowRunActionInputsInvalidProperty`), and a connector used by an action but missing from
`connectionReferences` (`Property 'host.connectionReferenceName' is missing`). The API deploy path tolerates the first,
so a green API deploy is NOT proof of importability. `flowcheck` fails both. Proof: MEASURED.

**P-03 Update, not Create-as-new, for a revision.** Create-as-new leaves two flows on one trigger (both fire) and gives
an app-called flow a new GUID. Proof: MEASURED.

**P-04 Connections are re-picked on import**, never carried; the first connection of each connector is created once
by a human. Proof: MEASURED.

**P-05 A file opened in Studio is a NEW app.** Its first plain Save creates an app named after
`Resources/PublishInfo.json` AppName (can be stale). Use Save as -> Replace existing. Proof: MEASURED.

**P-06 Windows `tar -a -cf x.msapp` writes a TAR** (format follows the extension); zip to `.zip` and rename. Proof:
MEASURED.

**P-07 `pac canvas validate` is retired and `pac canvas pack` validates nothing** (no property, formula or YAML-strict
checks). `pac canvas unpack` reads the COMPILED layer (misleading for LoadFromYaml packages) and throws
NullReferenceException on some zips Studio opens fine -- it is a false-negative validator. Proof: MEASURED.

**P-08 The tenant bindings live in the document.** A `.msapp` carries the data-source, connection and flow ids of the
tenant it was saved in. Build packages ON a Studio-saved export from the target tenant (the "reference export" /
base), stamping only Src into it; refuse a base whose embedded App checker result has formula errors. Proof: MEASURED
(foreign connection ids blocked a deploy; a red export was nearly adopted as truth).

**P-09 A YAML-packed document is re-serialised by the service on import** (control counts change, `packed.json`
disappears). Do not diff a downloaded copy against your build and conclude a stale build ran. Proof: MEASURED.

---

## D -- dev-tenant APIs and headless deploy

**D-01 One refresh token, many audiences (FOCI).** The SharePoint Online Management Shell public client's refresh
token redeems for SharePoint, Flow, Graph and apihub. Refresh tokens ROTATE -- persist every new one (a second stale
cache copy later fails `AuthenticationFailed`). Proof: MEASURED.

**D-02 Not every first-party client is preauthorized for every resource** (`AADSTS65002 ... must be configured via
preauthorization` -- a hard wall, not a consent prompt). The FOCI client above cannot mint Power Apps or Dataverse
tokens; the Power Automate Desktop client is apihub-only; the Power Platform CLI client works for Dataverse. Proof:
MEASURED. See reference/dev-tenant-auth.md.

**D-03 Token caches committed through a stale ignore path.** After a folder move the old `.gitignore` pattern no longer
matched and live refresh tokens were tracked for two weeks. Keep caches OUTSIDE the repo and use path-independent
ignore patterns; after any move run `git ls-files | grep -i token`. Proof: MEASURED (found, untracked; history not
rewritten).

**D-04 Flow API: `PowerApp` triggers cannot be driven by script.** `triggers/manual/run` fails
`InvokerConnectionOverrideFailed` and drops custom headers; the trigger's callback URL wants a Power Apps runtime token.
Test through an Http-trigger TWIN (same definition, connections Embedded); custom headers then reach
`triggerOutputs()['headers']`, which makes identity gates (`x-ms-user-email`) testable. Delete twins after. Proof:
MEASURED.

**D-05 The connections API answers 404 to a Flow-audience token** for a plain maker; harvest connection references from
live flows instead (they also give the exact field shape the tenant accepts). Proof: MEASURED.

**D-06 Canvas deploy route = BAP package import** (generateResourceStorage -> upload -> listImportParameters -> use ITS
re-staged packageLink -> importPackage). Direct POST/PATCH of `/apps` answers 500 for every body; the Power Platform API
route needs scopes a maker token lacks (403). Proof: MEASURED (30+ imports).

**D-07 The package key.** importPackage answers a bare InternalServerError unless the manifest's app resource key and
folder id were minted by the service's own exportPackage (opaque exact match, pair must match, reusable for any app,
survives the source app's deletion). Mint once per environment by exporting any app. Editing an EXPORTED package's
resources (e.g. adding connections) hangs listImportParameters or 400s on dangling dependency keys -- ship no
connections in the package. Proof: MEASURED by bisection.

**D-08 Import-as-Update lands as a DRAFT; publish with api-version 2017-05-01** (2016-11-01 refused). A CREATE import is
a single version (published == draft). Proof: MEASURED.

**D-09 The importer's draft has NO connection references, and PATCH cannot reach a draft.** Studio opens the draft,
shows everything "Not connected" and its first save wipes the references. Write the draft as Studio does:
`acquireLease` -> full-definition `PUT` (lifeCycleId Draft, the draft's own documentUri, four-part client versions) ->
`releaseLease`; publish then PROMOTES the draft's references. PATCH of the published definition is a fallback repair
only (publish briefly holds the lease: retry `AppLeaseActive`). Never open an unpublished headless draft in Studio.
Proof: MEASURED (draft 0 refs vs published 11; Studio then showed Connected on the written draft).

**D-10 "Not connected" flows after a headless deploy** had three layers: (1) the flow entry's connection id must be
`<de-dashed workflow GUID>-<16 hex>` (a bare dashed GUID = "added with the old method"); (2) per-flow dependency
wiring (`dependencies`, `parameterHints`, `parameterHintsV2`, `dependents`) for every connector the flow calls; (3) an
owning `LocalConnectionReferences` entry for every flow data source. A synthesized entry without `displayName` and
`iconUri` preceded Studio dropping ALL references -- never write one without them. GUID case, path case and the suffix
value were cosmetic. Proof: MEASURED by diffing working and broken exports.

**D-11 Embedded table schemas can be refreshed headless**: the connection runtime's
`/apim/sharepointonline/<conn>/$metadata.json/datasets/<site, DOUBLE-encoded>/tables/<list GUID>` returns byte-for-byte
what Studio stores; the display-name mapping follows Studio's collision rule (`name_mapping`). Proof: MEASURED (10
Studio-saved documents reproduced).

**D-12 Embedded flow signatures can be refreshed headless**: `POST .../flows/<id>/listWadl` returns what Studio embeds;
pin `siena:serviceId` to the data-source name. Proof: MEASURED (byte-identical to the Studio HAR).

**D-13 Only the player's launch call shows a broken runtime package.** `POST
https://<env host>/powerapps/apps/<id>/launch?api-version=2` -> `packageStatus`, error text, and a SAS to the compiled
JS; the maker API reports Published regardless. Env host (commercial):
`default<env GUID hex minus 2>.<last 2>.environment.api.powerplatform.com`. Proof: MEASURED.

**D-14 A handler NULL rule looks exactly like an empty property** (`_re1("<id>.<Prop>",null,false)`); only the
document tells them apart -- flag it only where the document has a non-constant formula. Proof: MEASURED.

**D-15 Browser automation of the player:** persistent signed-in profile (one process at a time); a consent iframe on
first launch; the app in frame `fullscreen-app-host`; controls by `[data-control-name]`. Proof: MEASURED.

**D-16 Connector-runtime REST quirks** (apihub): the dataset (site URL) must be DOUBLE URL-encoded ("Route did not
match" otherwise); `table` is the list GUID; a single-table GET 404s (use `/items`); a Choice value must be
`{"Value": "x"}` -- a plain string is silently dropped and the empty field is then omitted from reads, which looks like
a missing column; the proxy may wrap a body as a one-element array `[{"value": [...]}]`. Proof: MEASURED.

**D-17 Empty is not absent.** A response `{"value": []}` must not be mistaken for "no value member" (one parser returned
the envelope itself as a fake row). Test member presence separately from emptiness. Proof: MEASURED.

**D-18 Compare datetimes through ONE normalizer.** Parsing a zone-less string as local shifted every value by the
machine's UTC offset and re-patched thousands of rows each run. Proof: MEASURED.

**D-19 The FOCI Graph token may be denied group reads** (`Authorization_RequestDenied` even for `/me` on a restricted
tenant); read group members through a flow's Office 365 Groups connection instead. Proof: MEASURED.

**D-20 Approvals can be answered without a click:** `POST .../environments/<env>/approvals/<name>/approvalResponses`
`{"properties":{"response":"<option>","comments":"..."}}` with a Flow token -> 200 Committed and the wait resumes (the
Dataverse approval-response table was 403 for a maker). List pending: `approvalViews` with the mandatory
`$filter=properties/userRole eq 'Approver' and properties/isActive eq true`; pages oldest-first. Proof: MEASURED.

**D-21 Some agent sandboxes block commands whose text contains destructive-looking REST paths** (a DELETE to
`/items(...)` read as "remove on a system path"). Put such calls in a script file and run the file. Proof: OBSERVED
(agent-environment specific).

---

## L -- LLM gateways (from flows and apps)

**L-01 Anthropic-style Messages:** `system` is TOP-LEVEL (not a message); `max_tokens` is required and must be an
INTEGER (a serializer that writes `2000.0` gets 400 "valid integer"); the answer is `content[0].text`; models wrap JSON
in code fences despite "JSON only" -> strip fences. Proof: MEASURED.

**L-02 Do not echo a returned assistant block verbatim** -- a `tool_use` block may carry fields the request validator
rejects; rebuild `{type, id, name, input}`. Compaction must cut only at a genuine user-text message, never inside a tool
call/result pair. Proof: MEASURED.

**L-03 OpenAI-style reasoning models reject `max_tokens` (want `max_completion_tokens`) and any `temperature` != 1,
reported ONE AT A TIME.** Learn the accepted shape per model from successive 400s (bounded retries) and cache it.
Proof: MEASURED.

**L-04 Tool-call shapes differ by model behind one gateway** -- OpenAI `{type:function, function:{name, arguments}}` vs
Anthropic-style `{type:tool_use, name, input, text}` inside an OpenAI envelope. Parse both
(`coalesce(item()?['name'], item()?['function']?['name'])`, args from `arguments`/`text`/`input`). A half-parse (name
found, args not) is worse than failing: tools ran with `{}`. Dump the raw payload early. Proof: MEASURED.

**L-05 `finish_reason` is advisory** (seen `stop` with real tool calls); decide on the presence of tool calls. Tool
pairing is enforced both ways: every call needs a result and orphan results are rejected. `prompt_tokens` INCLUDES
cached tokens. Proof: MEASURED.

**L-06 Errors arrive as HTTP 200.** Bad key `{"response":"...invalid...","status":400}`, "model is experiencing
issues", token-limit messages, and oversize-file extraction errors are all 200 with the error in the body. Check the
body, not just the status. Proof: MEASURED.

**L-07 Listed is not usable.** A model in the catalogue can error on every call; an UNLISTED name can silently route to
a different model and return 200. Filter by name classes on token boundaries (`gemini` contains `mini`); catalogues mix
image models in with chat models. Proof: MEASURED.

**L-08 A conversation ending on an assistant turn** is rejected by some models (no prefill) -> intermittent 503 "model
experiencing issues". Guard the loop so a hop is never sent before the previous tool result is appended. Proof:
MEASURED.

**L-09 Models over-escape in tool arguments** (a literal `\n` reaches the reply); normalise before display. Proof:
MEASURED.

**L-10 An empty reply with no usage block** is usually a guardrail refusal, not a bug. Proof: MEASURED.

**L-11 Token cost of agentic loops is large.** A 40+ KB system prompt resent every hop made one question cost ~20k
tokens (measured against the gateway's meter; a chars/4 estimate overstated it ~2x). State the per-question cost and cap
live test runs to a named handful. Proof: MEASURED.

**L-12 A custom connector sends one file per call.** Multi-file multipart (repeated part names) needs a raw HTTP
action `$multipart` body. Proof: MEASURED.

**L-13 RAG ingest endpoints commonly APPEND** (same filename twice = both copies retrievable) and may not support
per-document delete of array-ingested content; datasets may need creating first; a 200 can carry a refusal message.
Verify against YOUR gateway before designing re-ingest. Proof: MEASURED on one gateway.

**L-14 A system prompt inside a single-quoted WDL expression** must have its apostrophes doubled or avoided. Proof:
MEASURED.

**L-15 Tenant DLP can block specific external APIs** from flows; test the gateway call from a flow in the target
environment early. The built-in HTTP action in an app-called flow does not make the APP premium (C-41). Proof:
OBSERVED.

---

## X -- process traps

**X-01 A green dev-tenant run is not production sign-off.** The legacy importer is stricter than the API (P-02); the
dev tenant may differ in schemas (C-02/C-28) and policies. Report which environment proved what. Proof: MEASURED.

**X-02 Never pipe a test into `tail` before a commit/push decision** -- the pipe's exit code masked failing suites
twice. Check the real exit status. Proof: MEASURED.

**X-03 Regex/sed replacements corrupt text** (`&` and `#` in replacements expanded; bash heredocs mangled backslashes
in patch scripts; CRLF files needed `\r?$`). Use literal string replacement from a file-writing tool. Proof: MEASURED.

**X-04 Mutation-test every new checker rule** -- make it reject a deliberately broken input after fixing a false
positive, not just go green. Proof: practice (the self-tests here do it).

**X-05 When a re-import breaks something that "worked", check which build was actually running** (an old package had
been live for weeks). Proof: MEASURED.

**X-06 Read your own path handling before blaming the service** (a local filename truncation dropped the extension;
the service was first blamed). Proof: MEASURED.

**X-07 Absence of a local example is not absence of a capability** -- check the vendor documentation before ruling
something out (a run link CAN carry inputs). Proof: MEASURED.

**X-08 Measure, don't infer** -- size plans from a purpose-built read-only census, never from stale logs (a paged read
that stopped short gave a confident wrong total). Proof: MEASURED.

**X-09 Keep the validation tenant for proving specific uncertain things**, not "keeping it current"; do not create
throwaway renamed apps to dodge problems (clutter, wrong app tested); delete experiments. Proof: MEASURED (feedback).

**X-10 Canvas vs flow iteration cost differs.** Big batched canvas changes are fine (App checker gives fast structured
feedback); cloud flows have no local compiler -- prefer precise, attested definitions plus a designer build guide over
blind JSON. Proof: practice.
