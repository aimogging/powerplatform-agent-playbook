#!/usr/bin/env python3
"""flowcheck.py -- offline structural checks for a Power Automate cloud-flow definition (WDL).

Cloud flows have NO local compiler. Every rule below is a failure that a real import, save or run
hit at least once (see reference/platform-traps.md for the incident behind each). The checker runs
in a second and catches them before a slow manual import round-trip.

    python tools/flowcheck.py <definition.json | flow-source-dir> [...] [--strict]
    python tools/flowcheck.py --self-test

Accepted inputs: a legacy-package definition.json ({name,id,type,properties:{definition,...}}),
an exported workflow ({"definition": {...}}), a bare definition ({"triggers":..,"actions":..}),
or a folder containing definition.json (and optionally flow.json, see tools/build-flow-package.py).

ERRORS (exit 1):
  json-dup-key         repeated key inside one JSON object
  dup-action           two actions with the same name anywhere in the tree
  result-non-scope     result('X') where X is not a Scope
  runafter-sibling     runAfter names an action that is not a sibling in the same scope
  ref-unknown          actions()/outputs()/body()/result() names an action that does not exist
  ref-not-upstream     an expression references an action that is not on its runAfter path
                       (the save fails: "InvalidTemplate ... must either be in 'runAfter' path")
  var-uninitialised    variables('X') / SetVariable X without an InitializeVariable X
  var-self-reference   SetVariable X whose value reads variables('X') (rejected at RUN time)
  nesting              deeper than 8 nested scopes (platform limit)
  wdl-unknown-fn       a function that is not in the WDL function reference (invented / wrong case)
  wdl-zero-arg         createArray() etc. with no arguments (InvalidTemplate at run time; use json('[]'))
  wdl-filter-fn        filter(...) used as an expression function: it is an ACTION (Filter array)
  expr-too-long        a single expression over 8192 characters (split it into Compose actions)
  odata-v4             contains()/in in a SharePoint $filter (SharePoint list filters are OData v3)
  pa-trigger-flags     PowerApp trigger input without x-ms-dynamically-added:true AND a description
  pa-trigger-union     PowerApp trigger input typed ["string","null"] (Studio cannot add/refresh the flow)
  pa-response-flag     Respond-to-PowerApp output without x-ms-dynamically-added:true (import drops it)
  legacy-auth          OpenApiConnection inputs carry "authentication" (legacy package import refuses it)
  conn-ref-missing     an action's connector has no properties.connectionReferences entry
  terminate-in-loop    Terminate inside a Foreach/Until (not allowed)

WARNINGS (exit 0 unless --strict):
  retry-default        connector action with no retryPolicy (a 5xx under the default policy can hold a
                       run in Running for a long time)
  coalesce-blank       coalesce(triggerBody()?[...]) on a PowerApp trigger input ("" is not null)
  digest-header        X-RequestDigest on a connector HttpRequest (the connector adds it)
  entity-type-literal  hardcoded SP.Data.*ListItem (resolve ListItemEntityTypeFullName at run time)
  var-unused           a variable initialised but never read or written
  list-trigger-cond    list-changed trigger with no trigger condition (own writes re-trigger it)
  respond-on-failure   PowerApp flow whose only Respond cannot run when an upstream action fails
"""
import argparse
import copy
import io
import json
import os
import re
import sys

MAX_NESTING = 8
MAX_EXPR = 8192

# Workflow Definition Language functions (MS Learn "Reference guide to workflow expression functions").
WDL_FUNCTIONS = {
    # string
    'concat', 'endsWith', 'formatNumber', 'guid', 'indexOf', 'isFloat', 'isInt', 'lastIndexOf', 'length',
    'nthIndexOf', 'replace', 'slice', 'split', 'startsWith', 'substring', 'toLower', 'toUpper', 'trim',
    # collection
    'chunk', 'contains', 'empty', 'first', 'intersection', 'join', 'last', 'reverse', 'skip', 'sort', 'take',
    'union',
    # logical
    'and', 'equals', 'greater', 'greaterOrEquals', 'if', 'less', 'lessOrEquals', 'not', 'or',
    # conversion
    'array', 'base64', 'base64ToBinary', 'base64ToString', 'binary', 'bool', 'createArray', 'dataUri',
    'dataUriToBinary', 'dataUriToString', 'decimal', 'decodeBase64', 'decodeDataUri', 'decodeUriComponent',
    'encodeUriComponent', 'float', 'int', 'json', 'string', 'uriComponent', 'uriComponentToBinary',
    'uriComponentToString', 'xml',
    # math
    'add', 'div', 'max', 'min', 'mod', 'mul', 'rand', 'range', 'sub',
    # date and time
    'addDays', 'addHours', 'addMinutes', 'addSeconds', 'addToTime', 'convertFromUtc', 'convertTimeZone',
    'convertToUtc', 'dateDifference', 'dayOfMonth', 'dayOfWeek', 'dayOfYear', 'formatDateTime', 'getFutureTime',
    'getPastTime', 'parseDateTime', 'startOfDay', 'startOfHour', 'startOfMonth', 'subtractFromTime', 'ticks',
    'utcNow',
    # workflow
    'action', 'actions', 'body', 'formDataMultiValues', 'formDataValue', 'item', 'items', 'iterationIndexes',
    'listCallbackUrl', 'multipartBody', 'outputs', 'parameters', 'result', 'trigger', 'triggerBody',
    'triggerFormDataMultiValues', 'triggerFormDataValue', 'triggerMultipartBody', 'triggerOutputs',
    'variables', 'workflow',
    # uri parsing
    'uriHost', 'uriPath', 'uriPathAndQuery', 'uriPort', 'uriQuery', 'uriScheme',
    # manipulation
    'addProperty', 'coalesce', 'removeProperty', 'setProperty', 'xpath',
}
ZERO_ARG_OK = {'item', 'utcNow', 'workflow', 'trigger', 'triggerBody', 'triggerOutputs', 'guid', 'rand',
               'action', 'body', 'outputs', 'iterationIndexes'}
REQUIRES_ARGS = {'createArray', 'concat', 'json', 'string', 'array', 'union', 'coalesce', 'if', 'equals',
                 'and', 'or', 'not', 'contains', 'empty', 'first', 'last', 'length', 'join', 'split'}
WRITE_TYPES = {'SetVariable', 'AppendToArrayVariable', 'AppendToStringVariable', 'IncrementVariable',
               'DecrementVariable'}
CONNECTOR_TYPES = {'OpenApiConnection', 'OpenApiConnectionWebhook', 'OpenApiConnectionNotification'}
LIST_TRIGGERS = {'GetOnUpdatedItems', 'GetOnNewItems', 'GetOnUpdatedFileItems', 'GetOnNewFileItems'}


class Checker(object):
    def __init__(self, raw, label):
        self.label = label
        self.errors = []
        self.warnings = []
        self.notes = []
        self.raw = raw
        dup = []

        def hook(pairs):
            seen = set()
            for k, _v in pairs:
                if k in seen:
                    dup.append(k)
                seen.add(k)
            return dict(pairs)

        doc = json.loads(raw, object_pairs_hook=hook)
        if dup:
            self.err('json-dup-key', 'repeated JSON key(s) inside one object: %s' % sorted(set(dup)))
        self.doc = doc
        if isinstance(doc.get('properties'), dict) and 'definition' in doc['properties']:
            self.definition = doc['properties']['definition']
            self.conn_refs = doc['properties'].get('connectionReferences') or {}
            self.has_conn_block = True
        elif 'definition' in doc:
            self.definition = doc['definition']
            self.conn_refs = doc.get('connectionReferences') or {}
            self.has_conn_block = 'connectionReferences' in doc
        else:
            self.definition = doc
            self.conn_refs = {}
            self.has_conn_block = False
        self.defraw = json.dumps(self.definition)
        self.root = self.definition.get('actions') or {}
        self.triggers = self.definition.get('triggers') or {}
        self.everything = list(self.walk(self.root, ()))
        self.by_name = {}
        dupes = []
        for name, act, path_, _s in self.everything:
            if name in self.by_name:
                dupes.append(name)
            self.by_name[name] = (act, path_)
        if dupes:
            self.err('dup-action', 'duplicate action names: %s' % sorted(set(dupes)))

    # ----------------------------------------------------------------------------- helpers
    def err(self, rule, msg):
        self.errors.append((rule, msg))

    def warn(self, rule, msg):
        self.warnings.append((rule, msg))

    def walk(self, actions, path):
        for name, act in (actions or {}).items():
            if not isinstance(act, dict):
                continue
            here = path + (name,)
            yield name, act, here, actions
            if isinstance(act.get('actions'), dict):
                for t in self.walk(act['actions'], here + ('actions',)):
                    yield t
            for key in ('else', 'default'):
                br = act.get(key)
                if isinstance(br, dict) and isinstance(br.get('actions'), dict):
                    for t in self.walk(br['actions'], here + (key,)):
                        yield t
            for cname, case in (act.get('cases') or {}).items():
                if isinstance(case, dict) and isinstance(case.get('actions'), dict):
                    for t in self.walk(case['actions'], here + ('case:' + cname,)):
                        yield t

    @staticmethod
    def chain(path):
        return [x for x in path if x not in ('actions', 'else', 'default') and not x.startswith('case:')]

    def expressions(self, obj):
        """Yield every expression string (starts with '@' but not '@@', or contains '@{')."""
        if isinstance(obj, str):
            if (obj.startswith('@') and not obj.startswith('@@')) or '@{' in obj:
                yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                for e in self.expressions(v):
                    yield e
        elif isinstance(obj, list):
            for v in obj:
                for e in self.expressions(v):
                    yield e

    # ------------------------------------------------------------------------------ rules
    def structural(self):
        scopes = {n for n, (a, _p) in self.by_name.items() if a.get('type') == 'Scope'}
        bad = sorted({m.group(1) for m in re.finditer(r"result\('([^']+)'\)", self.defraw) if m.group(1) not in scopes})
        if bad:
            self.err('result-non-scope', 'result() on non-Scope action(s): %s' % bad)
        for name, act, path, siblings in self.everything:
            for dep in (act.get('runAfter') or {}):
                if dep not in siblings:
                    self.err('runafter-sibling', "%s runAfter '%s', which is not a sibling in its scope" % (name, dep))
        refs = set(re.findall(r"(?:actions|outputs|body|result)\('([^']+)'\)", self.defraw))
        missing = sorted(r for r in refs if r not in self.by_name and r not in self.triggers)
        if missing:
            self.err('ref-unknown', 'expression references unknown action(s): %s' % missing)
        deep = [(n, p) for n, _a, p, _s in self.everything if len(self.chain(p)) - 1 > MAX_NESTING]
        for n, p in deep[:5]:
            self.err('nesting', '%s nests %d scopes deep, over the platform limit of %d' % (n, len(self.chain(p)) - 1, MAX_NESTING))
        self.notes.append('%d actions, structure checked' % len(self.by_name))

    def variables(self):
        inited = set()
        for _n, act, _p, _s in self.everything:
            if act.get('type') == 'InitializeVariable':
                for v in (act.get('inputs') or {}).get('variables', []):
                    inited.add(v.get('name'))
        read = set(re.findall(r"variables\('([^']+)'\)", self.defraw))
        written = {}
        for n, act, _p, _s in self.everything:
            if act.get('type') in WRITE_TYPES:
                written[(act.get('inputs') or {}).get('name')] = n
                if act.get('type') == 'SetVariable':
                    vname = (act.get('inputs') or {}).get('name')
                    val = json.dumps((act.get('inputs') or {}).get('value'))
                    if ("variables('%s')" % vname) in val:
                        self.err('var-self-reference', "%s: SetVariable '%s' reads its own variable -- rejected at run "
                                 "time ('Self reference is not supported'); compute into a Compose first" % (n, vname))
        loose = sorted((read | set(written)) - inited)
        if loose:
            self.err('var-uninitialised', 'variable(s) used without an InitializeVariable: %s' % loose)
        unused = sorted(inited - (read | set(written)))
        if unused:
            self.warn('var-unused', 'variable(s) initialised but never used: %s' % unused)

    def runafter_reachability(self):
        ref_re = re.compile(r"(?:outputs|body|actions|result)\('([^']+)'\)")
        parents = {name: (sib, self.chain(p)[:-1]) for name, _a, p, sib in self.everything}

        def nested(act):
            return {n for n, _a, _p, _s in self.walk({'_': act}, ()) if n != '_'}

        def closure(name, siblings):
            seen, todo = set(), list(((siblings.get(name) or {}).get('runAfter') or {}).keys())
            while todo:
                n = todo.pop()
                if n in seen or n not in siblings:
                    continue
                seen.add(n)
                todo.extend(((siblings[n].get('runAfter')) or {}).keys())
            return seen

        for name, act, path, _sib in self.everything:
            blob = json.dumps({k: act.get(k) for k in ('inputs', 'foreach', 'expression')})
            refs = {r for r in ref_re.findall(blob) if r in self.by_name and r != name}
            if not refs:
                continue
            reach = set()
            ch = self.chain(path)
            for level in range(len(ch) - 1, -1, -1):
                cur = ch[level]
                sibs = parents[cur][0]
                up = closure(cur, sibs)
                reach |= up
                for u in up:
                    reach |= nested(sibs[u])
                if level > 0:
                    reach.add(ch[level - 1])
            for r in sorted(refs - reach):
                self.err('ref-not-upstream', '%s references %s, which is not on its runAfter path' % (name, r))

    def vocabulary(self):
        unknown, zero, filt, long_ = set(), {}, 0, []
        for obj in (self.definition.get('triggers'), self.definition.get('actions')):
            for e in self.expressions(obj):
                if len(e) > MAX_EXPR:
                    long_.append(len(e))
                bare = re.sub(r"'(?:[^']|'')*'", "''", e)
                for m in re.finditer(r"(?<![\w.$])([A-Za-z_][A-Za-z0-9_]*)\s*\(", bare):
                    fn = m.group(1)
                    if fn == 'filter':
                        filt += 1
                    elif fn not in WDL_FUNCTIONS:
                        unknown.add(fn)
                for m in re.finditer(r"(?<![\w.$])([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)", bare):
                    if m.group(1) not in ZERO_ARG_OK:
                        zero[m.group(1)] = zero.get(m.group(1), 0) + 1
        if unknown:
            self.err('wdl-unknown-fn', 'unknown (possibly invented) WDL function(s): %s' % ', '.join(sorted(unknown)))
        for fn, c in sorted(zero.items()):
            self.err('wdl-zero-arg', "%s() with no arguments in %d place(s) -- InvalidTemplate at RUN time, not import. "
                     "For an empty array use json('[]')" % (fn, c))
        if filt:
            self.err('wdl-filter-fn', "filter(...) used in %d expression(s): it is not a WDL function -- use a "
                     "Filter array (Query) action; item() inside it binds to the enclosing loop" % filt)
        for n in long_:
            self.err('expr-too-long', 'an expression is %d characters (limit %d) -- precompute parts in Compose actions' % (n, MAX_EXPR))

    def odata(self):
        for name, act, _p, _s in self.everything:
            params = (act.get('inputs') or {}).get('parameters') if isinstance(act.get('inputs'), dict) else None
            if not isinstance(params, dict):
                continue
            cands = []
            if isinstance(params.get('$filter'), str):
                cands.append(params['$filter'])
            uri = params.get('parameters/uri')
            if isinstance(uri, str) and '$filter' in uri:
                cands.append(uri.split('$filter', 1)[1])
            for f in cands:
                if re.search(r'\bcontains\(', f):
                    self.err('odata-v4', "%s: contains() in a SharePoint $filter -- OData v3: use substringof('x', Field)" % name)
                if re.search(r"\s+in\s+\(", f):
                    self.err('odata-v4', '%s: "in (...)" in a SharePoint $filter -- chain eq ... or eq instead' % name)

    def powerapp_contract(self):
        pa_triggers = {n: t for n, t in self.triggers.items() if isinstance(t, dict) and t.get('kind') == 'PowerApp'}
        for tname, trig in pa_triggers.items():
            props = (((trig.get('inputs') or {}).get('schema') or {}).get('properties')) or {}
            for pname, prop in props.items():
                if not isinstance(prop, dict) or not prop.get('x-ms-dynamically-added') or not prop.get('description'):
                    self.err('pa-trigger-flags', "trigger '%s' input '%s' needs x-ms-dynamically-added:true AND a "
                             "description, or the designer throws 'dynamicallyAddedInfo' on Save" % (tname, pname))
                if isinstance(prop, dict) and isinstance(prop.get('type'), list):
                    self.err('pa-trigger-union', "trigger '%s' input '%s' is typed %s -- Studio cannot add or refresh "
                             "the flow (FlowWadlConversionNotSupported); use plain text and JSON-encode optional data"
                             % (tname, pname, prop.get('type')))
            for m in re.finditer(r"coalesce\(\s*triggerBody\(\)\?\['([^']+)'\]", self.defraw):
                self.warn('coalesce-blank', "coalesce(triggerBody()?['%s'], ...) -- a blank app input arrives as \"\" "
                          "(not null) so coalesce returns \"\"; use if(empty(...), default, value)" % m.group(1))
        responses = [(n, a) for n, a, _p, _s in self.everything if a.get('type') == 'Response' and a.get('kind') == 'PowerApp']
        for n, a in responses:
            props = ((((a.get('inputs') or {}).get('schema')) or {}).get('properties')) or {}
            for pname, prop in props.items():
                if not isinstance(prop, dict) or prop.get('x-ms-dynamically-added') is not True:
                    self.err('pa-response-flag', "%s output '%s' needs x-ms-dynamically-added:true -- a legacy import "
                             "silently drops it and Studio sees no return value" % (n, pname))
        if pa_triggers and len(responses) == 1:
            n, a = responses[0]
            states = set()
            for v in (a.get('runAfter') or {}).values():
                states |= set(v)
            if a.get('runAfter') and not ({'Failed', 'TimedOut'} & states):
                self.warn('respond-on-failure', '%s runs only on success -- a failure upstream returns nothing to the '
                          'app (the call times out); add Failed/TimedOut to its runAfter or a second error Respond' % n)

    def connectors(self):
        used = set()
        for n, act, _p, _s in list(self.everything) + [(k, v, (k,), self.triggers) for k, v in self.triggers.items()]:
            if act.get('type') not in CONNECTOR_TYPES:
                continue
            inputs = act.get('inputs') or {}
            host = inputs.get('host') or {}
            cname = host.get('connectionName') or host.get('connectionReferenceName') or ''
            api = (host.get('apiId') or '').split('/')[-1]
            used.add(cname or api)
            if 'authentication' in inputs:
                self.err('legacy-auth', '%s carries inputs.authentication -- legacy package import refuses it '
                         '(WorkflowRunActionInputsInvalidProperty); remove the key' % n)
            if act.get('type') == 'OpenApiConnection' and n in self.by_name and 'retryPolicy' not in inputs:
                self.warn('retry-default', '%s has no retryPolicy -- set {"type":"exponential","count":3,'
                          '"interval":"PT10S","minimumInterval":"PT5S","maximumInterval":"PT1M"} in inputs so a 5xx fails fast' % n)
            hdrs = (inputs.get('parameters') or {}).get('parameters/headers') if isinstance(inputs.get('parameters'), dict) else None
            if isinstance(hdrs, dict) and any(k.lower() == 'x-requestdigest' for k in hdrs):
                self.warn('digest-header', '%s sets X-RequestDigest -- the SharePoint connector adds it itself' % n)
            if host.get('operationId') in LIST_TRIGGERS and n in self.triggers:
                if not act.get('conditions'):
                    self.warn('list-trigger-cond', "trigger '%s' has no trigger condition -- the flow's own writes "
                              "(and other flows' writes) re-fire it; gate on a status column" % n)
        if self.has_conn_block:
            missing = sorted(c for c in used if c and c not in self.conn_refs)
            if missing:
                self.err('conn-ref-missing', "connector(s) used by actions but absent from connectionReferences: %s "
                         "(import error: Property 'host.connectionReferenceName' is missing)" % missing)
        if re.search(r'SP\.Data\.[A-Za-z0-9_]+ListItem', self.defraw):
            self.warn('entity-type-literal', 'hardcoded SP.Data.*ListItem entity type -- read ListItemEntityTypeFullName '
                      'from _api/web/lists(...) at run time (it varies per list, e.g. a trailing 1)')

    def terminate_in_loop(self):
        for n, act, path, _s in self.everything:
            if act.get('type') != 'Terminate':
                continue
            for anc in self.chain(path)[:-1]:
                if (self.by_name.get(anc, ({}, ()))[0] or {}).get('type') in ('Foreach', 'Until'):
                    self.err('terminate-in-loop', '%s (Terminate) sits inside loop %s -- not allowed; set a flag and '
                             'terminate after the loop' % (n, anc))
                    break

    def run(self):
        self.structural()
        self.variables()
        self.runafter_reachability()
        self.vocabulary()
        self.odata()
        self.powerapp_contract()
        self.connectors()
        self.terminate_in_loop()
        return self

    def report(self, strict=False):
        print('== %s' % self.label)
        for n in self.notes:
            print('  ok    %s' % n)
        for r, m in self.warnings:
            print('  WARN  [%s] %s' % (r, m))
        for r, m in self.errors:
            print('  FAIL  [%s] %s' % (r, m))
        print('  %d error(s), %d warning(s)' % (len(self.errors), len(self.warnings)))
        return 1 if self.errors or (strict and self.warnings) else 0


def load_path(p):
    if os.path.isdir(p):
        p = os.path.join(p, 'definition.json')
    return io.open(p, encoding='utf-8-sig').read(), p


# ------------------------------------------------------------------------------------ self-test
def _clean_fixture():
    return {
        'properties': {
            'displayName': 'SelfTestFlow',
            'connectionReferences': {'shared_sharepointonline': {
                'connectionName': 'shared-sharepointonline-selftest', 'source': 'Embedded',
                'id': '/providers/Microsoft.PowerApps/apis/shared_sharepointonline', 'tier': 'NotSpecified',
                'apiName': 'sharepointonline'}},
            'definition': {
                '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#',
                'contentVersion': '1.0.0.0',
                'parameters': {'$connections': {'defaultValue': {}, 'type': 'Object'},
                               '$authentication': {'defaultValue': {}, 'type': 'SecureObject'}},
                'triggers': {'manual': {'type': 'Request', 'kind': 'PowerApp', 'inputs': {'schema': {
                    'type': 'object', 'required': ['payload'],
                    'properties': {'payload': {'title': 'payload', 'type': 'string', 'x-ms-content-hint': 'TEXT',
                                               'x-ms-dynamically-added': True, 'description': 'JSON payload'}}}}}},
                'actions': {
                    'Compose_Payload': {'type': 'Compose', 'runAfter': {},
                                        'inputs': "@json(if(empty(triggerBody()?['payload']), '{}', triggerBody()?['payload']))"},
                    'Initialize_Count': {'type': 'InitializeVariable', 'runAfter': {'Compose_Payload': ['Succeeded']},
                                         'inputs': {'variables': [{'name': 'Count', 'type': 'integer', 'value': 0}]}},
                    'Get_Items': {'type': 'OpenApiConnection', 'runAfter': {'Initialize_Count': ['Succeeded']},
                                  'inputs': {'host': {'apiId': '/providers/Microsoft.PowerApps/apis/shared_sharepointonline',
                                                      'connectionName': 'shared_sharepointonline', 'operationId': 'GetItems'},
                                             'parameters': {'dataset': 'https://contoso.sharepoint.com/sites/HelpDesk',
                                                            'table': 'Tickets', '$filter': "substringof('x', Title)"},
                                             'retryPolicy': {'type': 'exponential', 'count': 3, 'interval': 'PT10S'}}},
                    'Set_Count': {'type': 'SetVariable', 'runAfter': {'Get_Items': ['Succeeded']},
                                  'inputs': {'name': 'Count', 'value': "@length(outputs('Get_Items')?['body/value'])"}},
                    'Respond_to_PowerApps': {'type': 'Response', 'kind': 'PowerApp',
                                             'runAfter': {'Set_Count': ['Succeeded', 'Failed', 'TimedOut', 'Skipped']},
                                             'inputs': {'statusCode': 200, 'body': {'result_json': "@{variables('Count')}"},
                                                        'schema': {'type': 'object', 'properties': {'result_json': {
                                                            'title': 'result_json', 'type': 'string',
                                                            'x-ms-dynamically-added': True}}}}},
                },
            },
        }
    }


def self_test():
    ok = True

    def run(doc):
        return Checker(json.dumps(doc), 'fixture').run()

    def expect(mutator, rule, kind='error', label=None):
        nonlocal ok
        doc = _clean_fixture()
        mutator(doc)
        c = run(doc)
        pool = c.errors if kind == 'error' else c.warnings
        hit = any(r == rule for r, _m in pool)
        print(('  ok   ' if hit else '  FAIL ') + 'mutation caught: %s (%s)' % (label or rule, kind))
        ok = ok and hit

    base = run(_clean_fixture())
    clean = not base.errors and not base.warnings
    print(('  ok   ' if clean else '  FAIL ') + 'clean fixture passes with no errors or warnings %s' % (base.errors + base.warnings if not clean else ''))
    ok = ok and clean
    A = lambda d: d['properties']['definition']['actions']  # noqa: E731
    T = lambda d: d['properties']['definition']['triggers']  # noqa: E731
    expect(lambda d: A(d)['Get_Items']['inputs'].__setitem__('authentication', "@parameters('$authentication')"), 'legacy-auth')
    expect(lambda d: d['properties'].__setitem__('connectionReferences', {}), 'conn-ref-missing')
    expect(lambda d: A(d)['Set_Count']['inputs'].__setitem__('value', "@add(variables('Count'), 1)"), 'var-self-reference')
    expect(lambda d: A(d)['Compose_Payload'].__setitem__('inputs', '@createArray()'), 'wdl-zero-arg')
    expect(lambda d: A(d)['Compose_Payload'].__setitem__('inputs', "@createObject('a', 1)"), 'wdl-unknown-fn')
    expect(lambda d: A(d)['Compose_Payload'].__setitem__('inputs', "@filter(json('[]'), item())"), 'wdl-filter-fn')
    expect(lambda d: A(d)['Get_Items']['inputs']['parameters'].__setitem__('$filter', "contains(Title,'x')"), 'odata-v4')
    expect(lambda d: T(d)['manual']['inputs']['schema']['properties']['payload'].pop('x-ms-dynamically-added'), 'pa-trigger-flags')
    expect(lambda d: T(d)['manual']['inputs']['schema']['properties']['payload'].pop('description'), 'pa-trigger-flags', label='pa-trigger-flags (description)')
    expect(lambda d: T(d)['manual']['inputs']['schema']['properties']['payload'].__setitem__('type', ['string', 'null']), 'pa-trigger-union')
    expect(lambda d: A(d)['Respond_to_PowerApps']['inputs']['schema']['properties']['result_json'].pop('x-ms-dynamically-added'), 'pa-response-flag')
    expect(lambda d: A(d)['Set_Count'].__setitem__('runAfter', {'Nope': ['Succeeded']}), 'runafter-sibling')
    expect(lambda d: A(d)['Set_Count']['inputs'].__setitem__('value', "@length(outputs('Ghost'))"), 'ref-unknown')
    expect(lambda d: A(d)['Initialize_Count']['inputs'].__setitem__('variables', [{'name': 'Other', 'type': 'integer', 'value': 0}]), 'var-uninitialised')
    expect(lambda d: A(d)['Compose_Payload'].__setitem__('inputs', "@concat('" + 'x' * 9000 + "')"), 'expr-too-long')

    def not_upstream(d):
        A(d)['Initialize_Count']['inputs']['variables'][0]['value'] = "@length(outputs('Get_Items')?['body/value'])"
    expect(not_upstream, 'ref-not-upstream')

    def result_non_scope(d):
        A(d)['Compose_Payload']['inputs'] = "@result('Get_Items')"
    expect(result_non_scope, 'result-non-scope')

    def terminate(d):
        A(d)['Loop'] = {'type': 'Foreach', 'foreach': "@outputs('Get_Items')?['body/value']",
                        'runAfter': {'Set_Count': ['Succeeded']},
                        'actions': {'Stop': {'type': 'Terminate', 'runAfter': {}, 'inputs': {'runStatus': 'Failed'}}}}
    expect(terminate, 'terminate-in-loop')

    def nesting(d):
        inner = {'type': 'Compose', 'runAfter': {}, 'inputs': 'x'}
        for i in range(10):
            inner = {'type': 'Scope', 'runAfter': {}, 'actions': {'S%d' % i: inner}}
        A(d)['Deep'] = dict(inner, runAfter={'Set_Count': ['Succeeded']})
    expect(nesting, 'nesting')

    def dupkey(_d):
        pass
    raw = json.dumps(_clean_fixture())
    raw = raw.replace('"contentVersion": "1.0.0.0"', '"contentVersion": "1.0.0.0", "contentVersion": "1.0.0.1"', 1)
    hit = any(r == 'json-dup-key' for r, _m in Checker(raw, 'dup').run().errors)
    print(('  ok   ' if hit else '  FAIL ') + 'mutation caught: json-dup-key (error)')
    ok = ok and hit

    expect(lambda d: A(d)['Get_Items']['inputs'].pop('retryPolicy'), 'retry-default', 'warning')
    expect(lambda d: A(d)['Compose_Payload'].__setitem__('inputs', "@coalesce(triggerBody()?['payload'], '{}')"), 'coalesce-blank', 'warning')
    expect(lambda d: A(d)['Respond_to_PowerApps'].__setitem__('runAfter', {'Set_Count': ['Succeeded']}), 'respond-on-failure', 'warning')
    expect(lambda d: A(d)['Initialize_Count']['inputs']['variables'].append({'name': 'Dead', 'type': 'string'}), 'var-unused', 'warning')

    def list_trigger(d):
        T(d).clear()
        T(d)['When_changed'] = {'type': 'OpenApiConnection', 'recurrence': {'frequency': 'Minute', 'interval': 1},
                                'inputs': {'host': {'apiId': '/providers/Microsoft.PowerApps/apis/shared_sharepointonline',
                                                    'connectionName': 'shared_sharepointonline', 'operationId': 'GetOnUpdatedItems'},
                                           'parameters': {'dataset': 'https://contoso.sharepoint.com/sites/HelpDesk', 'table': 'Tickets'}}}
    expect(list_trigger, 'list-trigger-cond', 'warning')
    print('self-test: ' + ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--strict', action='store_true', help='warnings also fail')
    ap.add_argument('--self-test', action='store_true')
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.paths:
        ap.error('give at least one definition.json or flow source folder')
    rc = 0
    for p in a.paths:
        raw, path = load_path(p)
        rc |= Checker(raw, path).run().report(a.strict)
    return rc


if __name__ == '__main__':
    sys.exit(main())
