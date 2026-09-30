# Dev-tenant authentication for the agent (commercial cloud)

The agent signs in as a normal maker in a DEV/TEST tenant with Microsoft's own public clients and the OAuth 2.0
device-code flow -- no app registration, no admin consent. `tools/devtenant/auth.py` implements all of this.

## Endpoints

| Purpose | Host / scope |
|---|---|
| sign-in | `https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/devicecode` and `/token` |
| SharePoint REST | resource = the site ORIGIN: `https://<TENANT>.sharepoint.com/.default` |
| Flow Web API | `https://api.flow.microsoft.com`, scope `https://service.flow.microsoft.com/.default` |
| Power Apps API | `https://api.powerapps.com`, scope `https://service.powerapps.com/.default` |
| BAP (package import) | `https://api.bap.microsoft.com`, SAME Power Apps token |
| Power Platform API (player launch) | `https://<env host>.environment.api.powerplatform.com`, the Power Apps token was accepted |
| Graph | `https://graph.microsoft.com/.default` |
| Connector runtime (apihub) | scope `https://apihub.azure.com/.default`; host = the connection's `testLinks[0].requestUri` host |
| Dataverse | `https://<ORG>.crm.dynamics.com/.default` |

Always add `offline_access` to get a refresh token.

## Which client for which audience

| Client (public, first-party) | Proven audiences | Refused |
|---|---|---|
| SharePoint Online Management Shell `9bc3ab49-b65d-410a-85ad-de819febfddc` (FOCI) | SharePoint (Sites.FullControl.All as the user), Flow, Graph, apihub -- one refresh token for all | Power Apps service, Dataverse (AADSTS65002) |
| Power Platform CLI `51f81489-12ee-4a9e-aaae-a2591f45987d` | Dataverse (device code) | -- |
| Power Automate Desktop `386ce8c0-7421-48c9-a1df-2a532400339f` | apihub (connector runtime) | SharePoint REST (AADSTS65002) |
| Power Apps PowerShell module `689e5960-2e49-4505-98d8-369236220fc6` | UNVERIFIED here for device code | -- |

Power Apps audience: the headless app deploy was proven with a Power Apps-audience token minted by a different
first-party client through the Windows account broker. The config defaults the Power Apps client to the pac client
(pac's own BAP calls use this audience -- documented from its binaries), but a device-code grant for it has NOT been
run here: treat the first `python -m devtenant login powerApps` as a verification. If it fails with AADSTS65002, set
`clients.powerApps` to another public client, or mint the token elsewhere and export it as `PP_TOKEN_POWERAPPS`.

`.default` returns whatever the (client, resource, user) triple already has -- for the FOCI client on SharePoint that
is tenant-wide FullControl for the signed-in user. Use it deliberately, in a tenant you may change.

## Rules

1. **Refresh tokens rotate.** Persist the new refresh token after EVERY grant; a second cache holding the old one
   fails later with `AuthenticationFailed`.
2. **Caches live outside the repo** (`~/.pp-playbook/token-cache.json`), and the repo's ignore patterns are
   path-independent (`**/*token-cache*.json`, `**/*.token.json`, `.secrets/`). A folder move once defeated a path-specific
   ignore rule and live refresh tokens were committed.
3. **One audience per token.** Assert the `aud` claim before calling an API (`auth.assert_audience`); a 401 from the
   right host usually means the right token went to the wrong service.
4. **Access tokens last about an hour.** The tell of expiry mid-run is an empty/401 first call (e.g. `_api/contextinfo`)
   cascading into blank values downstream; refresh first.
5. **Never print tokens.** `python -m devtenant whoami` prints only audience, user and expiry.
6. **A maker's Flow token cannot list connections** (404); harvest connection references from live flows, or name
   connections in `config/environment.json` `connections`.
7. **Group reads through Graph may be denied** for this client on a restricted tenant; read group members through a
   flow's Office 365 Groups connection instead.
