#!/usr/bin/env python3
"""Build the Contoso Help Desk example into importable artifacts (example/dist/, git-ignored).

    python example/build.py                          # flows -> dist/*.zip  (+ the list-provisioning flow)
    python example/build.py --base <downloaded.msapp> # + dist/Contoso Help Desk.msapp (Src stamped into your base)
    python example/build.py --check                  # offline self-check with placeholder values (no config needed)

Tenant values come from config/environment.json (siteUrl, operatorEmail) and are substituted into the flow
definitions at BUILD time, so the committed sources stay tenant-free. Without a config the placeholders
https://contoso.sharepoint.com/sites/HelpDesk and a contoso.com address are used -- such a build imports but its
flows will not find your list.
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOLS = os.path.join(REPO, 'tools')
FLOWS = ['HelpDeskSubmitTicket', 'HelpDeskNotifyNewTicket']
APP_NAME = 'Contoso Help Desk'
LIST = 'HelpDeskTickets'


def _tool(fname, mod):
    spec = importlib.util.spec_from_file_location(mod, os.path.join(TOOLS, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def values(config_path):
    v = {'__SITE_URL__': 'https://contoso.sharepoint.com/sites/HelpDesk', '__OPERATOR_EMAIL__': 'helpdesk-owner@contoso.com'}
    if config_path and os.path.isfile(config_path):
        cfg = json.load(open(config_path, encoding='utf-8-sig'))
        for key, token in (('siteUrl', '__SITE_URL__'), ('operatorEmail', '__OPERATOR_EMAIL__')):
            if cfg.get(key) and '<' not in cfg[key]:
                v[token] = cfg[key]
    else:
        print('NOTE: no config/environment.json -- building with contoso placeholders (fine for a dry look, not for a real import)')
    return v


def http_request(name, run_after, method, uri, headers=None, body=None):
    params = {'dataset': '__SITE_URL__', 'parameters/method': method, 'parameters/uri': uri,
              'parameters/headers': headers or {'Accept': 'application/json;odata=nometadata'}}
    if body is not None:
        params['parameters/body'] = body
    return {name: {'type': 'OpenApiConnection', 'runAfter': run_after, 'inputs': {
        'host': {'apiId': '/providers/Microsoft.PowerApps/apis/shared_sharepointonline', 'connectionName': 'shared_sharepointonline',
                 'operationId': 'HttpRequest'},
        'parameters': params,
        'retryPolicy': {'type': 'exponential', 'count': 3, 'interval': 'PT10S', 'minimumInterval': 'PT5S', 'maximumInterval': 'PT1M'}}}}


def provisioning_flow(schema):
    """The list schema -> an idempotent, audit-by-default provisioning flow (Button trigger, input apply=true/false).
    Shape mirrors a provisioning flow that ran on a real tenant: GET fields, Filter array per definition, create only
    what is missing, report. Choice lists of EXISTING fields are not extended here (the dev tool does that)."""
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    from devtenant.sharepoint import field_xml
    spec = schema['lists'][0]
    defs = [{'name': f['name'], 'type': f['type'], 'xml': field_xml(f)} for f in spec['fields']]
    verbose = {'Accept': 'application/json;odata=verbose', 'Content-Type': 'application/json;odata=verbose'}
    lst = "_api/web/lists/GetByTitle('%s')" % spec['title']
    actions = {
        'Initialize_Missing': {'type': 'InitializeVariable', 'runAfter': {}, 'inputs': {'variables': [{'name': 'Missing', 'type': 'array', 'value': []}]}},
    }
    actions.update(http_request('Get_List', {'Initialize_Missing': ['Succeeded']}, 'GET',
                                "_api/web/lists?$select=Id,Title&$filter=Title eq '%s'" % spec['title']))
    create_list = http_request('Create_List', {}, 'POST', '_api/web/lists', verbose,
                               {'__metadata': {'type': 'SP.List'}, 'BaseTemplate': 100, 'Title': spec['title'],
                                'Description': spec.get('description', '')})
    actions['If_List_Missing'] = {'type': 'If', 'runAfter': {'Get_List': ['Succeeded']},
                                  'expression': {'equals': ["@length(coalesce(body('Get_List')?['value'], json('[]')))", 0]},
                                  'actions': {'Append_List_Missing': {'type': 'AppendToArrayVariable', 'runAfter': {},
                                                                      'inputs': {'name': 'Missing', 'value': 'list ' + spec['title']}},
                                              'If_Apply_List': {'type': 'If', 'runAfter': {'Append_List_Missing': ['Succeeded']},
                                                                'expression': {'equals': ["@triggerBody()?['apply']", True]},
                                                                'actions': create_list, 'else': {'actions': {}}}},
                                  'else': {'actions': {}}}
    scope = {}
    scope.update(http_request('Get_Fields', {}, 'GET', lst + '/fields?$select=InternalName,TypeAsString'))
    scope['Field_Definitions'] = {'type': 'Compose', 'runAfter': {'Get_Fields': ['Succeeded']}, 'inputs': defs}
    create_field = http_request('Create_Field', {}, 'POST', lst + '/fields/CreateFieldAsXml', verbose,
                                {'parameters': {'__metadata': {'type': 'SP.XmlSchemaFieldCreationInformation'},
                                                'SchemaXml': "@items('Audit_Fields')?['xml']", 'Options': 24}})
    scope['Audit_Fields'] = {'type': 'Foreach', 'runAfter': {'Field_Definitions': ['Succeeded']}, 'foreach': "@outputs('Field_Definitions')",
                             'runtimeConfiguration': {'concurrency': {'repetitions': 1}},
                             'actions': {
                                 'Filter_Existing': {'type': 'Query', 'runAfter': {}, 'inputs': {
                                     'from': "@coalesce(body('Get_Fields')?['value'], json('[]'))",
                                     'where': "@equals(item()?['InternalName'], items('Audit_Fields')?['name'])"}},
                                 'If_Field_Missing': {'type': 'If', 'runAfter': {'Filter_Existing': ['Succeeded']},
                                                      'expression': {'equals': ["@length(body('Filter_Existing'))", 0]},
                                                      'actions': {'Append_Field_Missing': {'type': 'AppendToArrayVariable', 'runAfter': {},
                                                                                           'inputs': {'name': 'Missing', 'value': "@concat('field ', items('Audit_Fields')?['name'])"}},
                                                                  'If_Apply_Field': {'type': 'If', 'runAfter': {'Append_Field_Missing': ['Succeeded']},
                                                                                     'expression': {'equals': ["@triggerBody()?['apply']", True]},
                                                                                     'actions': create_field, 'else': {'actions': {}}}},
                                                      'else': {'actions': {}}}}}
    if spec.get('titleDisplayName'):
        rename = http_request('Rename_Title', {}, 'POST', lst + "/fields/GetByInternalNameOrTitle('Title')",
                              dict(verbose, **{'X-HTTP-Method': 'MERGE', 'IF-MATCH': '*'}),
                              {'__metadata': {'type': 'SP.Field'}, 'Title': spec['titleDisplayName']})
        scope['If_Apply_Title'] = {'type': 'If', 'runAfter': {'Audit_Fields': ['Succeeded']},
                                   'expression': {'equals': ["@triggerBody()?['apply']", True]}, 'actions': rename, 'else': {'actions': {}}}
    actions['Scope_Fields'] = {'type': 'Scope', 'runAfter': {'If_List_Missing': ['Succeeded']}, 'actions': scope}
    actions['Audit_Result'] = {'type': 'Compose', 'runAfter': {'Scope_Fields': ['Succeeded', 'Failed']}, 'inputs': {
        'mode': "@if(equals(triggerBody()?['apply'], true), 'APPLY', 'AUDIT ONLY')",
        'missing_at_start': "@variables('Missing')",
        'fields_step': "@result('Scope_Fields')",
        'message': "@if(equals(triggerBody()?['apply'], true), 'Missing items were created. Run again with apply = false to verify nothing is missing.', 'Nothing was changed. Run again with apply = true to create what is missing.')"}}
    return {'properties': {
        'displayName': 'HelpDeskProvisionList',
        'connectionReferences': {'shared_sharepointonline': {
            'connectionName': 'shared-sharepointonline-HelpDeskProvisionList', 'source': 'Embedded',
            'id': '/providers/Microsoft.PowerApps/apis/shared_sharepointonline', 'tier': 'NotSpecified', 'apiName': 'sharepointonline',
            'isProcessSimpleApiReferenceConversionAlreadyDone': False}},
        'definition': {
            '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#',
            'contentVersion': '1.0.0.0',
            'parameters': {'$authentication': {'defaultValue': {}, 'type': 'SecureObject'}, '$connections': {'defaultValue': {}, 'type': 'Object'}},
            'triggers': {'manual': {'type': 'Request', 'kind': 'Button', 'inputs': {'schema': {'type': 'object', 'required': ['apply'], 'properties': {
                'apply': {'title': 'Apply (create what is missing)', 'type': 'boolean', 'default': False, 'x-ms-content-hint': 'BOOLEAN',
                          'description': 'False audits only. True creates the list and any missing columns.'}}}}}},
            'actions': actions, 'outputs': {}}}}


def substitute(obj, vals):
    text = json.dumps(obj)
    for k, v in vals.items():
        text = text.replace(k, json.dumps(v)[1:-1])
    return json.loads(text)


def build(dist, config_path, base=None, quiet=False):
    vals = values(config_path)
    bfp = _tool('build-flow-package.py', 'build_flow_package')
    stage = os.path.join(dist, 'src')
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    outputs = []
    schema = json.load(open(os.path.join(HERE, 'sharepoint', 'helpdesk.schema.json'), encoding='utf-8'))
    sources = {name: (json.load(open(os.path.join(HERE, 'flows', name, 'definition.json'), encoding='utf-8')),
                      json.load(open(os.path.join(HERE, 'flows', name, 'flow.json'), encoding='utf-8'))) for name in FLOWS}
    sources['HelpDeskProvisionList'] = (provisioning_flow(schema), {
        'displayName': 'HelpDeskProvisionList',
        'description': 'Creates the HelpDeskTickets list and its columns if missing. Run with apply = false first (audit), then true.',
        'connectors': {'shared_sharepointonline': {'displayName': 'SharePoint'}}})
    for name, (definition, meta) in sources.items():
        folder = os.path.join(stage, name)
        os.makedirs(folder)
        json.dump(substitute(definition, vals), open(os.path.join(folder, 'definition.json'), 'w', encoding='utf-8'), indent=1)
        json.dump(meta, open(os.path.join(folder, 'flow.json'), 'w', encoding='utf-8'), indent=1)
        outputs.append(bfp.build(folder, os.path.join(dist, name + '.zip')))
    cl = _tool('canvas-lint.py', 'canvas_lint')
    files, issues = cl.lint([os.path.join(HERE, 'canvas', 'Src')])
    if cl.report(files, issues, True):
        raise SystemExit('canvas source has lint findings; fix them before packing')
    if base:
        mt = _tool('msapp-tool.py', 'msapp_tool')
        out = os.path.join(dist, APP_NAME + '.msapp')
        mt.cmd_stamp(base, os.path.join(HERE, 'canvas', 'Src'), out)
        outputs.append(out)
    else:
        print('NOTE: no --base: the app is not packed. Create the app shell in Studio, Download a copy, then rerun with --base.')
    return outputs


def check():
    ok = True
    with tempfile.TemporaryDirectory() as d:
        outs = build(d, None)
        ok = ok and len(outs) == 3
        with zipfile.ZipFile(os.path.join(d, 'HelpDeskSubmitTicket.zip')) as z:
            dfn = [n for n in z.namelist() if n.endswith('/definition.json')][0]
            text = z.read(dfn).decode('utf-8')
        ok = ok and 'contoso.sharepoint.com/sites/HelpDesk' in text and '__SITE_URL__' not in text
        base = os.path.join(d, 'base.msapp')
        with zipfile.ZipFile(base, 'w') as z:
            z.writestr('Header.json', '{}')
            z.writestr('Properties.json', '{"LocalConnectionReferences":"{}"}')
            z.writestr('Src/App.pa.yaml', 'App:\n  Properties:\n    OnStart: =true\n')
            z.writestr('Src/scrHelpDesk.pa.yaml', 'Screens:\n  scrHelpDesk:\n    Properties:\n      Fill: =Color.White\n')
        outs = build(d, None, base)
        with zipfile.ZipFile(outs[-1]) as z:
            ok = ok and b'HelpDeskSubmitTicket.Run' in z.read('Src/scrHelpDesk.pa.yaml') and 'packed.json' in z.namelist()
    print(('  ok   ' if ok else '  FAIL ') + 'three flow packages built and flowchecked, tokens substituted, app stamped into a base with packed.json')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base', help='a Studio "Download a copy" of the app shell (see README step 3)')
    ap.add_argument('--config', default=os.path.join(REPO, 'config', 'environment.json'))
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    if a.check:
        return check()
    outs = build(os.path.join(HERE, 'dist'), a.config, a.base)
    print('\nBuilt:\n  ' + '\n  '.join(os.path.relpath(o, REPO) for o in outs))
    return 0


if __name__ == '__main__':
    sys.exit(main())
