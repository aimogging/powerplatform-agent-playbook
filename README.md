# Power Platform agent playbook

**What gets delivered: a functional, importable canvas `.msapp` and cloud-flow packages that a person imports into
their environment.** The automated deploys into a dev tenant exist only to test that exact build end to end; they are
never how anything is delivered.

## Install (one line, no git)

**Claude Code plugin** -- in Claude Code, run these two commands:

```
/plugin marketplace add aimogging/powerplatform-agent-playbook
```

```
/plugin install powerplatform-agent-playbook@powerplatform-agent-playbook
```

**Everything, as a folder** (tools, example, docs; for any agent) -- in a terminal (Windows cmd, macOS or Linux):

```
curl -fsSL https://raw.githubusercontent.com/aimogging/powerplatform-agent-playbook/main/install.py | python -
```

```
curl -fsSL https://raw.githubusercontent.com/aimogging/powerplatform-agent-playbook/main/install.py | python3 -
```

(Use whichever of `python` / `python3` / `py` starts Python 3.9+ on your machine.) It downloads the latest version into
`./powerplatform-agent-playbook`, creates a Python environment with the optional packages and a browser for app tests,
checks for the optional Power Platform CLI, creates `config/environment.json` and tells you what to fill in. Later,
to update in place (your `config/environment.json` is kept):

```
curl -fsSL https://raw.githubusercontent.com/aimogging/powerplatform-agent-playbook/main/install.py | python - --update
```

Other options go after the lone `-`: `--dir <folder>`, `--force`, `--no-venv`, `--no-browser`. Both one-liners need
the repository to be **public** (the plugin route also works for a private repository if you are signed in to GitHub
with access to it).

---

A method, a trap catalogue and working Python tools for having an AI coding agent build and test **canvas Power
Apps** and **Power Automate cloud flows** -- including SharePoint provisioning, calling LLM gateways from flows, and
fully automated testing in a dev tenant -- while the production tenant receives nothing but packages a person imports
by hand.

Start with **[AGENTS.md](AGENTS.md)** (the agent's entry point; `CLAUDE.md` is the same file).

## First run: prove your setup

1. Fill in `config/environment.json` for a Microsoft 365 **dev/test** tenant (never production): `tenantId`,
   `environmentId` (e.g. `Default-<tenant id>`), `siteUrl` (a SharePoint site where you may create a list),
   `operatorEmail` (your own address; the demo mails only you). Plugin installs: put it at
   `~/.pp-playbook/environment.json` instead, so updates never overwrite it.
2. In the maker portal, make sure a **SharePoint** and an **Office 365 Outlook** connection exist (Data ->
   Connections). No API can create one.
3. Sign in once per API (device code; later runs refresh silently), then run the read-only doctor:

```
cd tools
python -m devtenant login sharepoint
python -m devtenant login powerApps
python -m devtenant login apihub
python -m devtenant doctor
cd ..
```

   Every line is PASS / FAIL / SKIP with a category -- TOOL (install something), CONFIG (a value in the config),
   SIGN-IN (run the login shown), PERMISSION (ask an admin for the named right) -- and the fix.
4. Run the whole lifecycle on the invented "Contoso Help Desk" demo, then watch it clean up after itself:

```
python example/run_e2e.py --template-msapp "<any app you saved in Studio in this tenant>.msapp"
```

   Stages: preflight, provision, build, deploy, bind, publish (runtime gate), drive the flows, drive the app in a real
   browser, verify run history, build the handoff packages from the same sources, then the PERSON PATH -- the handoff
   `.msapp` opened in Power Apps Studio (Import app > From file, App checker, Save as > Replace existing, Publish,
   one Submit in play mode) and the flow package imported in Power Automate (Import Package (Legacy), connections
   picked, Turn on, one run) -- and cleanup (verified). The summary
   table shows PASS/FAIL and the tier each stage proves; `--from <stage>` resumes, `--keep` leaves the demo in place,
   `--only cleanup` removes it later. The template `.msapp` only supplies Microsoft's control templates: any app saved
   in Studio that uses a text label, text input, button, rectangle, vertical gallery and the modern drop down. The
   first browser stage opens a window; sign in there once (set `browser.profileDir` in the config to reuse a profile).

## Use as a template

Clone or download (the installer above does it without git), open the folder in your agent, and it reads `AGENTS.md`
/ `CLAUDE.md`. Tools run from the repo root:

```
python tools/run-self-tests.py                 # all offline self-tests (no tenant needed)
python example/build.py --check                # builds the example into a temp folder
```

Then follow `example/README.md`: Part A is the manual-import handoff, Part B the agent's automated dev-tenant loop.

## Install as a Claude Code plugin

The two `/plugin` commands at the top add this repository as a marketplace and install the plugin. Its skills load
on demand: `playbook-rules` carries the rules and routing (a plugin's own `CLAUDE.md` is not loaded, so the rules
travel as a skill -- `tools/plugin-check.py` keeps the two texts identical), the others are the procedures. Inside
skills, paths are written `${CLAUDE_PLUGIN_ROOT}/tools/...`; Claude Code fills in the installed location. Keep your
tenant config at `~/.pp-playbook/environment.json`. Update with `/plugin marketplace update` and uninstall with
`/plugin uninstall powerplatform-agent-playbook@powerplatform-agent-playbook`. For a one-off session without
installing: `claude --plugin-dir <path to this folder>`.

## What is here

| Path | For |
|---|---|
| `AGENTS.md` | rules, routing, the develop -> check -> dev-test -> package -> handoff -> verify loop |
| `skills/*/SKILL.md` | ten skills: `playbook-rules`, `lifecycle`, canvas authoring and packaging, cloud-flow authoring and packaging, verification, LLM calls, dev-tenant automation, headless deploy |
| `reference/platform-traps.md` | platform traps (symptom -> cause -> fix -> how it was proven) |
| `reference/attested-connector-shapes.md` | connector action shapes that imported and ran, with placeholders |
| `reference/dev-tenant-auth.md` | which Microsoft public client works for which API, and the token rules |
| `tools/` | Python tools, standard library only; every tool has `--self-test` |
| `example/` | "Contoso Help Desk": an invented list + two flows + a canvas app; `run_e2e.py` is the first-run check |
| `install.py` | the no-git installer / updater |
| `.claude-plugin/` | plugin and marketplace manifests |
| `docs/index.html` | the quick start as a page |

## Requirements

- Python 3.9+. Optional: PyYAML (stricter canvas lint, layout renderer), Playwright (browser tests of published
  apps), the Power Platform CLI `pac` (an alternative canvas packing route). The installer sets up the first two.
- A Microsoft 365 **dev/test** tenant with Power Apps and Power Automate for the automated loop.

## Verification status

The offline tools are tested by their self-tests here. The platform facts in `reference/` carry their own proof level.
The tenant-facing Python modules and the Contoso example were proven live end to end in a commercial validation
tenant with `example/run_e2e.py` (provision, flow deploy, headless app deploy and publish, runtime gate, flows driven
through a twin, the app driven in a browser, run history, handoff build, verified cleanup) -- and the DELIVERABLES
were proven the way a person handles them: the Python-built `.msapp` opened in Studio (0 formula errors, the stamped
YAML read, Save as > Replace existing, published, played) and the flow package imported through the portal (connections
picked, turned on, ran). That run found and fixed a package defect the API path could not show (P-10). Device-code
sign-in itself was not re-run (the run reused existing refresh tokens of the same public clients); treat your first
`login` as a check. Not run: an import into a SECOND tenant (the person path ran in the same validation tenant).

## Keeping it clean

`tools/scrub-check.py --denylist <file outside the repo> [--history]` must report CLEAN before every commit: no tenant
identifiers, hostnames, e-mail addresses, tokens or model ids in content or history. Real tenant values belong in the
git-ignored `config/environment.json`; caches, logs and browser profiles under `~/.pp-playbook/`.
