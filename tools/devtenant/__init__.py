"""devtenant -- the agent's own Python tooling for a DEV/TEST Microsoft 365 tenant.

Everything here is for automated development and testing in a tenant you are allowed to change:
sign-in (device code, cached refresh tokens), SharePoint REST provisioning and fixtures, the Flow Web API
(deploy, run, read run history, answer approvals), the Power Apps / BAP package import (headless canvas-app
deploy), canvas document repairs (bindings, schemas, flow signatures), runtime null-rule checks, and a
browser driver for published apps.

Standard library only (Playwright is optional, for appdriver). Every network call goes through
http.Transport so the offline self-test (python -m devtenant self-test) can replace it.

The end user of the apps/flows never runs any of this: production still receives manually imported
packages (see skills/canvas-packaging and skills/cloud-flow-packaging).
"""
