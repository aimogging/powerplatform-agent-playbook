# Example: Contoso Help Desk

A deliberately small, invented solution that exercises the whole method end to end:

| Piece | What it is | Why it is in the example |
|---|---|---|
| `sharepoint/helpdesk.schema.json` | one list, `ContosoHelpDeskTickets` | one schema drives both provisioning routes; renamed Title; Choice columns; display names with spaces |
| `flows/ContosoHelpDeskSubmitTicket` | app-called flow: PowerApp trigger (ONE JSON text input) -> create item -> Respond | the Run() contract, `x-ms-dynamically-added`, error Respond, caller identity from headers |
| `flows/ContosoHelpDeskNotifyNewTicket` | list-triggered flow: when Status = New -> mail the operator -> mark Triaged | trigger condition vs. the flow's own write re-firing it; retry policy; Update-not-Create on re-import |
| `ContosoHelpDeskProvisionList` (generated) | button flow, audit-by-default, creates the list/columns if missing | how list changes reach a tenant where nobody runs scripts |
| `canvas/Src/*.pa.yaml` | one-screen canvas app "Contoso Help Desk" | block scalars, IfError around Flow.Run, display-name columns, `Title` still `Title` |
| `build.py` | builds `dist/` (flow packages + stamped `.msapp`) | the manual-import handoff |
| `deploy.manifest.json` | the same thing, deployed headless into a DEV tenant | the agent's automated loop |

**Verification status.** Part B was proven live in a commercial validation tenant by `run_e2e.py` (below): list
provisioned, both flows deployed and running, the app imported headless, published (runtime package Ready, 0 NULL
rules), a ticket submitted through the Http twin and one through the app in a real browser, each triaged exactly once
by the list-triggered flow (mail to the operator), run history read, then everything deleted and the deletion
verified. Part A (a person importing the packages by hand into another tenant) has NOT been run with this example:
the packages pass the offline gates and use connector shapes that imported elsewhere; treat the first manual import
as its verification and report what differed.

**First run: `python example/run_e2e.py --template-msapp <a Studio-saved .msapp>`** does Part B end to end and
cleans up (stages, resume and options: its header, or the repo README "First run").

---

## Part A -- the handoff path (manual import, what a production owner does)

No scripts run on the target side. The agent builds; a human imports.

**A1. Build the flows** (agent side, repo root):

```
python example/build.py
```

With `config/environment.json` present, its `siteUrl` and `operatorEmail` are substituted into the flows; without
it you get contoso placeholders (fine to inspect, useless to run). Outputs land in `example/dist/`.

**A2. Create the list.** Power Automate -> My flows -> Import -> *Import Package (Legacy)* ->
`example/dist/ContosoHelpDeskProvisionList.zip` -> set the SharePoint connection slot -> *Create as new* -> Import.
Open the flow, Run it with **apply = false** (audit: the run's `Audit_Result` lists what is missing), then Run with
**apply = true**, then once more with false to confirm nothing is missing.

**A3. Import the two flows** the same way: `ContosoHelpDeskSubmitTicket.zip` (SharePoint slot) and
`ContosoHelpDeskNotifyNewTicket.zip` (SharePoint + Outlook slots). First time: *Create as new*. Every later revision:
choose **Update** and pick the existing flow -- never *Create as new* again (a second copy of the list-triggered
flow fires on every change, and a re-created app-called flow gets a new GUID the app no longer points at).
Turn both flows **On**.

**A4. Create the app shell once in Studio** (the one thing that cannot come from source: data-source bindings are
minted by the tenant):
1. make.powerapps.com -> Create -> Blank app -> Blank canvas app, name **Contoso Help Desk**, Tablet.
2. Rename `Screen1` to **scrHelpDesk** (tree view -> rename).
3. Data -> Add data -> SharePoint -> your site -> **ContosoHelpDeskTickets**.
4. Power Automate pane -> Add flow -> **ContosoHelpDeskSubmitTicket**.
5. Save. Then Save menu (the arrow next to Save) -> **Download a copy**. Keep that file: it is your *base*.

**A5. Stamp the source into the base:**

```
python example/build.py --base "<path to the downloaded .msapp>"
```

This runs `canvas-lint`, refuses a base whose embedded App checker result has formula errors, replaces
`Src/*.pa.yaml`, and adds `packed.json` (`LoadFromYaml: true`) -- without it Studio silently ignores every YAML edit.
Output: `example/dist/Contoso Help Desk.msapp`.

**A6. Import the app:** make.powerapps.com -> Apps -> Import canvas app -> *From file (.msapp)* -> pick the file.
It opens in Studio as a NEW, unsaved app. Open **App checker**: Formulas must show 0 errors. Then Save menu ->
**Save as -> Replace existing -> Contoso Help Desk -> Replace**. Do **not** press plain Save first: that creates a
second app named after the package's stored name. Then **Publish**.

**A7. Test in the product** (write down each result):
1. Play the app. The header and form render (no all-black screen).
2. Submit a ticket with subject "Printer jam". Expect the green "Ticket N created" banner and the ticket at the top
   of *My tickets* with status **New**.
3. Within about two minutes the operator mailbox receives "[Help desk] Normal - Printer jam", and the ticket shows
   **Triaged** after a refresh.
4. Power Automate -> ContosoHelpDeskNotifyNewTicket -> run history: exactly ONE run did work for that ticket; the run
   caused by its own "Triaged" write is skipped by the trigger condition (no second mail).
5. ContosoHelpDeskSubmitTicket -> run history -> the run -> `Respond_Ok` -> inputs: `result_json` is
   `{"status":"ok","id":N}`.
6. Break it on purpose: turn ContosoHelpDeskSubmitTicket **Off** and submit again -> the app shows the red "did not answer"
   message instead of hanging. Turn it back On.

What each failure usually means is in `skills/verify/SKILL.md` ("reading symptoms").

---

## Part B -- the agent's automated loop (DEV tenant only)

For iterating fast in a tenant you own. Same sources, no clicking except the one-time app shell (A4) and connection
creation.

```
cd tools
python -m devtenant login sharepoint          # device code, once; later calls refresh silently
python -m devtenant login flow
python -m devtenant login powerApps           # see reference/dev-tenant-auth.md if this client is refused
python -m devtenant sp-provision ../example/sharepoint/helpdesk.schema.json            # audit
python -m devtenant sp-provision ../example/sharepoint/helpdesk.schema.json --apply    # create what is missing
cd ..
python example/build.py --base "<downloaded base .msapp>"
python tools/deploy.py --manifest example/deploy.manifest.json --plan-only
python tools/deploy.py --manifest example/deploy.manifest.json
```

`deploy.py` updates flows in place (GUIDs kept), imports the app through the package API, writes the draft's
connection references under an editing lease, publishes, then runs the GATE: the player's runtime package must be
`Ready` with **0 NULL rules**. After the first successful run set `"mustExist": true` on ContosoHelpDeskNotifyNewTicket.

Drive the flows and read results without clicking:

```
cd tools
python -m devtenant flow-invoke ContosoHelpDeskSubmitTicket payload.json --header x-ms-user-email=tester@contoso.com
python -m devtenant flow-runs ContosoHelpDeskNotifyNewTicket
python -m devtenant flow-run ContosoHelpDeskSubmitTicket <runId>
python -m devtenant flow-twin-delete ContosoHelpDeskSubmitTicket
python -m devtenant sp-cleanup ContosoHelpDeskTickets --tag "[fixture]" --apply
```

`payload.json` is `{"payload": "{\"subject\":\"[fixture] printer jam\",\"priority\":\"High\"}"}`. A PowerApp-trigger
flow cannot be called by a script directly; `flow-invoke` creates an Http-trigger twin (same definition,
Embedded connections) and calls that. Fixture rows carry a tag so cleanup deletes only what the test created.
