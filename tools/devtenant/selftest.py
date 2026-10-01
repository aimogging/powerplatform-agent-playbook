"""Offline self-test for devtenant: every module against a fake transport. No network, no tokens.

    cd tools && python -m devtenant self-test
"""
import io
import json
import os
import tempfile
import zipfile

from . import auth as authmod
from . import canvasdoc, census, config, runtime_rules
from .flows import FlowClient, read_flow_source
from .http import FakeTransport, Http, Response
from .powerapps import FALLBACK_VERSION, PowerAppsClient
from .sharepoint import SharePoint, field_xml

ENV = 'Default-00000000-0000-0000-0000-000000000000'
CFG = config.from_dict({'tenantId': '00000000-0000-0000-0000-000000000000', 'environmentId': ENV,
                        'siteUrl': 'https://contoso.sharepoint.com/sites/HelpDesk'})
OK = []


def check(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    OK.append(bool(cond))


class FakeAuth(object):
    def headers(self, key, extra=None):
        h = {'Authorization': 'Bearer test-' + key, 'Accept': 'application/json'}
        h.update(extra or {})
        return h


def http_with(routes):
    t = FakeTransport(routes)
    return Http(t, sleep=lambda s: None, log=lambda *a: None), t


def test_http():
    calls = {'n': 0}

    def flaky(m, u, h, d):
        calls['n'] += 1
        return '{"ok":true}' if calls['n'] >= 3 else None
    h, t = http_with([('GET', '/flaky', 200, flaky)])
    t.routes = [('GET', '/flaky', 503, '{}', {'Retry-After': '1'})] * 2 + t.routes
    seq = iter([503, 503, 200])

    class Seq(object):
        def send(self, method, url, headers, data, timeout):
            s = next(seq)
            return Response(s, {'Retry-After': '1'} if s == 503 else {}, '{"ok":true}' if s == 200 else '')
    h2 = Http(Seq(), sleep=lambda s: None, log=lambda *a: None)
    check(h2.json('GET', 'https://example.com/y') == {'ok': True}, 'http: GET retries 503 with Retry-After, then succeeds')
    seq2 = iter([500, 200])

    class Seq2(object):
        def send(self, method, url, headers, data, timeout):
            return Response(next(seq2), {}, '')
    h3 = Http(Seq2(), sleep=lambda s: None, log=lambda *a: None)
    check(h3.request('POST', 'https://example.com/y', body={}).status == 500, 'http: a write is NOT retried after a 500 (may have applied)')
    import gzip as _gz
    check(Response(200, {}, _gz.compress(b'{"app": ["a.js"]}')).json() == {'app': ['a.js']},
          'http: a gzip body without Content-Encoding is decoded (runtime package blobs, D-23)')
    from .powerapps import retry_seconds
    check(retry_seconds('PT10S') == 10 and retry_seconds('PT1M5S') == 65 and retry_seconds('7') == 7 and retry_seconds(None) == 10,
          'powerapps: launch retryAfter accepts ISO-8601 durations and seconds (D-23)')


def test_auth():
    with tempfile.TemporaryDirectory() as d:
        cfg = config.from_dict(dict(CFG, tokenCache=os.path.join(d, 'cache.json')))
        cache = authmod.TokenCache(cfg.token_cache)
        cache.data['refresh'][cfg.clients['flow']] = 'rt-old'
        grants = []

        def token(m, u, h, data):
            grants.append(data.decode())
            return {'access_token': 'a.eyJhdWQiOiJodHRwczovL3NlcnZpY2UuZmxvdy5taWNyb3NvZnQuY29tLyJ9.s', 'refresh_token': 'rt-new', 'expires_in': 3600}
        h, _t = http_with([('POST', '/oauth2/v2.0/token', 200, token)])
        a = authmod.Auth(cfg, h, cache, clock=lambda: 1000.0, out=lambda *x: None)
        tok = a.token('flow', interactive=False)
        check('refresh_token=rt-old' in grants[0] and 'service.flow.microsoft.com' in grants[0], 'auth: refresh grant uses the cached token and the Flow scope')
        check(json.load(open(cfg.token_cache))['refresh'][cfg.clients['flow']] == 'rt-new', 'auth: the ROTATED refresh token is persisted')
        check(authmod.claims(tok).get('aud') == 'https://service.flow.microsoft.com/', 'auth: JWT claims decode (audience check)')
        check(not cfg.token_cache.startswith(config.REPO), 'auth: token cache lives outside the repo')
        h2, _t2 = http_with([('POST', '/oauth2/v2.0/token', 400, {'error': 'invalid_grant', 'error_description': 'AADSTS65002: preauthorization'})])
        cache.data['access'] = {}
        a2 = authmod.Auth(cfg, h2, cache, clock=lambda: 1000.0, out=lambda *x: None)
        try:
            a2.token('powerApps', interactive=False)
            hit = False
        except SystemExit as ex:
            hit = 'not preauthorized' in str(ex) or 'no cached token' in str(ex)
        check(hit, 'auth: AADSTS65002 / missing token explains which client/audience to fix')


def test_sharepoint():
    xml = field_xml({'name': 'Status', 'type': 'Choice', 'choices': ['New', 'Closed'], 'default': 'New'})
    check('<CHOICE>New</CHOICE>' in xml and 'Type="Choice"' in xml and '<Default>New</Default>' in xml, 'sharepoint: Choice field XML')
    fields = [{'InternalName': 'Title', 'Title': 'Title', 'SchemaXml': ''},
              {'InternalName': 'Status', 'Title': 'Status', 'SchemaXml': '<Field><CHOICES><CHOICE>New</CHOICE></CHOICES></Field>'},
              {'InternalName': '_x0053_ha256', 'Title': 'Sha256', 'SchemaXml': ''}]
    routes = [('GET', "$filter=Title eq 'Tickets'", 200, {'value': [{'Id': 'L1', 'Title': 'Tickets', 'ListItemEntityTypeFullName': 'SP.Data.TicketsListItem'}]}),
              ('GET', "lists(guid'L1')/fields", 200, {'value': fields}),
              ('POST', '/contextinfo', 200, {'FormDigestValue': 'digest'}),
              ('POST', '/_api/', 200, {})]
    h, t = http_with(routes)
    sp = SharePoint(CFG, FakeAuth(), h)
    spec = {'title': 'Tickets', 'titleDisplayName': 'Subject', 'fields': [
        {'name': 'Status', 'type': 'Choice', 'choices': ['New', 'Triaged']},
        {'name': 'Priority', 'type': 'Choice', 'choices': ['Low', 'High']},
        {'name': 'Sha256', 'type': 'Text'}]}
    _row, actions = sp.plan_list(spec)
    kinds = [(k, n) for k, n, _a in actions]
    check(('append-choices', 'Status') in kinds and ('create-field', 'Priority') in kinds, 'sharepoint: audit appends missing choices and creates missing fields')
    check(('mangled', 'Sha256') in kinds, 'sharepoint: a mangled internal name (_x0053_ha256) is reported, never twinned')
    check(('rename-title', 'Subject') in kinds, 'sharepoint: Title display-name rename planned')
    try:
        sp.apply_list(spec, actions)
        refused = False
    except SystemExit:
        refused = True
    check(refused, 'sharepoint: apply refuses while a mangled field is in the plan')
    ok_actions = [x for x in actions if x[0] != 'mangled']
    sp.apply_list(spec, ok_actions)
    posts = [c for c in t.calls if c[0] == 'POST' and 'contextinfo' not in c[1]]
    check(any('CreateFieldAsXml' in c[1] and b'"Options":24' in c[3] for c in posts), 'sharepoint: CreateFieldAsXml with Options 24')
    check(all(c[2].get('X-RequestDigest') == 'digest' for c in posts), 'sharepoint: every write carries X-RequestDigest')
    check(any(c[2].get('X-HTTP-Method') == 'MERGE' and c[2].get('IF-MATCH') == '*' for c in posts), 'sharepoint: MERGE + IF-MATCH * for updates')
    check(sp.server_relative('Shared Documents/a') == '/sites/HelpDesk/Shared Documents/a', 'sharepoint: library-relative -> server-relative')
    h2, t2 = http_with([('POST', '/contextinfo', 200, {'FormDigestValue': 'd'}), ('POST', 'addUsingPath', 200, {})])
    SharePoint(CFG, FakeAuth(), h2).ensure_folder('Shared Documents/a/b/c')
    made = [c[1] for c in t2.calls if 'addUsingPath' in c[1]]
    check(len(made) == 3 and all('overwrite=true' in u for u in made), 'sharepoint: ensure_folder creates each level (addUsingPath is single-level)')


def _flow_def(kind='PowerApp'):
    return {'triggers': {'manual': {'type': 'Request', 'kind': kind, 'inputs': {'schema': {'type': 'object', 'properties': {
        'payload': {'type': 'string', 'x-ms-dynamically-added': True, 'description': 'p'}}, 'required': ['payload']}}}},
        'actions': {'Init_Key': {'type': 'InitializeVariable', 'inputs': {'variables': [{'name': 'ApiKey', 'type': 'string', 'value': '<API_KEY>'}]}},
                    'Respond': {'type': 'Response', 'kind': 'PowerApp', 'inputs': {'schema': {'properties': {'result_json': {'type': 'string', 'x-ms-dynamically-added': True}}}}}}}


def test_flows():
    live = {'name': 'F1', 'properties': {'displayName': 'Submit Ticket', 'definition': {'actions': {'Init_Key': {
        'type': 'InitializeVariable', 'inputs': {'variables': [{'name': 'ApiKey', 'type': 'string', 'value': 'real-secret'}]}}}},
        'connectionReferences': {'shared_sharepointonline': {'connectionName': 'conn-live', 'id': '/providers/Microsoft.PowerApps/apis/shared_sharepointonline', 'source': 'Invoker'}}}}
    routes = [('GET', '/flows?', 200, {'value': [{'name': 'F1', 'properties': {'displayName': 'Submit Ticket'}}]}),
              ('GET', '/flows/F1?', 200, live),
              ('PATCH', '/flows/F1?', 200, {'name': 'F1'}),
              ('POST', '/flows/F1/start', 200, {})]
    h, t = http_with(routes)
    fc = FlowClient(CFG, FakeAuth(), h)
    check(fc.find('SubmitTicket')['name'] == 'F1', 'flows: display name matched ignoring spaces/case (renamed in place)')
    check(fc.find('=SubmitTicket') is None, "flows: '=Name' is an exact-only selector")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, 'f'))
        json.dump({'properties': {'displayName': 'SubmitTicket', 'definition': _flow_def(),
                                  'connectionReferences': {'shared_sharepointonline': {'connectionName': 'shared_sharepointonline'}}}},
                  open(os.path.join(d, 'f', 'definition.json'), 'w'))
        plan = fc.deploy(os.path.join(d, 'f'))
        check(plan['existing'] == 'F1' and plan['sources']['shared_sharepointonline'] == 'existing flow', 'flows: connection reference taken from the live flow (placeholder never used)')
        check(plan['secrets'] and plan['secrets'][0][2] == 'preserved', 'flows: a placeholder secret keeps the live value (value never printed)')
        check(not any(c[0] == 'PATCH' for c in t.calls), 'flows: plan writes nothing')
        fc.deploy(os.path.join(d, 'f'), apply=True)
        patch = [c for c in t.calls if c[0] == 'PATCH'][0]
        body = json.loads(patch[3])
        check(body['properties']['connectionReferences']['shared_sharepointonline']['connectionName'] == 'conn-live', 'flows: PATCH keeps the GUID and the live connection')
        check('real-secret' in patch[3].decode(), 'flows: preserved secret written back on update')
        try:
            h2, _t2 = http_with([('GET', '/flows?', 200, {'value': []})])
            FlowClient(CFG, FakeAuth(), h2).deploy(os.path.join(d, 'f'), must_exist=True)
            refused = False
        except SystemExit:
            refused = True
        check(refused, 'flows: mustExist refuses to create a second copy')
    refs, missing, _s = FlowClient.resolve_references({'shared_office365': {'connectionName': 'shared_office365'}}, None, {})
    check(missing == ['shared_office365'], 'flows: a connector with no real connection anywhere is reported missing')
    twin_routes = [('GET', '/flows/F1?', 200, {'name': 'F1', 'properties': {'displayName': 'Submit Ticket', 'definition': _flow_def(),
                                                                        'connectionReferences': {'shared_sharepointonline': {'connectionName': 'c', 'source': 'Invoker'}}}}),
                   ('GET', '/flows?', 200, {'value': []}), ('POST', '/flows?', 201, {'name': 'T1'}), ('POST', '/flows/T1/start', 200, {})]
    h3, t3 = http_with(twin_routes)
    FlowClient(CFG, FakeAuth(), h3).http_twin('F1', 'Submit Ticket [http twin]')
    created = json.loads([c for c in t3.calls if c[0] == 'POST' and c[1].split('?')[0].endswith('/flows')][0][3])
    check(created['properties']['definition']['triggers']['manual']['kind'] == 'Http'
          and created['properties']['connectionReferences']['shared_sharepointonline']['source'] == 'Embedded',
          'flows: Http twin = trigger kind Http + Embedded connections')
    h4, t4 = http_with([('POST', '/approvalResponses', 200, {'properties': {'status': 'Committed'}})])
    FlowClient(CFG, FakeAuth(), h4).answer_approval('A1', 'Approve', 'ok')
    check(json.loads(t4.calls[0][3]) == {'properties': {'response': 'Approve', 'comments': 'ok'}}, 'flows: approval answered via approvalResponses')


def _msapp(path, refs, data_sources, src=None, packed=False):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('Header.json', '{}')
        z.writestr('Properties.json', json.dumps({'DocumentLayoutWidth': 1366, 'DocumentLayoutHeight': 768,
                                                  'LocalConnectionReferences': json.dumps(refs)}))
        z.writestr('References/DataSources.json', json.dumps({'DataSources': data_sources}))
        for n, t in (src or {}).items():
            z.writestr('Src/' + n, t)
        if packed:
            z.writestr('packed.json', '{"LoadConfiguration":{"LoadFromYaml":true}}')


WADL = ('<application xmlns:siena="http://schemas.microsoft.com/MicrosoftProjectSiena/WADL/2014/11" siena:serviceId="Old">'
        '<resources><resource><method id="Run"><request><representation mediaType="application/json">'
        '<param style="plain" name="payload" required="true"/></representation></request></method></resource></resources>'
        '<grammars><siena:object name="ResponseActionOutput"><siena:property name="result_json" type="string"/></siena:object></grammars></application>')


def test_canvasdoc():
    flow_guid = '11111111-2222-3333-4444-555555555555'
    sp_ref = {'id': 'k-sp', 'connectionInstanceId': '/providers/microsoft.powerapps/apis/shared_sharepointonline/connections/conn1',
              'dataSources': ['Tickets'], 'dependents': [], 'connectionRef': {'id': '/providers/microsoft.powerapps/apis/shared_sharepointonline', 'displayName': 'SharePoint', 'iconUri': 'https://example.com/sp.png'}}
    web_ref = {'id': 'k-web', 'connectionInstanceId': '/providers/microsoft.powerapps/apis/shared_webcontents/connections/c2',
               'dataSources': [], 'dependents': ['k-flow'], 'connectionRef': {'id': '/providers/microsoft.powerapps/apis/shared_webcontents'}}
    flow_ref = {'id': 'k-flow', 'connectionInstanceId': '/providers/microsoft.powerapps/apis/shared_logicflows/connections/' + flow_guid,
                'dataSources': ['SubmitTicket'], 'dependencies': {'shared_webcontents': 'k-web'},
                'connectionRef': {'id': '/providers/microsoft.powerapps/apis/shared_logicflows', 'iconUri': 'https://example.com/lf.png',
                                  'parameterHints': {'workflowName': {'value': flow_guid}, 'k-web': {'value': 'shared_webcontents'}},
                                  'parameterHintsV2': {'shared_webcontents': {'value': 'k-web'}}}}
    refs = {'k-sp': sp_ref, 'k-web': web_ref, 'k-flow': flow_ref}
    fmap = {flow_guid.replace('-', ''): ['shared_sharepointonline', 'shared_webcontents']}
    out, rep = canvasdoc.set_flow_dependencies(json.loads(json.dumps(refs)), fmap, {})
    fe = out['k-flow']
    check(fe['connectionInstanceId'].endswith('/connections/%s-%s' % (flow_guid.replace('-', ''), canvasdoc.DEFAULT_SUFFIX)),
          'canvasdoc: bare dashed flow GUID canonicalised to <de-dashed>-<16 hex>')
    check(fe['dependencies'] == {'shared_sharepointonline': 'k-sp'} and fe['connectionRef']['parameterHints']['k-sp'] == {'value': 'shared_sharepointonline'}
          and fe['connectionRef']['parameterHintsV2']['shared_sharepointonline'] == {'value': 'k-sp'} and 'k-flow' in out['k-sp']['dependents'],
          'canvasdoc: per-flow dependency wiring (dependencies + both hint maps + dependents)')
    check('k-web' not in out and any('shared_webcontents' in s for s in rep['premiumSkipped']), 'canvasdoc: premium connector unwired and its orphan entry removed')
    _o2, rep2 = canvasdoc.set_flow_dependencies(json.loads(json.dumps(refs)), {flow_guid.replace('-', ''): ['shared_office365']}, {'shared_office365': {'connectionName': 'x'}})
    check(rep2['leftForStudio'] == ['shared_office365'], 'canvasdoc: no standalone entry without displayName + iconUri (left for Studio)')
    ds = [{'Name': 'NewFlow', 'ServiceKind': 'ConnectedWadl', 'FlowNameId': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}]
    r3, added, _re, left = canvasdoc.add_flow_references({}, ds, '')
    check(left == ['NewFlow'] and not added, 'canvasdoc: flow entry not synthesised without a logic-flows icon')
    r4, added4, _re4, _l4 = canvasdoc.add_flow_references({}, ds, 'https://example.com/lf.png')
    check(added4 == ['NewFlow'] and list(r4.values())[0]['connectionRef']['parameterHints']['workflowName']['value'] == ds[0]['FlowNameId'],
          'canvasdoc: flow data source without an owner gets a Studio-shaped entry')
    mapping = canvasdoc.name_mapping({'Title': {'title': 'Title'}, 'Name': {'title': 'Title'}, '{Name}': {'title': 'Name'}, 'Status': {'title': 'Status'}})
    check(mapping == {'Title': 'Title', 'Name': 'Title (Name)', '{Name}': 'Name ({Name})', 'Status': 'Status'}, 'canvasdoc: Studio display-name collision rule')
    check(canvasdoc.cdp_time('2001-01-02T03:04:05.6789Z') == '2001-01-02T03:04:05.6780000Z', 'canvasdoc: CdpRevision time form')
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'a.msapp')
        _msapp(p, refs, [{'Name': 'SubmitTicket', 'ServiceKind': 'ConnectedWadl', 'ApiId': '/providers/microsoft.powerapps/apis/shared_logicflows',
                          'FlowNameId': flow_guid, 'WadlMetadata': {'WadlXml': WADL}},
                         {'Name': 'Tickets', 'Type': 'ConnectedDataSourceInfo', 'ApiId': '/providers/microsoft.powerapps/apis/shared_sharepointonline',
                          'DatasetName': 'https://contoso.sharepoint.com/sites/HelpDesk', 'TableName': 'L1'}],
                {'scrMain.pa.yaml': 'Screens:\n  scrMain:\n    Children:\n      - btnGo:\n          Control: Classic/Button@2.2.0\n          Properties:\n            OnSelect: =SubmitTicket.Run("{}")\n'},
                packed=True)
        doc = canvasdoc.Msapp(p)
        check(canvasdoc.app_actions(doc).get('SubmitTicket') == ['Run'], 'canvasdoc: flow actions the formulas call are detected')
        new_wadl = WADL.replace('siena:serviceId="Old"', 'siena:serviceId="Renamed"').replace(
            '<param style="plain" name="payload" required="true"/>', '<param style="plain" name="payload" required="true"/><param style="plain" name="fileJson"/>')
        lines = canvasdoc.refresh_flow_signatures(doc, {flow_guid: new_wadl})
        sig = canvasdoc.wadl_signature(doc.json('References/DataSources.json')['DataSources'][0]['WadlMetadata']['WadlXml'])
        check(sig['serviceId'] == 'SubmitTicket' and sig['inputs'] == ['payload*', 'fileJson'] and 'inputs' in lines[0],
              'canvasdoc: listWadl signature stored with serviceId pinned to the data-source name')
        try:
            canvasdoc.refresh_flow_signatures(doc, {flow_guid: {'unsupported': True, 'inputs': ['payload*', 'other'], 'outputs': ['result_json']}})
            refused = False
        except SystemExit:
            refused = True
        check(refused, 'canvasdoc: WADL-inexpressible flow with a stale embedded signature is refused')
        meta = json.dumps({'schema': {'items': {'properties': {'Title': {'title': 'Subject'}, 'Status': {'title': 'Status'}}}}})
        canvasdoc.refresh_table_schemas(doc, {'L1': meta}, '2001-01-02T03:04:05.000Z')
        tds = doc.json('References/DataSources.json')['DataSources'][1]
        check(tds['DataEntityMetadataJson'] == {'L1': meta} and tds['ConnectedDataSourceInfoNameMapping'] == {'Title': 'Subject', 'Status': 'Status'},
              'canvasdoc: table schema stored verbatim + name mapping')
        _r, runtime, rep5 = canvasdoc.runtime_references(doc, fmap, {}, 'https://example.com/lf.png')
        check(runtime['k-flow']['actions'] == ['Run'] and runtime['k-flow']['dependencies'] == ['k-sp'], 'canvasdoc: runtime references carry actions + dependency keys')
        doc.save(os.path.join(d, 'b.msapp'))
        with zipfile.ZipFile(os.path.join(d, 'b.msapp')) as z:
            check('References/DataSources.json' in z.namelist(), 'canvasdoc: document re-saved')


def test_powerapps():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'a.msapp')
        _msapp(p, {}, [])
        key = {'resourceKey': 'key-1', 'folderId': '12345'}
        z = PowerAppsClient.build_package(p, 'Contoso Help Desk', key, os.path.join(d, 'pkg.zip'))
        with zipfile.ZipFile(z) as zz:
            m = json.loads(zz.read('manifest.json'))
            names = zz.namelist()
            appj = json.loads(zz.read([n for n in names if n.endswith('.json') and n != 'manifest.json'
                                       and 'identity' not in n][0]))
        ver = (((appj.get('appDefinitionTemplate') or {}).get('properties') or {}).get('createdByClientVersion') or {})
        check(ver.get('major', 0) > 0 and ver.get('revision', -1) >= 0,
              'powerapps: the package template carries a four-part client version (a create stored 0.0.0.0 -> stuck, D-22)')
        check(list(m['resources']) == ['key-1'] and any(n.startswith('Microsoft.PowerApps/apps/12345/N') and n.endswith('-document.msapp') for n in names),
              'powerapps: package uses the minted key/folder pair and carries the document verbatim')
        try:
            PowerAppsClient.build_package(p, 'Help Desk / Ops', key, os.path.join(d, 'x.zip'))
            refused = False
        except SystemExit:
            refused = True
        check(refused, 'powerapps: a display name with / is refused (the service rejects it on every later PUT)')
        cfg = config.from_dict(dict(CFG, packageKeyCache=os.path.join(d, 'keys.json')))
        state = {'tags': {}, 'appType': 'ClassicCanvasApp', 'properties': {
            'displayName': 'Contoso Help Desk', 'environment': {'id': '/x/' + ENV, 'name': ENV},
            'createdByClientVersion': {'major': 3, 'minor': 1, 'build': 2, 'revision': -1},
            'unpublishedAppDefinition': {'properties': {'appUris': {'documentUri': {'value': 'https://example.com/doc.msapp'}},
                                                        'createdByClientVersion': {'major': 0, 'minor': 0, 'build': 0, 'revision': 0}}}}}
        routes = [('GET', '/apps/A1?', 200, state), ('POST', '/acquireLease', 200, {'leaseId': 'L'}),
                  ('PUT', '/apps/A1?', 200, {}), ('POST', '/releaseLease', 200, {})]
        h, t = http_with(routes)
        pa = PowerAppsClient(cfg, FakeAuth(), h, sleep=lambda s: None, log=lambda *a: None)
        pa.write_draft_references('A1', {'k': {'id': '/providers/microsoft.powerapps/apis/shared_sharepointonline'}})
        put = json.loads([c for c in t.calls if c[0] == 'PUT'][0][3])
        order = [c[1].split('?')[0].rsplit('/', 1)[-1] for c in t.calls if c[0] in ('POST', 'PUT')]
        check(order == ['acquireLease', 'A1', 'releaseLease'], 'powerapps: draft write = lease, full PUT, release')
        check(put['properties']['lifeCycleId'] == 'Draft' and put['properties']['createdByClientVersion'] == FALLBACK_VERSION
              and put['properties']['appUris']['documentUri']['value'] == 'https://example.com/doc.msapp',
              'powerapps: PUT targets the draft document with a FOUR-part client version (never a negative revision)')
        pa2 = PowerAppsClient(config.from_dict(dict(CFG, environmentId='Default-' + '0123456789abcdef' * 2)), FakeAuth(), h)
        check(pa2.environment_host() == 'https://default0123456789abcdef0123456789abcd.ef.environment.api.powerplatform.com',
              'powerapps: player launch host derived from the environment id (commercial split 2)')


def test_runtime_rules():
    js = ['var _0=["App","btnSave","lblOk"];__addC(_0[1],"12");__addC(_0[2],"13");'
          '_re0("12","OnSelect",function(_as){return _w0(null).then(x)});_re1("13.OnSelect",null,false);']
    got = runtime_rules.scan(js, {('lblOk', 'OnSelect'): '=false'})
    check(got == [('btnSave', 'OnSelect', 'async')], 'runtime_rules: async null rule found and named; constant handler ignored')
    got2 = runtime_rules.scan(js, {('lblOk', 'OnSelect'): '=Set(x, UTCNow())'})
    check(('lblOk', 'OnSelect', 'handler') in got2, 'runtime_rules: an empty-looking handler IS a null when the document has a formula')


def test_census():
    class SP(object):
        def find_list(self, t):
            return {'ItemCount': 3}

        def items(self, *a, **kw):
            return [{'Id': 1, 'Title': 'A', 'Key': 'k1'}, {'Id': 2, 'Title': '', 'Key': 'k1'}]

        def delete_item(self, *a):
            raise AssertionError('should never be reached')
    res = census.census(SP(), [{'list': 'L', 'required': ['Title'], 'key': 'Key'}])[0]
    check(res['completeRead'] is False and res['blank']['Title'] == 1 and res['duplicateKeys'] == 1, 'census: incomplete read, blanks and duplicates measured')
    try:
        census.ReadOnly(SP()).delete_item('L', 1)
        refused = False
    except PermissionError:
        refused = True
    check(refused, 'census: the read-only guard refuses writes')


def test_flow_source():
    with tempfile.TemporaryDirectory() as d:
        zp = os.path.join(d, 'p.zip')
        with zipfile.ZipFile(zp, 'w') as z:
            z.writestr('manifest.json', '{}')
            z.writestr('Microsoft.Flow/flows/abc/definition.json', json.dumps({'properties': {'displayName': 'X', 'definition': {'actions': {}}}}))
        check(read_flow_source(zp)['displayName'] == 'X', 'flows: reads the definition out of a legacy package zip')


def run():
    for t in (test_http, test_auth, test_sharepoint, test_flows, test_canvasdoc, test_powerapps, test_runtime_rules, test_census, test_flow_source):
        try:
            t()
        except Exception as ex:   # a crash is a failure, reported, not a traceback wall
            import traceback
            traceback.print_exc()
            check(False, '%s raised %r' % (t.__name__, ex))
    ok = all(OK)
    print('self-test: %s (%d checks)' % ('PASS' if ok else 'FAIL', len(OK)))
    return 0 if ok else 1
