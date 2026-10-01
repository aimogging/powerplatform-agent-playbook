#!/usr/bin/env python3
"""build-flow-package.py -- turn a flow source folder into a legacy import package (.zip) for MANUAL import
(Power Automate > My flows > Import > Import Package (Legacy)).

Flow source folder (this repo's convention):
    <FlowName>/definition.json   {"properties": {"displayName", "definition", "connectionReferences"}}  (what flowcheck reads)
    <FlowName>/flow.json         {"displayName", "description", "connectors": {"shared_x": {"displayName", "iconUri"}}}  (optional)

Package layout (VERIFIED against a real tenant export after a MissingPackageManifest error):
    manifest.json                                  resources graph: the flow + one api + one connection resource per connector
    Microsoft.Flow/flows/manifest.json             {"packageSchemaVersion":"1.0","flowAssets":{"assetPaths":[<asset GUID>]}}
    Microsoft.Flow/flows/<asset GUID>/definition.json   {name, id, type, properties{apiId, displayName, definition,
                                                        connectionReferences, flowFailureAlertSubscribed, isManaged}}
    Microsoft.Flow/flows/<asset GUID>/apisMap.json        {"shared_x": "<api resource key>"}
    Microsoft.Flow/flows/<asset GUID>/connectionsMap.json {"shared_x": "<connection resource key>"}
The asset folder GUID is independent of the definition's own name/id. The human-only definition_pretty.json is NOT
packaged. GUIDs here are derived deterministically from the display name (uuid5), so rebuilds are stable.

Import behaviour (proven by hand imports):
  * connections are RE-PICKED in the import dialog (one slot per connection resource) -- nothing tenant-specific ships;
  * a revision of an existing flow: choose UPDATE on the existing flow, never "Create as new" (two copies of a
    list-triggered flow both fire); a brand-new flow: Create as new;
  * the importer refuses an OpenApiConnection action carrying inputs.authentication, and a connector used by an
    action but missing from connectionReferences ("Property 'host.connectionReferenceName' is missing") --
    flowcheck.py fails both, and this builder runs flowcheck first.
  * Resource shapes for the api/connection pairs were modelled on a real export (and a synthesized pair modelled on
    that shape imported fine). iconUri values: pass --template <an exported package from YOUR tenant> to copy the
    exact api/connection resources from it; without a template the resources carry no iconUri [UNVERIFIED whether the
    importer requires one -- if the dialog misbehaves, export any flow using that connector and pass it as --template].

    python tools/build-flow-package.py <flow-folder> [--out dist/X.zip] [--template exported.zip] [--skip-check]
                                       [--handoff config/environment.json]   # delivery build: refuse dev-tenant leaks
    python tools/build-flow-package.py --self-test
"""
import argparse
import copy
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
NS = uuid.NAMESPACE_URL


def did(*parts):
    return str(uuid.uuid5(NS, 'pp-playbook/' + '/'.join(parts)))


def template_resources(template_zip):
    """api name -> (api resource, connection resource) copied from a real export of the target tenant."""
    out = {}
    if not template_zip:
        return out
    with zipfile.ZipFile(template_zip) as z:
        res = json.loads(z.read('manifest.json').decode('utf-8-sig'))['resources']
    for k, r in res.items():
        if r.get('type') == 'Microsoft.PowerApps/apis':
            conn = next((c for c in res.values() if c.get('type') == 'Microsoft.PowerApps/apis/connections' and k in (c.get('dependsOn') or [])), None)
            if conn:
                out[r.get('name')] = (copy.deepcopy(r), copy.deepcopy(conn))
    return out


def build(folder, out=None, template=None, skip_check=False, handoff=None):
    with open(os.path.join(folder, 'definition.json'), encoding='utf-8-sig') as fh:
        src = json.load(fh)
    meta = {}
    if os.path.isfile(os.path.join(folder, 'flow.json')):
        with open(os.path.join(folder, 'flow.json'), encoding='utf-8-sig') as fh:
            meta = json.load(fh)
    props = src['properties']
    name = meta.get('displayName') or props['displayName']
    if name != props['displayName']:
        raise SystemExit('flow.json displayName %r != definition.json properties.displayName %r -- the importer and any '
                         'API deploy read the DEFINITION one; keep them equal (package file name = flow name)' % (name, props['displayName']))
    if not skip_check:
        spec = importlib.util.spec_from_file_location('flowcheck', os.path.join(HERE, 'flowcheck.py'))
        fcm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fcm)
        c = fcm.Checker(json.dumps(src), os.path.join(folder, 'definition.json')).run()
        if c.report():
            raise SystemExit('refusing to package: flowcheck errors above')
    tmpl = template_resources(template)
    asset = did(name, 'asset')
    flow_key = did(name, 'flow-resource')
    resources = {flow_key: {'type': 'Microsoft.Flow/flows', 'suggestedCreationType': 'New', 'creationType': 'Existing, New, Update',
                            'details': {'displayName': name}, 'configurableBy': 'User', 'hierarchy': 'Root', 'dependsOn': []}}
    apis_map, conns_map = {}, {}
    for api in sorted((props.get('connectionReferences') or {}).keys()):
        api_key, conn_key = did(name, api, 'api'), did(name, api, 'connection')
        info = (meta.get('connectors') or {}).get(api) or {}
        if api in tmpl:
            api_res, conn_res = tmpl[api]
            api_res['dependsOn'] = []
            conn_res['dependsOn'] = [api_key]
        else:
            details = {'displayName': info.get('displayName') or api.replace('shared_', '')}
            if info.get('iconUri'):
                details['iconUri'] = info['iconUri']
            api_res = {'id': '/providers/Microsoft.PowerApps/apis/' + api, 'name': api, 'type': 'Microsoft.PowerApps/apis',
                       'suggestedCreationType': 'Existing', 'details': details, 'configurableBy': 'System', 'hierarchy': 'Child', 'dependsOn': []}
            conn_res = {'type': 'Microsoft.PowerApps/apis/connections', 'suggestedCreationType': 'Existing', 'creationType': 'Existing',
                        'details': dict(details, displayName='%s connection' % details['displayName']), 'configurableBy': 'User',
                        'hierarchy': 'Child', 'dependsOn': [api_key]}
        resources[api_key], resources[conn_key] = api_res, conn_res
        resources[flow_key]['dependsOn'] += [api_key, conn_key]
        apis_map[api], conns_map[api] = api_key, conn_key
    flow_guid = did(name, 'definition')
    definition = {'name': flow_guid, 'id': '/providers/Microsoft.Flow/flows/' + flow_guid, 'type': 'Microsoft.Flow/flows',
                  'properties': {'apiId': '/providers/Microsoft.PowerApps/apis/shared_logicflows', 'displayName': name,
                                 'definition': props['definition'], 'connectionReferences': props.get('connectionReferences') or {},
                                 'flowFailureAlertSubscribed': False, 'isManaged': False}}
    manifest = {'schema': '1.0', 'details': {'displayName': name, 'description': meta.get('description', ''),
                                             'createdTime': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.0000000Z'),
                                             'packageTelemetryId': did(name, 'telemetry'), 'creator': 'N/A', 'sourceEnvironment': ''},
                'resources': resources}
    out = out or os.path.join(os.path.dirname(os.path.abspath(folder)), '..', 'dist', name + '.zip')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    base = 'Microsoft.Flow/flows/%s/' % asset
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json', json.dumps(manifest, indent=2))
        z.writestr('Microsoft.Flow/flows/manifest.json', json.dumps({'packageSchemaVersion': '1.0', 'flowAssets': {'assetPaths': [asset]}}, indent=2))
        z.writestr(base + 'definition.json', json.dumps(definition, separators=(',', ':')))
        z.writestr(base + 'apisMap.json', json.dumps(apis_map))
        z.writestr(base + 'connectionsMap.json', json.dumps(conns_map))
    if handoff:
        spec = importlib.util.spec_from_file_location('leakcheck', os.path.join(HERE, 'leakcheck.py'))
        lk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lk)
        if lk.check(out, handoff):
            os.remove(out)
            raise SystemExit('refusing the HANDOFF package: it carries dev-tenant values or test scaffolding (see LEAK lines)')
    print('RESULT: %s -> %s (%d connector(s): %s)' % (name, os.path.normpath(out), len(apis_map), ', '.join(apis_map) or 'none'))
    print('IMPORT: My flows > Import > Import Package (Legacy) > upload > set each connection slot > Update (existing) or Create as new (first time) > Import')
    return out


def self_test():
    ok = True

    def check(cond, what):
        nonlocal ok
        print(('  ok   ' if cond else '  FAIL ') + what)
        ok = ok and cond

    spec = importlib.util.spec_from_file_location('flowcheck', os.path.join(HERE, 'flowcheck.py'))
    fcm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fcm)
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, 'SelfTestFlow')
        os.makedirs(f)
        json.dump(fcm._clean_fixture(), open(os.path.join(f, 'definition.json'), 'w'))
        out = build(f, os.path.join(d, 'dist', 'SelfTestFlow.zip'))
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            m = json.loads(z.read('manifest.json'))
            fm = json.loads(z.read('Microsoft.Flow/flows/manifest.json'))
            asset = fm['flowAssets']['assetPaths'][0]
            dfn = json.loads(z.read('Microsoft.Flow/flows/%s/definition.json' % asset))
            amap = json.loads(z.read('Microsoft.Flow/flows/%s/apisMap.json' % asset))
        check('Microsoft.Flow/flows/manifest.json' in names, 'package has Microsoft.Flow/flows/manifest.json (MissingPackageManifest otherwise)')
        types = sorted(r['type'] for r in m['resources'].values())
        check(types == ['Microsoft.Flow/flows', 'Microsoft.PowerApps/apis', 'Microsoft.PowerApps/apis/connections'], 'resources: flow + api + connection')
        check(dfn['properties']['displayName'] == 'SelfTestFlow' and dfn['properties']['apiId'].endswith('shared_logicflows'), 'definition wrapped with apiId + displayName')
        check(amap == {'shared_sharepointonline': [k for k, r in m['resources'].items() if r['type'] == 'Microsoft.PowerApps/apis'][0]}, 'apisMap points at the api resource')
        check(not any(n.endswith('definition_pretty.json') for n in names), 'human-only definition_pretty.json is not packaged')
        again = build(f, os.path.join(d, 'dist', 'again.zip'))
        with zipfile.ZipFile(again) as z:
            check(json.loads(z.read('Microsoft.Flow/flows/manifest.json'))['flowAssets']['assetPaths'][0] == asset, 'asset GUID is deterministic across rebuilds')
        bad = fcm._clean_fixture()
        bad['properties']['definition']['actions']['Get_Items']['inputs']['authentication'] = "@parameters('$authentication')"
        json.dump(bad, open(os.path.join(f, 'definition.json'), 'w'))
        try:
            build(f, os.path.join(d, 'dist', 'bad.zip'))
            refused = False
        except SystemExit:
            refused = True
        check(refused, 'a definition failing flowcheck is not packaged')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    if '--self-test' in sys.argv[1:]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder')
    ap.add_argument('--out')
    ap.add_argument('--template')
    ap.add_argument('--skip-check', action='store_true')
    ap.add_argument('--handoff', metavar='DEV_CONFIG', help='this is the delivery build: refuse any dev-tenant value from that config')
    a = ap.parse_args()
    build(a.folder, a.out, a.template, a.skip_check, a.handoff)
    return 0


if __name__ == '__main__':
    sys.exit(main())
