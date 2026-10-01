"""Dev-tenant sign-in for the agent: device code once, then silent refresh-token grants per audience.

PROVEN (a commercial validation tenant; reference/dev-tenant-auth.md has the table):
  * The SharePoint Online Management Shell public client (9bc3ab49-...) is in the FOCI "family of client ids":
    ONE refresh token redeems for SharePoint (<site origin>/.default -> full _api, Sites.FullControl.All for that
    user), the Flow API (service.flow.microsoft.com) and Graph. It is refused (AADSTS65002) for apihub, the Power
    Apps service and Dataverse.
  * The Power Platform CLI public client (51f81489-...) gets the Power Apps service and Dataverse. Its apihub token
    is refused by the connector runtime (403 "missing connection ACL", user_impersonation only).
  * The Power Automate Desktop public client (386ce8c0-...) gets apihub with Runtime.All -- what the connector
    runtime needs.
  * Refresh tokens ROTATE: always persist the newest one and keep ONE cache.
  * `seed()` (CLI: login <key> --refresh-token-from <file>) starts a cache from an existing refresh token of the
    same client; the live proof used it. The device-code prompt itself was not re-run there.

Token caches NEVER live in the repo: default ~/.pp-playbook/token-cache.json. (A path move once silently defeated
an ignore rule and live refresh tokens were committed for two weeks.)
"""
import base64
import json
import os
import time
import urllib.parse

from .http import Http, HttpError

AUDIENCES = {
    'flow': 'https://service.flow.microsoft.com/.default offline_access',
    'graph': 'https://graph.microsoft.com/.default offline_access',
    'apihub': 'https://apihub.azure.com/.default offline_access',
    'powerApps': 'https://service.powerapps.com/.default offline_access',
}


def scope_for(cfg, key):
    if key == 'sharepoint':
        if not cfg.site_origin:
            raise SystemExit('config siteUrl is required for a SharePoint token')
        return cfg.site_origin + '/.default offline_access'
    if key == 'dataverse':
        return (cfg.get('dataverseUrl') or '').rstrip('/') + '/.default offline_access'
    return AUDIENCES[key]


def claims(token):
    try:
        part = token.split('.')[1]
        part += '=' * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode('ascii')).decode('utf-8'))
    except Exception:
        return {}


def assert_audience(token, host_fragment):
    aud = str(claims(token).get('aud', ''))
    if host_fragment.lower() not in aud.lower():
        raise SystemExit('token audience is %r, expected one containing %r -- wrong token for this API' % (aud, host_fragment))
    return aud


class TokenCache(object):
    def __init__(self, path):
        self.path = path
        self.data = {'refresh': {}, 'access': {}}
        if os.path.isfile(path):
            with open(path, encoding='utf-8') as fh:
                self.data = json.load(fh)
            self.data.setdefault('refresh', {})
            self.data.setdefault('access', {})

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self.data, fh, indent=1)
        os.replace(tmp, self.path)


class Auth(object):
    def __init__(self, cfg, http=None, cache=None, clock=time.time, out=print):
        self.cfg, self.http = cfg, http or Http()
        self.cache = cache or TokenCache(cfg.token_cache)
        self.clock, self.out = clock, out
        self.tenant = cfg.get('tenantId') or 'organizations'

    def _token_url(self):
        return '%s/%s/oauth2/v2.0/token' % (self.cfg.hosts['login'].rstrip('/'), self.tenant)

    def _form(self, url, fields):
        body = urllib.parse.urlencode(fields).encode('ascii')
        r = self.http.request('POST', url, headers={'Content-Type': 'application/x-www-form-urlencoded'}, raw_body=body,
                              allow_write_retry=True)
        return r.status, (r.json() or {})

    def device_code(self, key):
        client = self.cfg.clients[key]
        status, dc = self._form('%s/%s/oauth2/v2.0/devicecode' % (self.cfg.hosts['login'].rstrip('/'), self.tenant),
                                {'client_id': client, 'scope': scope_for(self.cfg, key)})
        if status != 200:
            raise SystemExit('devicecode request failed: %s' % dc)
        self.out(dc.get('message') or ('Open %s and enter %s' % (dc.get('verification_uri'), dc.get('user_code'))))
        deadline = self.clock() + int(dc.get('expires_in', 900))
        interval = int(dc.get('interval', 5))
        while self.clock() < deadline:
            time.sleep(interval)
            status, tok = self._form(self._token_url(), {'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                                                         'client_id': client, 'device_code': dc['device_code']})
            if status == 200:
                self._store(key, client, tok)
                return tok['access_token']
            if tok.get('error') not in ('authorization_pending', 'slow_down'):
                raise SystemExit('device-code sign-in failed: %s %s' % (tok.get('error'), tok.get('error_description', '')[:300]))
        raise SystemExit('device-code sign-in timed out')

    def seed(self, key, refresh_token):
        """Seed the cache with an existing refresh token of the SAME client (e.g. from another tool's cache) and redeem
        it for `key` -- for sessions where nobody can complete a device-code sign-in. The token is never printed."""
        client = self.cfg.clients[key]
        self.cache.data['refresh'][client] = refresh_token
        self.cache.data['access'].pop(key, None)
        self.cache.save()
        return self.token(key, interactive=False)

    def _store(self, key, client, tok):
        if tok.get('refresh_token'):
            self.cache.data['refresh'][client] = tok['refresh_token']     # rotated: always persist the newest
        self.cache.data['access'][key] = {'token': tok['access_token'], 'client': client,
                                          'expires_at': self.clock() + int(tok.get('expires_in', 3600)) - 120}
        self.cache.save()

    def token(self, key, interactive=True):
        env = os.environ.get('PP_TOKEN_' + key.upper())
        if env:
            return env
        slot = self.cache.data['access'].get(key)
        if slot and slot.get('expires_at', 0) > self.clock():
            return slot['token']
        client = self.cfg.clients[key]
        rt = self.cache.data['refresh'].get(client)
        if rt:
            status, tok = self._form(self._token_url(), {'grant_type': 'refresh_token', 'client_id': client,
                                                         'refresh_token': rt, 'scope': scope_for(self.cfg, key)})
            if status == 200:
                self._store(key, client, tok)
                return tok['access_token']
            err = '%s %s' % (tok.get('error'), (tok.get('error_description') or '')[:200])
            if 'AADSTS65002' in err:
                raise SystemExit('client %s is not preauthorized for %s (AADSTS65002). Set clients.%s in the config to a '
                                 'client that is, or pass a token in PP_TOKEN_%s.' % (client, key, key, key.upper()))
            self.out('refresh grant failed (%s); falling back to device code' % err)
        if not interactive:
            raise SystemExit('no cached token for %s; run: python -m devtenant login %s' % (key, key))
        return self.device_code(key)

    def headers(self, key, extra=None):
        h = {'Authorization': 'Bearer ' + self.token(key), 'Accept': 'application/json'}
        h.update(extra or {})
        return h
