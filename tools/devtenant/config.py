"""Load config/environment.json (git-ignored) with commercial defaults and placeholder checks."""
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(REPO, 'config', 'environment.json')
EXAMPLE_PATH = os.path.join(REPO, 'config', 'environment.example.json')
HOME_PATH = os.path.join(os.path.expanduser('~'), '.pp-playbook', 'environment.json')

DEFAULT_HOSTS = {
    'login': 'https://login.microsoftonline.com',
    'flow': 'https://api.flow.microsoft.com',
    'powerApps': 'https://api.powerapps.com',
    'bap': 'https://api.bap.microsoft.com',
    'graph': 'https://graph.microsoft.com',
    'player': 'https://apps.powerapps.com',
    'maker': 'https://make.powerapps.com',
    'powerPlatformApiSuffix': 'api.powerplatform.com',
}
FOCI_SPO_SHELL = '9bc3ab49-b65d-410a-85ad-de819febfddc'
PAC_CLIENT = '51f81489-12ee-4a9e-aaae-a2591f45987d'
PAD_CLIENT = '386ce8c0-7421-48c9-a1df-2a532400339f'   # the only one proven to get apihub Runtime.All
DEFAULT_CLIENTS = {
    'sharepoint': FOCI_SPO_SHELL, 'flow': FOCI_SPO_SHELL, 'graph': FOCI_SPO_SHELL, 'apihub': PAD_CLIENT,
    'powerApps': PAC_CLIENT, 'dataverse': PAC_CLIENT,
}


class Config(dict):
    """dict with attribute helpers; values come from environment.json over the defaults."""

    @property
    def hosts(self):
        h = dict(DEFAULT_HOSTS)
        h.update(self.get('hosts') or {})
        return h

    @property
    def clients(self):
        c = dict(DEFAULT_CLIENTS)
        c.update(self.get('clients') or {})
        return c

    def home_path(self, key, default):
        p = self.get(key) or default
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else os.path.join(os.path.expanduser('~'), p)

    @property
    def token_cache(self):
        return self.home_path('tokenCache', '.pp-playbook/token-cache.json')

    @property
    def package_key_cache(self):
        return self.home_path('packageKeyCache', '.pp-playbook/package-keys.json')

    @property
    def site_origin(self):
        m = re.match(r'^(https://[^/]+)', self.get('siteUrl') or '')
        return m.group(1) if m else ''

    def require(self, *keys):
        bad = [k for k in keys if not self.get(k) or '<' in str(self.get(k))]
        if bad:
            raise SystemExit('config/environment.json: fill in %s (placeholders still present)' % ', '.join(bad))
        return self


def load(path=None):
    path = path or os.environ.get('PP_PLAYBOOK_CONFIG') or DEFAULT_PATH
    if path == DEFAULT_PATH and not os.path.isfile(path) and os.path.isfile(HOME_PATH):
        path = HOME_PATH          # plugin installs: the plugin folder is replaced on update, the home copy survives
    if not os.path.isfile(path):
        raise SystemExit('no %s -- copy config/environment.example.json to config/environment.json (or ~/.pp-playbook/environment.json) and fill it in' % path)
    with open(path, encoding='utf-8-sig') as fh:
        return Config(json.load(fh))


def from_dict(d):
    return Config(d)
