---
name: canvas-packaging
description: Turn canvas-app .pa.yaml sources into a .msapp that a person imports by hand in Power Apps Studio, and hand it over with exact import steps. Use whenever an app edit must reach a tenant, when an imported app "shows none of my changes", when Studio refuses to open a package, or when choosing between the pac CLI route and the no-pac stamp route.
---

# Canvas packaging for manual import

The production owner imports the `.msapp` by hand. Your job: a package Studio will open AND read, plus steps a human
can follow without judgement calls.

## The two facts that decide everything

1. **Bindings come from the tenant.** Data sources, connection ids, flow ids and the embedded list schemas / flow
   signatures are minted in the target tenant. Build on a **base**: a `.msapp` saved by Studio in THAT tenant with the
   data sources and flows already added (P-08). Your YAML goes into it; nothing else changes.
2. **Studio reads `Src/` only when `packed.json` says `LoadFromYaml: true`** (C-17). A raw "Download a copy" lacks it,
   so edits are silently ignored. Studio drops it again on its next save: every new base needs it re-added.

## Route A (default): stamp into a Studio download -- no pac needed

```
# once per screen-set change: in Studio create/rename the screens, add data sources + flows, Save,
# then Save menu -> Download a copy  => base.msapp
python tools/msapp-tool.py info base.msapp            # entries, packed.json state, stored app name, checker counts
python tools/msapp-tool.py stamp base.msapp <Src dir> "dist/<App>.msapp"
```

`stamp` copies every base entry byte-for-byte, replaces/adds `Src/*.pa.yaml`, adds `packed.json`, runs
`canvas-lint`, and refuses a base whose embedded App checker result has formula errors (fix those in Studio first).
To edit a download by hand instead: `msapp-tool.py unpack` -> edit `Src/` -> `msapp-tool.py pack`.

Proof status: the stamp shape (Python-copied entries of a Studio download) opened in Studio; the tar-zipped
unpack/packed.json/zip round trip was MEASURED end to end (import -> 0 formula errors -> Save as Replace -> Publish ->
runtime package Ready). The combination "Python zip + packed.json" itself is UNVERIFIED: if Studio refuses it, rezip
the unpacked folder with `tar -a -cf out.zip -C <dir> .` and rename to `.msapp` (P-06).

## Route B: the pac CLI

```
python tools/msapp-tool.py msapr base.msapp work/App.msapr    # resource pack (every non-Src entry)
# put App.msapr next to the Src/ folder, then:
pac canvas pack --sources work --msapp "dist/<App>.msapp"
```

pac validates nothing (P-07); run `canvas-lint` first. `pac canvas unpack` of a Studio export is a quick way to get
Src YAML from an app built by clicks.

## Screens

Create or rename screens in Studio before building (C-18: YAML-only screens are UNCERTAIN). New controls inside an
existing screen are fine. A control type the base never used may need one instance added in Studio first (C-19).

## Handoff steps (paste these to the person importing)

1. make.powerapps.com -> Apps -> Import canvas app -> **From file (.msapp)** -> pick `<App>.msapp`.
2. It opens in Studio as a new, unsaved app. Open **App checker**: *Formulas* must show 0 errors. If not, stop and
   send a screenshot of the expanded panel (or the saved app, see `verify`).
3. Save menu -> **Save as -> Replace existing -> <App> -> Replace**. Never press plain Save first -- it creates a
   second app named after the package's stored name (P-05).
4. **Publish**. Play the app once; if the player says "You're using an old version", refresh (C-44).
5. If a flow shows "Not connected" or an action is unknown: Power Automate pane -> the flow -> ... -> Refresh (C-03).
   If the flow was re-created (new GUID), remove and re-add it (C-39).
6. Roll back if needed: app -> Details -> Versions -> previous version -> Restore -> Publish.

## Also true

- The app display name must avoid `. \ / : * ? " < > |` (C-42), and the whole app stays standard-licensed only if no
  premium connector is a data source (C-41).
- Deploy the flows the app calls BEFORE packing/importing the app, updating them in place (C-39, P-03).
- For a dev tenant, `deploy-headless` does all of this through the API.
