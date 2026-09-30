"""Headless canvas-app deploy into a DEV/TEST environment through the BAP package-import API.

PROVEN protocol (a commercial validation tenant; the proven run used a Power Apps-audience token from a different
first-party client -- see auth.py for the token caveat):

  storage  POST {bap}/providers/Microsoft.BusinessAppPlatform/environments/{env}/generateResourceStorage  -> container SAS
  upload   PUT  <container>/package.zip?<sas>  x-ms-blob-type: BlockBlob, NO bearer
  plan     POST .../listImportParameters {packageLink} -> 202 + Location, poll to Succeeded; answers the resource map AND a
           RE-STAGED properties.packageLink -- importPackage must use THAT link (the upload link -> 400 PackageManifestNotFound)
  import   POST .../importPackage {packageLink, details, resources(+selectedCreationType, id)} -> poll
  draft    import-as-Update lands as a DRAFT; a CREATE is a single version (published == draft)
  refs     the importer's draft has NO connection references and PATCH cannot reach a draft (only the published definition).
           Studio opens the draft -> everything "Not connected" -> its first save wipes the set. Write the draft like Studio
           does: POST apps/{id}/acquireLease -> PUT apps/{id}?api-version=2017-05-01 (full definition, lifeCycleId Draft,
           appUris.documentUri = the draft's own document) -> POST releaseLease.
  publish  POST {powerapps}/providers/Microsoft.PowerApps/apps/{id}/publish?api-version=2017-05-01 (2016-11-01 is refused).
           Publish PROMOTES the draft's references; the metadata PATCH (2017-05-01) is only a fallback repair after publish.

THE PACKAGE KEY: importPackage answers a bare InternalServerError unless the manifest's app resource key + folder id
were minted by the service's own exportPackage (opaque string match; the pair must match; reusable for any app name;
survives deletion of the source app). Mint once per environment by exporting ANY app (mint_package_key) and cache it.
DEAD ROUTES: direct POST/PATCH /apps (HTTP 500 for every body); editing an EXPORTED package's manifest (listImport
hangs or importPackage 400s on dangling connection keys) -- never ship connection resources inside the package.

Other measured rules: createdByClientVersion must have FOUR parts (a 3-part value is stored with revision -1 and the
runtime packager refuses it: "Authoring tool version ... is invalid" -> "This app couldn't be published"); app display
names cannot contain . \\ / : * ? " < > | (InvalidApplicationNameCharacters on every later full PUT); an app opened in
Studio holds a lease (409 AppLeaseActive, up to ~15 min after the tab closes) -- wait, never deploy a renamed copy.
"""
import io
import json
import os
import re
import time
import urllib.parse
import uuid
import zipfile

from . import canvasdoc
from .http import HttpError

API = '2016-11-01'
API_DEF = '2017-05-01'
FALLBACK_VERSION = {'major': 3, 'minor': 26091, 'build': 11, 'revision': 0}
BAD_NAME = re.compile(r'[.\\/:*?"<>|]')


class PowerAppsClient(object):
    def __init__(self, cfg, auth, http, sleep=time.sleep, log=print):
        self.cfg, self.auth, self.http, self.sleep, self.log = cfg, auth, http, sleep, log
        self.env = cfg['environmentId']
        self.base = cfg.hosts['powerApps'].rstrip('/') + '/providers/Microsoft.PowerApps'
        self.bap = cfg.hosts['bap'].rstrip('/') + '/providers/Microsoft.BusinessAppPlatform/environments/' + self.env

    def _h(self):
        return self.auth.headers('powerApps')

    def api(self, method, url, body=None, ok=(200, 201, 202, 204), **kw):
        return self.http.json(method, url, headers=self._h(), body=body, ok=ok, **kw)

    # ---------------------------------------------------------------------------------- read
    def apps(self):
        out, url = [], "%s/apps?api-version=%s&$filter=environment eq '%s'&$top=250" % (self.base, API, self.env)
        while url:
            page = self.api('GET', url) or {}
            out.extend(page.get('value') or [])
            url = page.get('nextLink')
        return out

    def find(self, display_name, apps=None, name='', owner=''):
        """A pinned id wins; else exact display name; several hits need an owner filter or it is an error
        (never write to someone else's same-named app)."""
        apps = self.apps() if apps is None else apps
        if name:
            for a in apps:
                if a['name'] == name:
                    return a
            raise SystemExit('%s: no app with id %s is visible here' % (display_name, name))
        hits = [a for a in apps if (a.get('properties') or {}).get('displayName') == display_name]
        if len(hits) > 1 and owner:
            hits = [a for a in hits if owner.lower() in json.dumps((a.get('properties') or {}).get('owner') or {}).lower()]
        if len(hits) > 1:
            raise SystemExit('%s matches %d apps -- pin "name" (app id) or "owner" in the manifest: %s' % (
                display_name, len(hits), ', '.join(a['name'] for a in hits)))
        return hits[0] if hits else None

    def get(self, app_id, draft=False):
        url = '%s/apps/%s?api-version=%s' % (self.base, app_id, API_DEF)
        return self.api('GET', url + ('&$expand=unpublishedAppDefinition' if draft else ''))

    def connector(self, api_name):
        """displayName, iconUri, tier and changedTime of a connector (tier drives the premium rule)."""
        try:
            rec = self.api('GET', "%s/apis/%s?api-version=%s&$filter=environment eq '%s'" % (self.base, api_name, API, self.env))
        except HttpError:
            return None
        p = (rec or {}).get('properties') or {}
        return {'displayName': p.get('displayName'), 'iconUri': p.get('iconUri'), 'tier': p.get('tier'), 'changedTime': p.get('changedTime')}

    def connection_runtime_host(self, connection_name, api='shared_sharepointonline'):
        rec = self.api('GET', "%s/apis/%s/connections/%s?api-version=%s&$filter=environment eq '%s'" % (self.base, api, connection_name, API, self.env))
        for link in ((rec or {}).get('properties') or {}).get('testLinks') or []:
            m = re.match(r'^(https://[^/]+)/apim/', link.get('requestUri', ''))
            if m:
                return m.group(1)
        return ''

    def download_document(self, app_id, out_path, draft=False):
        """'Download a copy' headless: the live (or draft) document, byte-for-byte what Studio saved last -- including
        Studio's App checker result (read it with tools/appchecker-sarif.py). SAS URL: no bearer."""
        app = self.get(app_id, draft)
        props = app['properties']
        if draft and props.get('unpublishedAppDefinition'):
            props = props['unpublishedAppDefinition']['properties']
        uri = ((props.get('appUris') or {}).get('documentUri') or {}).get('value')
        r = self.http.request('GET', uri, headers={})
        if r.status != 200:
            raise HttpError('GET', 'documentUri', r.status, r.text)
        with open(out_path, 'wb') as fh:
            fh.write(r.body)
        return out_path

    # ---------------------------------------------------------------------------------- BAP plumbing
    def _bap(self, method, path, body=None, allow_write_retry=False):
        r = self.http.request(method, '%s/%s?api-version=%s' % (self.bap, path, API), headers=self._h(), body=body,
                              allow_write_retry=allow_write_retry)
        if r.status >= 400:
            raise HttpError(method, path, r.status, r.text)
        return r

    def _wait(self, r, timeout=900):
        if r.status != 202 or not r.headers.get('location'):
            return r.json()
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sleep(5)
            p = self.http.request('GET', r.headers['location'], headers=self._h())
            if p.status == 200 and re.search(r'"status"\s*:\s*"(Succeeded|Failed)"', p.text):
                return p.json()
        raise SystemExit('BAP operation did not finish within %ds' % timeout)

    @staticmethod
    def _status(op):
        return ((op or {}).get('properties') or {}).get('status') or (op or {}).get('status')

    # ---------------------------------------------------------------------------------- package key
    def mint_package_key(self, apps=None):
        """Export ANY app once (reads only) to learn a (resourceKey, folderId) pair the importer accepts."""
        last = ''
        for app in (self.apps() if apps is None else apps):
            aid = '/providers/Microsoft.PowerApps/apps/' + app['name']
            try:
                lpr = self._wait(self._bap('POST', 'listPackageResources', {'baseResourceIds': [aid]}))
                res = (lpr or {}).get('resources') or ((lpr or {}).get('properties') or {}).get('resources')
                if not res:
                    raise SystemExit('listPackageResources enumerated nothing')
                for v in res.values():
                    v.setdefault('suggestedCreationType', 'New')
                op = self._wait(self._bap('POST', 'exportPackage', {
                    'includedResourceIds': [aid], 'resources': res,
                    'details': {'displayName': 'package key mint', 'description': 'exported only to mint a package key',
                                'creator': 'devtenant', 'sourceEnvironment': self.env}}))
                if self._status(op) != 'Succeeded':
                    raise SystemExit('exportPackage did not succeed')
                link = (((op.get('properties') or {}).get('packageLink')) or op.get('packageLink') or {}).get('value')
                blob = self.http.request('GET', link, headers={})
                with zipfile.ZipFile(io.BytesIO(blob.body)) as z:
                    manifest = json.loads(z.read('manifest.json').decode('utf-8-sig'))
                    key = next(k for k, v in manifest['resources'].items() if v.get('type') == 'Microsoft.PowerApps/apps')
                    folder = next(n.split('/')[2] for n in z.namelist() if n.replace('\\', '/').startswith('Microsoft.PowerApps/apps/'))
                return {'resourceKey': key, 'folderId': folder, 'mintedFrom': app['properties'].get('displayName')}
            except (HttpError, SystemExit, StopIteration, KeyError) as ex:
                last = '%s: %s' % (app['properties'].get('displayName'), ex)
        raise SystemExit('could not mint a package key from any app; import one app by hand first. Last error: ' + last)

    def package_key(self, apps=None, mint=True):
        path = self.cfg.package_key_cache
        cache = json.load(open(path, encoding='utf-8')) if os.path.isfile(path) else {}
        if self.env in cache:
            return cache[self.env]
        if not mint:
            return None
        key = self.mint_package_key(apps)
        cache[self.env] = key
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(cache, fh, indent=1)
        return key

    # ---------------------------------------------------------------------------------- package build and import
    @staticmethod
    def build_package(msapp_path, display_name, key, out_zip, description='', source_env=''):
        """.msapp -> legacy import package (layout transcribed from a service export). Nothing about connections goes in."""
        if BAD_NAME.search(display_name):
            raise SystemExit('app display name %r contains a character the service refuses (. \\ / : * ? " < > |)' % display_name)
        with zipfile.ZipFile(msapp_path) as z:
            props = json.loads(z.read(next(n for n in z.namelist() if n.replace('\\', '/') == 'Properties.json')).decode('utf-8-sig'))
        width, height = str(props.get('DocumentLayoutWidth') or '1366'), str(props.get('DocumentLayoutHeight') or '768')
        phone = props.get('DocumentLayoutOrientation') == 'portrait' or int(width) < int(height)
        folder, rkey = key['folderId'], key['resourceKey']
        doc, ident = 'N%s-document.msapp' % uuid.uuid4(), 'N%s-identity.json' % uuid.uuid4()
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        manifest = {'schema': '1.0', 'details': {'displayName': display_name, 'description': description, 'createdTime': now,
                                                 'packageTelemetryId': str(uuid.uuid4()), 'creator': 'devtenant',
                                                 'sourceEnvironment': source_env},
                    'resources': {rkey: {'type': 'Microsoft.PowerApps/apps', 'suggestedCreationType': 'New',
                                         'creationType': 'New, Update', 'details': {'displayName': display_name},
                                         'configurableBy': 'User', 'dependsOn': []}}}
        app_json = {'schemaVersion': '1.0', 'appDefinitionTemplate': {
            'id': '/providers/Microsoft.PowerApps/apps/', 'type': 'Microsoft.PowerApps/apps',
            'tags': {'primaryDeviceWidth': width, 'primaryDeviceHeight': height, 'supportsPortrait': 'true',
                     'supportsLandscape': 'true', 'primaryFormFactor': 'Phone' if phone else 'Tablet', 'showStatusBar': 'false',
                     'minimumRequiredApiVersion': '2.2.0', 'hasComponent': 'false', 'hasUnlockedComponent': 'false',
                     'isUnifiedRootApp': 'false'},
            'properties': {'appVersion': now, 'lifeCycleId': 'Published', 'displayName': display_name, 'description': description,
                           'commitMessage': '', 'appUris': {'documentUri': {'value': '/%s/%s' % (folder, doc)}, 'imageUris': [],
                                                             'additionalUris': [{'isSolutionAware': False, 'value': '/%s/%s' % (folder, ident)}]},
                           'connectionReferences': {}, 'databaseReferences': {}, 'almMode': 'Environment'},
            'isAppComponentLibrary': False, 'appType': 'ClassicCanvasApp', 'appComponents': []}}
        base = 'Microsoft.PowerApps/apps/%s/' % folder
        with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('manifest.json', json.dumps(manifest, indent=2))
            z.writestr(base + folder + '.json', json.dumps(app_json, indent=2))
            z.write(msapp_path, base + doc)
            z.writestr(base + ident, '{"__Version":"0.1"}')
        return out_zip

    def import_package(self, zip_path, app_id=None, apply=False):
        sas = (self._bap('POST', 'generateResourceStorage', {}).json() or {}).get('sharedAccessSignature')
        container, query = sas.split('?', 1)
        blob = container.rstrip('/') + '/package.zip?' + query          # fixed name: a retried PUT is an idempotent overwrite
        with open(zip_path, 'rb') as fh:
            put = self.http.request('PUT', blob, headers={'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'application/octet-stream'},
                                    raw_body=fh.read(), allow_write_retry=True)
        if put.status >= 400:
            raise HttpError('PUT', 'package upload', put.status, put.text)
        params = self._wait(self._bap('POST', 'listImportParameters', {'packageLink': {'value': blob}}))
        if self._status(params) != 'Succeeded':
            raise SystemExit('listImportParameters failed: %s' % json.dumps(params)[:600])
        resources = params['properties']['resources']
        plan = {}
        for k, res in resources.items():
            t = res.get('type')
            if t == 'Microsoft.PowerApps/apps':
                res['selectedCreationType'] = 'Update' if app_id else 'New'
                if app_id:
                    res['id'] = '/providers/Microsoft.PowerApps/apps/' + app_id
            elif t == 'Microsoft.PowerApps/apis':
                res['selectedCreationType'] = 'Existing'
            else:
                raise SystemExit('the package carries a %s resource -- never ship connections/flows inside an app package' % t)
            plan[k] = (t, res['selectedCreationType'], res.get('id', ''))
        if not apply:
            return {'dryRun': True, 'plan': plan}
        staged = params['properties'].get('packageLink') or {'value': blob}
        op = self._wait(self._bap('POST', 'importPackage', {'packageLink': staged, 'details': params['properties'].get('details'),
                                                            'resources': resources}, allow_write_retry=True))
        if self._status(op) != 'Succeeded':
            raise SystemExit('importPackage finished %s (InternalServerError with no detail = the package key pair is not '
                             'service-minted for this environment): %s' % (self._status(op), json.dumps(op)[:800]))
        new_id = app_id
        for res in ((op.get('properties') or {}).get('resources') or {}).values():
            if res.get('type') == 'Microsoft.PowerApps/apps' and res.get('id'):
                new_id = res['id'].split('/apps/')[-1]
        return {'dryRun': False, 'appId': new_id, 'plan': plan}

    # ---------------------------------------------------------------------------------- draft refs, publish, verify
    def write_draft_references(self, app_id, references, display_name=''):
        """Leased full-definition PUT onto the DRAFT (what Studio's save does). Returns False when there is no separate draft."""
        state = self.get(app_id, draft=True)
        draft = (state.get('properties') or {}).get('unpublishedAppDefinition')
        if not draft or not draft.get('properties'):
            return False
        dp, pp = draft['properties'], state['properties']
        doc = ((dp.get('appUris') or {}).get('documentUri') or {}).get('value')
        if not doc:
            raise SystemExit('%s: the draft carries no documentUri' % app_id)

        def version(field):
            for src in (dp, pp):
                v = src.get(field) or {}
                if v.get('major', 0) > 0 and v.get('revision', 0) >= 0:
                    return dict(v, revision=v.get('revision', 0))
            return dict(FALLBACK_VERSION)

        pick = lambda f: dp.get(f) if dp.get(f) is not None else pp.get(f)  # noqa: E731
        body = {'tags': state.get('tags'), 'appType': state.get('appType') or 'ClassicCanvasApp', 'properties': {
            'displayName': display_name or pick('displayName'), 'description': pick('description') or '',
            'backgroundColor': pick('backgroundColor') or '', 'environment': {'id': pp['environment']['id'], 'name': pp['environment']['name']},
            'connectionReferences': references, 'minClientVersion': version('minClientVersion'),
            'createdByClientVersion': version('createdByClientVersion'), 'lifeCycleId': 'Draft',
            'appUris': {'documentUri': {'value': doc}}}}
        url = '%s/apps/%s' % (self.base, app_id)
        try:
            lease = self.api('POST', url + '/acquireLease?api-version=' + API_DEF, {})
        except HttpError as ex:
            if 'AppLeaseSameUserConflict' in ex.body or 'AppLeaseActive' in ex.body:
                raise SystemExit('%s: the app is open in Studio (editing lease held). Close it, wait for the lease to expire '
                                 '(up to ~15 min), re-run. Do NOT deploy a renamed copy instead.' % (display_name or app_id))
            raise
        try:
            self.api('PUT', url + '?api-version=' + API_DEF, body)
        finally:
            try:
                self.api('POST', url + '/releaseLease?api-version=' + API_DEF, {'leaseId': lease.get('leaseId')})
            except HttpError as ex:
                self.log('WARN releaseLease failed (%s); the lease expires on its own' % ex)
        return True

    def publish(self, app_id):
        self.api('POST', '%s/apps/%s/publish?api-version=%s' % (self.base, app_id, API_DEF), {}, allow_write_retry=True)

    def patch_references(self, app_id, references, attempts=6):
        """Fallback repair of the PUBLISHED definition only. Publish briefly holds the lease: retry AppLeaseActive."""
        for i in range(1, attempts + 1):
            try:
                return self.api('PATCH', '%s/apps/%s?api-version=%s' % (self.base, app_id, API_DEF),
                                {'properties': {'connectionReferences': references}})
            except HttpError as ex:
                if 'AppLeaseActive' in ex.body and i < attempts:
                    self.sleep(min(30, 5 * i))
                    continue
                raise

    @staticmethod
    def assert_references(expected, app):
        actual = ((app or {}).get('properties') or {}).get('connectionReferences') or {}
        for k, want in expected.items():
            got = actual.get(k)
            if not got or not got.get('id'):
                raise SystemExit('lost app connection reference %s; not treating the deploy as complete' % k)
            if got.get('id') != want.get('id'):
                raise SystemExit('wrong connector for %s' % k)
            for ds in want.get('dataSources') or []:
                if ds not in (got.get('dataSources') or []):
                    raise SystemExit('lost data source %s' % ds)
            wf = ((want.get('parameterHints') or {}).get('workflowName') or {}).get('value')
            if wf and ((got.get('parameterHints') or {}).get('workflowName') or {}).get('value') != wf:
                raise SystemExit('wrong workflow bound for %s' % k)

    # ---------------------------------------------------------------------------------- runtime package (player) check
    def environment_host(self):
        """<'default' if default env><guid hex minus last 2>.<last 2>.environment.api.powerplatform.com (commercial: split 2)."""
        eid = self.env.lower()
        pre = ''
        if eid.startswith('default-'):
            pre, eid = 'default', eid[8:]
        h = eid.replace('-', '')
        return 'https://%s%s.%s.environment.%s' % (pre, h[:-2], h[-2:], self.cfg.hosts['powerPlatformApiSuffix'])

    def launch(self, app_id, timeout=600):
        """The PLAYER's launch call: the only place a broken runtime package shows (packageStatus, error text).
        The maker API calls such an app Published. Polls while InProgress. Returns the launch JSON."""
        url = '%s/powerapps/apps/%s/launch?api-version=2&bypass-cache=true' % (self.environment_host(), app_id)
        deadline = time.time() + timeout
        while True:
            r = self.api('POST', url, {}, allow_write_retry=True)
            det = (r or {}).get('listAppPackageOperationDetails') or {}
            if det.get('packageStatus') != 'InProgress' or time.time() > deadline:
                return r
            self.sleep(max(5, int(det.get('retryAfter') or 10)))


def deploy_app(pa, fc, msapp_path, display_name, apply=False, publish=True, name='', owner='', allow_create=False,
               refresh_schemas=True, refresh_signatures=True, sp_auth_headers=None, work_dir=None, log=print):
    """One app end to end. Returns a report dict. Order matters: the FLOWS the app calls must already be deployed
    (update-in-place keeps their GUIDs) -- the app binds them by GUID."""
    work_dir = work_dir or os.path.join(os.path.expanduser('~'), '.pp-playbook', 'work')
    os.makedirs(work_dir, exist_ok=True)
    apps = pa.apps()
    target = pa.find(display_name, apps, name, owner)
    if target is None and apply and not allow_create:
        raise SystemExit('%s: no existing app matched; set allowCreate:true (first deploy) or pin "name"' % display_name)
    doc = canvasdoc.Msapp(msapp_path)
    lines = []
    flows = fc.list()
    dep_map = fc.dependency_map(flows)
    apis = sorted({a for v in dep_map.values() for a in v})
    samples = {}
    catalog = fc.connection_catalog(flows)
    for api in apis + ['shared_logicflows']:
        meta = pa.connector(api) or {}
        samples[api] = dict(meta, connectionName=(catalog.get(api) or {}).get('reference', {}).get('connectionName'))
    if refresh_signatures:
        live = {}
        for ds in doc.data_sources():
            fid = (ds.get('FlowNameId') or '').lower()
            if ds.get('ServiceKind') != 'ConnectedWadl' or not fid or fid in live:
                continue
            status, text = fc.list_wadl(fid)
            if status == 200:
                live[fid] = text
            elif 'FlowWadlConversionNotSupported' in text:
                sig = canvasdoc.definition_signature(fc.get(fid)['properties']['definition'])
                live[fid] = {'unsupported': True, 'inputs': sig['inputs'], 'outputs': sig['outputs']}
            else:
                raise SystemExit('listWadl %s -> HTTP %s (flow not deployed here? deploy flows first): %s' % (fid, status, text[:300]))
        lines += canvasdoc.refresh_flow_signatures(doc, live) if live else []
    if refresh_schemas and canvasdoc.sharepoint_tables(doc):
        meta_by_table = {}
        changed = canvasdoc.cdp_time((pa.connector('shared_sharepointonline') or {}).get('changedTime') or '')
        for t in canvasdoc.sharepoint_tables(doc):
            if t['table'] in meta_by_table:
                continue
            host = pa.connection_runtime_host(t['connection'])
            site = urllib.parse.quote(urllib.parse.quote(t['dataset'], safe=''), safe='')   # DOUBLE-encoded: load-bearing
            r = pa.http.request('GET', '%s/apim/sharepointonline/%s/$metadata.json/datasets/%s/tables/%s' % (host, t['connection'], site, t['table']),
                                headers=sp_auth_headers or pa.auth.headers('apihub'))
            if r.status != 200:
                raise SystemExit('table metadata %s -> HTTP %s' % (t['name'], r.status))
            meta_by_table[t['table']] = r.text
        lines += canvasdoc.refresh_table_schemas(doc, meta_by_table, changed)
    _refs, references, rep = canvasdoc.runtime_references(doc, dep_map, samples, (samples.get('shared_logicflows') or {}).get('iconUri', ''))
    if rep['premiumBound']:
        raise SystemExit('%s binds premium connector(s) directly: %s -- the app would need premium licences' % (display_name, rep['premiumBound']))
    for s in rep['premiumSkipped']:
        lines.append('WARN premium connector NOT wired into the app: %s (that flow call will fail from the app)' % s)
    for s in rep['leftForStudio']:
        lines.append('WARN left for Studio (no connection/displayName/iconUri known): %s' % s)
    prepared = os.path.join(work_dir, re.sub(r'[^A-Za-z0-9_-]', '_', display_name) + '.prepared.msapp')
    doc.set_refs(_refs)
    doc.save(prepared)
    key = pa.package_key(apps, mint=apply)
    if key is None:
        lines.append('PLAN no package key cached for this environment yet; -Apply mints one by exporting any app (read-only)')
        return {'displayName': display_name, 'target': target and target['name'], 'dryRun': True, 'lines': lines}
    zip_path = pa.build_package(prepared, display_name, key, prepared.replace('.msapp', '.package.zip'), source_env=pa.env)
    res = pa.import_package(zip_path, target and target['name'], apply)
    if not apply:
        return {'displayName': display_name, 'target': target and target['name'], 'dryRun': True, 'plan': res['plan'], 'lines': lines}
    app_id = res['appId']
    wrote = pa.write_draft_references(app_id, references, display_name) if references else False
    if wrote:
        pa.assert_references(references, pa.get(app_id, draft=True)['properties']['unpublishedAppDefinition'])
    elif not publish:
        raise SystemExit('%s: the import produced no separate draft to write references into' % display_name)
    if publish:
        pa.publish(app_id)
        try:
            pa.assert_references(references, pa.get(app_id))
        except SystemExit as ex:
            lines.append('WARN publish did not carry the references (%s); repairing with the metadata PATCH' % ex)
            pa.patch_references(app_id, references)
            pa.assert_references(references, pa.get(app_id))
    else:
        lines.append('NOTE draft only: publish it before anyone opens it in Studio')
    return {'displayName': display_name, 'appId': app_id, 'created': target is None, 'published': publish, 'lines': lines}
