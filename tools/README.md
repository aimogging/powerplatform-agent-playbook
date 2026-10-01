# tools/

Python 3.9+ standard library only (PyYAML makes `canvas-lint.py` stricter; Playwright is needed only for
`devtenant.appdriver`). There is no PowerShell anywhere in this repository. Run from the repo root.
Every tool has an offline `--self-test`; `python tools/run-self-tests.py` runs them all.

## Offline authoring and packaging (safe anywhere, no tenant)

| Tool | What it does |
|---|---|
| `flowcheck.py <flow-dir or definition.json>` | Structural + import/run-trap checks for a cloud-flow definition (WDL). Mutation-tested. |
| `canvas-lint.py <Src dir / unpacked msapp / *.pa.yaml>` | Canvas source checks: YAML traps Studio refuses, PA2108 properties, NULL-rule and publish-hang patterns. |
| `build-flow-package.py <flow-dir>` | Flow source folder -> legacy import package `.zip` for a MANUAL import. Runs flowcheck first. |
| `msapp-tool.py unpack/lint/pack/stamp/msapr/info` | Edited `.pa.yaml` -> `.msapp` a human imports by hand (adds `packed.json` so Studio reads the YAML). |
| `appchecker-sarif.py <app.msapp>` | Studio's App checker result embedded in a saved/downloaded `.msapp` (the only machine-readable copy). |
| `parse-appcheck.py <errors.html>` | Compact issue list from an App checker panel outerHTML dump. |
| `add-copy-buttons.py <file.html>` | Copy buttons on every `<pre>` in an HTML doc (idempotent). |
| `scrub-check.py --denylist <path outside repo> [--history]` | The gate that keeps deployment-identifying data out of this repo. |

## Dev/test-tenant automation (the agent's own tools; NEVER pointed at production)

| Tool | What it does |
|---|---|
| `python -m devtenant ...` (run inside `tools/`) | Sign-in (device code + cached refresh token), SharePoint provisioning/fixtures, flow deploy/run/inspect, approvals, headless app deploy, document download, runtime NULL-rule checks, census. `python -m devtenant --help`. |
| `deploy.py --manifest <m.json>` | Plan / confirm / apply a whole dev-tenant deployment (provision -> flows -> apps -> publish -> runtime gate) with `--resume-from`, `--stop-after`, `--skip`, `--plan-only`, selectors. |

Configuration: copy `config/environment.example.json` to `config/environment.json` (git-ignored). Token
caches, package keys, logs and the browser profile live under `~/.pp-playbook/`, never in the repo.

Status: the checks and self-tests run offline here. The `devtenant` modules were proven live in a commercial
validation tenant by `example/run_e2e.py` (SharePoint provisioning and cleanup incl. recycle bin, Flow API deploy /
Http twin / run history, headless app import + references + publish, launch gate with the NULL-rule scan, connector
runtime schema refresh, `listWadl`, the Playwright app driver, app/flow/list deletion). `deploy.py` (the manifest
orchestrator) calls the same functions but was itself exercised only by its offline self-test. The device-code prompt
was not re-run live (see reference/dev-tenant-auth.md).

| First-run and packaging helpers | What it does |
|---|---|
| `python -m devtenant doctor` | READ-ONLY: tools, config, a token per API, permissions (site, flows, apps, connections, import storage); PASS/FAIL per line with the fix |
| `../example/run_e2e.py` | the whole lifecycle on the Contoso demo in the dev tenant, verified cleanup (README "First run") |
| `leakcheck.py` | refuses a handoff package carrying dev-tenant values or test scaffolding (used by `--handoff`) |
| `plugin-check.py [--write]` | keeps `skills/playbook-rules` identical to AGENTS.md; validates the plugin/marketplace manifests |
| `../install.py` | no-git installer/updater (`--self-test` is offline) |
