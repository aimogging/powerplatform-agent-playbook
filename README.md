# Power Platform agent playbook

A method, a trap catalogue and working Python tools for having an AI coding agent build and test **canvas Power
Apps** and **Power Automate cloud flows** -- including SharePoint provisioning, calling LLM gateways from flows, and
fully automated testing in a dev tenant -- while the production tenant receives nothing but packages a person imports
by hand.

Start with **[AGENTS.md](AGENTS.md)** (the agent's entry point; `CLAUDE.md` is the same file).

## What is here

| Path | For |
|---|---|
| `AGENTS.md` | rules, routing, the develop -> check -> dev-test -> package -> handoff -> verify loop |
| `skills/*/SKILL.md` | eight procedures: canvas authoring and packaging, cloud-flow authoring and packaging, verification, LLM calls, dev-tenant automation, headless deploy |
| `reference/platform-traps.md` | 140 platform traps (symptom -> cause -> fix -> how it was proven) |
| `reference/attested-connector-shapes.md` | connector action shapes that imported and ran, with placeholders |
| `reference/dev-tenant-auth.md` | which Microsoft public client works for which API, and the token rules |
| `tools/` | Python tools, standard library only; every tool has `--self-test` |
| `example/` | "Contoso Help Desk": an invented list + two flows + a canvas app, with a walkthrough for both paths |
| `docs/index.html` | the quick start as a page |

## Quick start

```
python tools/run-self-tests.py                 # all offline self-tests (no tenant needed)
python example/build.py --check                # builds the example into a temp folder
cp config/environment.example.json config/environment.json   # then fill in YOUR dev tenant
```

Then follow `example/README.md`: Part A is the manual-import handoff, Part B the agent's automated dev-tenant loop.

## Requirements

- Python 3.9+. Optional: PyYAML (stricter canvas lint), Playwright (browser tests of published apps),
  the Power Platform CLI `pac` (an alternative canvas packing route).
- A Microsoft 365 **dev/test** tenant with Power Apps and Power Automate for the automated loop.

## Verification status

The offline tools are tested by their self-tests here. The platform facts in `reference/` carry their own proof level
(most were measured on a real tenant). The tenant-facing Python modules (`tools/devtenant`, `tools/deploy.py`) and the
example are ports/inventions that have been exercised only against offline fakes: their first live run is a
verification run. The playbook says so wherever it matters.

## Keeping it clean

`tools/scrub-check.py --denylist <file outside the repo> [--history]` must report CLEAN before every commit: no tenant
identifiers, hostnames, e-mail addresses, tokens or model ids in content or history. Real tenant values belong in the
git-ignored `config/environment.json`; caches, logs and browser profiles under `~/.pp-playbook/`.
