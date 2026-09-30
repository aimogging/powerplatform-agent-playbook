# Glossary

**App checker** -- Studio's formula/accessibility checker. No export; its result is embedded in a saved app as
`AppCheckerResult.sarif` (read with `tools/appchecker-sarif.py`) or captured from the panel HTML
(`tools/parse-appcheck.py`).

**Attested shape** -- a connector action definition copied from one that imported and ran in a real tenant (or the
designer's Peek code). The opposite of a guessed one.

**Base / reference export** -- a `.msapp` saved by Studio in the TARGET tenant, carrying that tenant's data-source,
connection and flow bindings. Builds stamp repo `Src/*.pa.yaml` into it.

**BAP** -- Business Application Platform API (`api.bap.microsoft.com`); hosts the package import/export used for
headless canvas deploys.

**Connection vs connection reference** -- a connection is a signed-in credential object in an environment (created by
a human); a connection reference is the flow's/app's pointer to one (`connectionReferences` in a flow,
`LocalConnectionReferences` in a canvas document).

**Dev / validation tenant** -- a commercial test tenant the agent may change (headless deploys, fixtures). Used to prove
specific uncertain behaviour, not kept "in sync".

**Draft vs published** -- canvas apps have versions; an import-as-Update creates a draft; publish makes it live. Studio
opens the draft.

**FOCI** -- "family of client IDs": first-party public clients whose refresh tokens are interchangeable across the
family's resources.

**Http twin** -- a copy of a PowerApp-trigger flow whose trigger kind is Http and whose connections are Embedded, so a
script can call it through its signed URL. Deleted after the test.

**Invoker vs Embedded** -- whose credentials a flow action uses: the caller's (Invoker; always the case for PowerApp
triggers) or the connection stored with the flow (Embedded).

**Legacy package** -- the `.zip` of "Import Package (Legacy)": `manifest.json` + `Microsoft.Flow/flows/...`. The
handoff format for flows in this playbook.

**LoadFromYaml / packed.json** -- the marker file that makes Studio read `Src/*.pa.yaml` instead of the compiled
`Controls/*.json`.

**Mint (package key)** -- exporting any app once so the service registers a (resource key, folder id) pair that
`importPackage` will accept.

**NULL rule** -- a formula the runtime compiler could not bind, shipped as a rule that does nothing. Only visible in the
player's compiled JavaScript.

**pa.yaml / Source Code layout** -- the YAML form of a canvas app (`Src/App.pa.yaml`, one file per screen).

**Production (locked-down) tenant** -- where the solution runs for real; reached only by a human importing the built
packages. No scripts, no API access from the agent.

**Respond-to-PowerApp** -- the `Response` action of kind `PowerApp` that returns values to `Flow.Run()`.

**Runtime package** -- the JavaScript the player downloads for a published app; its `packageStatus` (Ready / InProgress
/ Error) comes from the player's launch call.

**WADL** -- the XML description of a flow's Run() inputs and Respond outputs embedded in a canvas app per flow data
source; refreshed from `listWadl`.

**WDL** -- Workflow Definition Language: the JSON + `@`-expression language of cloud flows (shared with Logic Apps).
