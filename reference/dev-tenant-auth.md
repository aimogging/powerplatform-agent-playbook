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

Measured on a commercial validation tenant (the defaults in `config/environment.example.json` follow this table):

| Client (public, first-party) | Proven audiences | Refused / not enough |
|---|---|---|
| SharePoint Online Management Shell `9bc3ab49-b65d-410a-85ad-de819febfddc` (FOCI) | SharePoint (Sites.FullControl.All as the user), Flow (incl. `listWadl`), Graph -- one refresh token for all | apihub, Power Apps service, Dataverse (AADSTS65002) |
| Power Platform CLI `51f81489-12ee-4a9e-aaae-a2591f45987d` | Power Apps service (+ BAP, player launch, connections list), Dataverse | apihub: token issued (`user_impersonation`) but the connector runtime answers 403 `missing connection ACL` |
| Power Automate Desktop `386ce8c0-7421-48c9-a1df-2a532400339f` | apihub with `Runtime.All` -- the only one the connector runtime (`$metadata.json` schemas) accepted | SharePoint REST (AADSTS65002) |
| Power Apps PowerShell module `689e5960-2e49-4505-98d8-369236220fc6` | UNVERIFIED here | -- |

So a full setup is three sign-ins: `login sharepoint` (also covers flow and graph), `login powerApps` (also covers
dataverse), `login apihub`. `python -m devtenant doctor` shows which ones are missing.

**What was and was not exercised.** Every audience above was used live by the tools (provision, deploy, publish,
launch gate, schema refresh, run history, cleanup). The device-code prompt itself was not re-run in that proof: the
caches were seeded from existing refresh tokens of the SAME public clients with
`python -m devtenant login <key> --refresh-token-from <file.json>` (a JSON file with a `refresh_token` member; the
token is redeemed once and the rotated one cached). Device code is the standard OAuth flow for these clients, but
treat your first `login` as a check and read its output. If a client is refused in your tenant, set
`clients.<key>` in the config to another public client, or mint the token elsewhere and export it as
`PP_TOKEN_<KEY>` (e.g. `PP_TOKEN_POWERAPPS`).

`.default` returns whatever the (client, resource, user) triple already has -- for the FOCI client on SharePoint that
is tenant-wide FullControl for the signed-in user. Use it deliberately, in a tenant you may change.

## Rules

1. **Refresh tokens rotate.** Persist the new refresh token after EVERY grant. A second cache holding the old one
   once failed later with `AuthenticationFailed`; in another measurement the old one still worked -- rely on neither,
   keep ONE cache.
2. **Caches live outside the repo** (`~/.pp-playbook/token-cache.json`), and the repo's ignore patterns are
   path-independent (`**/*token-cache*.json`, `**/*.token.json`, `.secrets/`). A folder move once defeated a path-specific
   ignore rule and live refresh tokens were committed.
3. **One audience per token.** Assert the `aud` claim before calling an API (`auth.assert_audience`); a 401 from the
   right host usually means the right token went to the wrong service.
4. **Access tokens last about an hour.** The tell of expiry mid-run is an empty/401 first call (e.g. `_api/contextinfo`)
   cascading into blank values downstream; refresh first.
5. **Never print tokens.** `python -m devtenant whoami` prints only audience, user and expiry.
6. **A maker's Flow token cannot list connections** (404), the Power Apps token can (that is what `doctor` uses);
   flow deploys harvest connection references from live flows, or use the names in `config/environment.json`
   `connections`. No token can CREATE a connection: make the first one per connector in the maker portal.
7. **Group reads through Graph may be denied** for this client on a restricted tenant; read group members through a
   flow's Office 365 Groups connection instead.
