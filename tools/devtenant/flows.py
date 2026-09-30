"""Flow Web API client (commercial): headless deploy, run, inspect, approvals, and listWadl.

Base: https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments/<env>, api-version 2016-11-01,
token audience https://service.flow.microsoft.com. All routes below were exercised on a real tenant unless noted.

  create   POST  {base}/flows                      body {properties:{displayName, definition, connectionReferences}}
  update   PATCH {base}/flows/{id}                 same body; the workflow GUID is KEPT (canvas apps bind flows by
                                                   GUID -- never delete-and-recreate a flow an app calls)
  start    POST  {base}/flows/{id}/start
  runs     GET   {base}/flows/{id}/runs?$top=N;  .../runs/{run}/actions;  .../actions/{name}/repetitions
           inputsLink / outputsLink on each action are SAS URLs: GET them with NO Authorization header
  trigger  POST  {base}/flows/{id}/triggers/manual/listCallbackUrl -> response.value (a signed URL); POST JSON to it
  listWadl POST  {base}/flows/{id}/listWadl         the Run() signature Studio embeds in a canvas app (see canvasdoc)

TRAPS (measured):
  * PATCH re-baselines an automated trigger's checkpoint: an item changed right around the update is not picked up.
  * A PowerApp-trigger flow's connection references are ALWAYS stored as source "Invoker" whatever you send, and
    triggers/manual/run then fails InvokerConnectionOverrideFailed -- so test such a flow through an Http-trigger
    TWIN (http_twin below): same definition, trigger kind Http, connections Embedded; custom request headers reach
    triggerOutputs()['headers'] (that is how an x-ms-user-email identity gate is tested). Delete the twin after.
  * listFlows pages at $top=50 -- follow nextLink.
  * The API cannot create a CONNECTION; the first connection of each connector is made once in the maker portal.
  * An in-flight Foreach lists NO repetitions; a loop that is busy retrying a 5xx looks "never dispatched".
"""
import copy
import io
import json
import os
import re
import zipfile

API = '2016-11-01'


def name_key(display_name):
    """Space/punctuation/case-insensitive identity: 'Order Intake' == 'OrderIntake'."""
    return re.sub(r'[^A-Za-z0-9]', '', display_name or '').lower()


def is_real_connection_name(api, name):
    """A tenant-minted connection name: 32 hex chars, or 'shared-<api>-<guid>' for a few connectors. Anything else
    (the api name itself, shared_*, 'shared-<api>-<flow name>' placeholders written for a manual import) is not."""
    name = (name or '').lower()
    return bool(re.fullmatch(r'[0-9a-f]{32}', name) or
                re.search(r'-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', name))


def read_flow_source(path):
    """A legacy package .zip or a flow source folder -> {displayName, definition, connectionReferences}."""
    if os.path.isdir(path):
        with io.open(os.path.join(path, 'definition.json'), encoding='utf-8-sig') as fh:
            doc = json.load(fh)
    else:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.replace('\\', '/').endswith('/definition.json') and 'Microsoft.Flow/flows/' in n.replace('\\', '/')]
            if not names:
                raise SystemExit('no Microsoft.Flow/flows/*/definition.json inside %s' % path)
            doc = json.loads(z.read(names[0]).decode('utf-8-sig'))
    props = doc.get('properties', doc)
    return {'displayName': props.get('displayName', ''), 'definition': props['definition'],
            'connectionReferences': props.get('connectionReferences') or {}}


class FlowClient(object):
    def __init__(self, cfg, auth, http):
        self.cfg, self.auth, self.http = cfg, auth, http
        self.env = cfg['environmentId']
        self.base = '%s/providers/Microsoft.ProcessSimple/environments/%s' % (cfg.hosts['flow'].rstrip('/'), self.env)

    def _h(self):
        return self.auth.headers('flow')

    def _u(self, path, extra=''):
        return '%s/%s?api-version=%s%s' % (self.base, path, API, extra)

    # ---------------------------------------------------------------------------------------- read
    def list(self):
        out, url = [], self._u('flows', '&$top=50')
        while url:
            page = self.http.json('GET', url, headers=self._h()) or {}
            out.extend(page.get('value') or [])
            url = page.get('nextLink')
        return out

    def get(self, flow_id):
        return self.http.json('GET', self._u('flows/%s' % flow_id), headers=self._h())

    def find(self, display_name, flows=None, exact_only=False):
        """Exact name first; else the ONE flow equal ignoring spaces/punctuation/case (renamed in place on update).
        Two near-misses = ambiguous -> error. A name given as '=Name' never falls back."""
        if display_name.startswith('='):
            display_name, exact_only = display_name[1:], True
        flows = self.list() if flows is None else flows
        for f in flows:
            if f['properties'].get('displayName') == display_name:
                return f
        if exact_only:
            return None
        near = [f for f in flows if name_key(f['properties'].get('displayName')) == name_key(display_name)]
        if len(near) > 1:
            raise SystemExit('%s: %d flows match ignoring spaces/case: %s -- rename or delete the extras' % (
                display_name, len(near), ', '.join('%s (%s)' % (f['properties']['displayName'], f['name']) for f in near)))
        return near[0] if near else None

    def connection_catalog(self, flows=None, max_flows=60):
        """api -> a connectionReferences entry harvested from flows already running here (the shape the tenant accepts)."""
        cat = {}
        for f in (self.list() if flows is None else flows)[:max_flows]:
            try:
                full = self.get(f['name'])
            except Exception:
                continue
            for api, ref in ((full.get('properties') or {}).get('connectionReferences') or {}).items():
                if api not in cat and is_real_connection_name(api, ref.get('connectionName')):
                    cat[api] = {'reference': dict(ref), 'seenIn': full['properties'].get('displayName')}
        return cat

    def dependency_map(self, flows=None, max_flows=200):
        """flow GUID (no dashes, lower) -> connector apis its definition calls (feeds canvasdoc.set_flow_dependencies)."""
        out = {}
        for f in (self.list() if flows is None else flows)[:max_flows]:
            try:
                full = self.get(f['name'])
            except Exception:
                continue
            refs = (full.get('properties') or {}).get('connectionReferences') or {}
            out[f['name'].replace('-', '').lower()] = [a for a in refs if a != 'shared_logicflows']
        return out

    # ---------------------------------------------------------------------------------------- deploy
    @staticmethod
    def resolve_references(package_refs, existing=None, catalog=None):
        """Existing flow's reference > catalog harvested from live flows > the package's own (only if real).
        A placeholder connection name (the api name itself, shared_*) is never used. Returns (refs, missing, sources)."""
        live = ((existing or {}).get('properties') or {}).get('connectionReferences') or {}
        refs, missing, sources = {}, [], {}
        for api, pref in (package_refs or {}).items():
            if api in live:
                refs[api], sources[api] = dict(live[api]), 'existing flow'
            elif catalog and api in catalog:
                refs[api], sources[api] = dict(catalog[api]['reference']), 'catalog: %s' % catalog[api]['seenIn']
            elif is_real_connection_name(api, (pref or {}).get('connectionName')):
                refs[api], sources[api] = dict(pref), 'package'
            else:
                missing.append(api)
        return refs, missing, sources

    @staticmethod
    def preserve_secrets(pkg_def, live_def):
        """Keep hand-entered secrets across a redeploy: an InitializeVariable STRING whose committed value is a
        placeholder (blank, <...>, REPLACE_WITH..., TODO_...) and whose name looks like a credential takes the live
        value. Values are never printed -- only lengths."""
        def inits(actions, out):
            for n, a in (actions or {}).items():
                if not isinstance(a, dict):
                    continue
                if a.get('type') == 'InitializeVariable':
                    out[n] = a
                inits(a.get('actions'), out)
                for k in ('else', 'default'):
                    inits((a.get(k) or {}).get('actions'), out)
                for c in (a.get('cases') or {}).values():
                    inits(c.get('actions'), out)
            return out

        def placeholder(v):
            return v is None or (isinstance(v, str) and (not v.strip() or re.match(r'^<.*>$', v.strip())
                                 or re.search(r'(?i)REPLACE_WITH|PASTE[ _].*KEY|CONFIGURE_IN|^TODO_', v.strip())))
        secretish = re.compile(r'(?i)key|secret|token|password|pwd|credential')
        live = inits((live_def or {}).get('actions'), {})
        report = []
        for n, a in inits(pkg_def.get('actions'), {}).items():
            for v in (a.get('inputs') or {}).get('variables', []):
                if v.get('type') != 'string' or not placeholder(v.get('value')):
                    continue
                secure = bool((a.get('runtimeConfiguration') or {}).get('secureData'))
                if not (secure or (secretish.search(v.get('name', '')) and not re.search(r'(?i)tokens$', v.get('name', '')))):
                    continue
                outcome, length = 'live-missing', 0
                for lv in ((live.get(n) or {}).get('inputs') or {}).get('variables', []):
                    if lv.get('name') == v.get('name') and isinstance(lv.get('value'), str) and not placeholder(lv['value']):
                        v['value'], outcome, length = lv['value'], 'preserved', len(lv['value'])
                report.append((n, v.get('name'), outcome, length))
        return report

    def deploy(self, source_path, display_name=None, apply=False, start=True, flows=None, catalog=None, must_exist=False):
        pkg = read_flow_source(source_path)
        name = display_name or pkg['displayName']
        flows = self.list() if flows is None else flows
        existing = self.find(name, flows)
        if must_exist and existing is None:
            raise SystemExit('%s: mustExist -- no live flow of that name; refusing to CREATE a second copy of a '
                             'list-triggered flow (it would double-fire)' % name)
        full = self.get(existing['name']) if existing else None
        catalog = self.connection_catalog(flows) if catalog is None else catalog
        refs, missing, sources = self.resolve_references(pkg['connectionReferences'], full, catalog)
        if missing:
            raise SystemExit('%s: no connection in this environment for %s -- create one once in the maker portal' % (name, ', '.join(missing)))
        definition = copy.deepcopy(pkg['definition'])
        secrets = self.preserve_secrets(definition, ((full or {}).get('properties') or {}).get('definition'))
        plan = {'displayName': name, 'existing': existing['name'] if existing else None,
                'renamedFrom': existing['properties']['displayName'] if existing and existing['properties']['displayName'] != name else '',
                'sources': sources, 'secrets': [(a, v, o, l) for a, v, o, l in secrets]}
        if not apply:
            return plan
        body = {'properties': {'displayName': name, 'definition': definition}}
        if refs:
            body['properties']['connectionReferences'] = refs
        if existing:
            self.http.json('PATCH', self._u('flows/%s' % existing['name']), headers=self._h(), body=body)
            flow_id = existing['name']
        else:
            flow_id = self.http.json('POST', self._u('flows'), headers=self._h(), body=body)['name']
        if start:
            self.http.json('POST', self._u('flows/%s/start' % flow_id), headers=self._h(), body={}, allow_write_retry=True)
        plan.update({'id': flow_id, 'state': (self.get(flow_id).get('properties') or {}).get('state')})
        return plan

    def delete(self, flow_id):
        self.http.json('DELETE', self._u('flows/%s' % flow_id), headers=self._h())

    # ---------------------------------------------------------------------------------------- run and inspect
    def http_twin(self, flow_id, twin_name, transform=None):
        """Create/update an Http-trigger twin of a PowerApp-trigger flow so a script can call it. Returns twin id."""
        src = self.get(flow_id)
        d = copy.deepcopy(src['properties']['definition'])
        if 'manual' not in d.get('triggers', {}):
            raise SystemExit('%s has no manual trigger to twin' % flow_id)
        d['triggers']['manual']['kind'] = 'Http'
        if transform:
            transform(d)
        refs = {}
        for api, r in (src['properties'].get('connectionReferences') or {}).items():
            r = dict(r)
            r['source'] = 'Embedded'       # Invoker means nothing without a Power Apps caller
            refs[api] = r
        body = {'properties': {'displayName': twin_name, 'definition': d, 'connectionReferences': refs}}
        existing = self.find('=' + twin_name)
        if existing:
            self.http.json('PATCH', self._u('flows/%s' % existing['name']), headers=self._h(), body=body)
            tid = existing['name']
        else:
            tid = self.http.json('POST', self._u('flows'), headers=self._h(), body=body)['name']
        self.http.json('POST', self._u('flows/%s/start' % tid), headers=self._h(), body={}, allow_write_retry=True)
        return tid

    def callback_url(self, flow_id, trigger='manual'):
        r = self.http.json('POST', self._u('flows/%s/triggers/%s/listCallbackUrl' % (flow_id, trigger)), headers=self._h(),
                           body={}, allow_write_retry=True) or {}
        return (r.get('response') or {}).get('value') or r.get('value')

    def invoke(self, callback_url, payload, headers=None, timeout=300):
        """POST to a signed trigger URL (no bearer). Returns (status, parsed-json-or-text)."""
        h = {'Accept': 'application/json'}
        h.update(headers or {})
        r = self.http.request('POST', callback_url, headers=h, body=payload, timeout=timeout)
        try:
            return r.status, r.json()
        except ValueError:
            return r.status, r.text

    def runs(self, flow_id, top=5):
        return (self.http.json('GET', self._u('flows/%s/runs' % flow_id, '&$top=%d' % top), headers=self._h()) or {}).get('value') or []

    def run_actions(self, flow_id, run_id):
        out, url = [], self._u('flows/%s/runs/%s/actions' % (flow_id, run_id))
        while url:
            page = self.http.json('GET', url, headers=self._h()) or {}
            out.extend(page.get('value') or [])
            url = page.get('nextLink')
        return out

    def repetitions(self, flow_id, run_id, action):
        return (self.http.json('GET', self._u('flows/%s/runs/%s/actions/%s/repetitions' % (flow_id, run_id, action)),
                               headers=self._h()) or {}).get('value') or []

    def read_link(self, link_obj):
        """inputsLink/outputsLink -> parsed JSON. SAS-authorised: NO bearer header."""
        uri = (link_obj or {}).get('uri') if isinstance(link_obj, dict) else link_obj
        if not uri:
            return None
        r = self.http.request('GET', uri, headers={})
        try:
            return r.json()
        except ValueError:
            return r.text

    def failed_actions(self, flow_id, run_id):
        """[(name, status, code, message)] for every non-Succeeded/Skipped action -- the first stop when a run fails."""
        out = []
        for a in self.run_actions(flow_id, run_id):
            p = a.get('properties') or {}
            if p.get('status') not in ('Succeeded', 'Skipped'):
                err = p.get('error') or {}
                out.append((a.get('name'), p.get('status'), p.get('code') or err.get('code'), (err.get('message') or '')[:300]))
        return out

    def list_wadl(self, flow_id):
        r = self.http.request('POST', self._u('flows/%s/listWadl' % flow_id), headers=self.auth.headers('flow', {'Accept': 'application/xml'}),
                              body={}, allow_write_retry=True)
        return r.status, r.text

    # ---------------------------------------------------------------------------------------- approvals
    def approvals_waiting(self):
        """Approvals the signed-in user can answer. The $filter on userRole is mandatory. Pages oldest-first (follow nextLink)."""
        out = []
        url = self._u('approvalViews', "&$filter=properties/userRole eq 'Approver' and properties/isActive eq true")
        while url:
            page = self.http.json('GET', url, headers=self._h()) or {}
            out.extend(page.get('value') or [])
            url = page.get('nextLink')
        return out

    def answer_approval(self, approval_name, response, comments=''):
        """Answer an approval with no click (the Dataverse approval-response table is 403 for a plain maker; this is not).
        The waiting 'Wait for an approval' action resumes."""
        return self.http.json('POST', self._u('approvals/%s/approvalResponses' % approval_name), headers=self._h(),
                              body={'properties': {'response': response, 'comments': comments}})
