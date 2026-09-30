"""Canvas-app DOCUMENT (.msapp) surgery for headless deploys: flow bindings, dependency wiring, premium filter,
embedded SharePoint schemas and embedded flow signatures.

Why this exists: a canvas app carries TENANT bindings inside its document, and the service compiles the published
app against the copies it embeds. Every function here reproduces something Studio does on open/refresh, measured
by diffing Studio-saved documents:

  flow binding      Properties.json LocalConnectionReferences (a JSON STRING) holds one entry per connection. A flow
                    entry's connectionInstanceId must be /providers/microsoft.powerapps/apis/shared_logicflows/
                    connections/<de-dashed workflow GUID>-<16 hex>; a bare dashed GUID binds as "added with the old
                    method" (cannot refresh, missing from + Add flow). GUID case, path case and the suffix VALUE were
                    cosmetic across two working apps; the FORMAT is required.
  flow deps         each flow entry lists, per connector its OWN definition calls:
                      dependencies[api] = depKey, connectionRef.parameterHints[depKey] = {value: api},
                      connectionRef.parameterHintsV2[api] = {value: depKey}, and depKey's entry lists the flow in
                      dependents. A flow-only connector gets a standalone entry (dataSources []). Without this the
                      flow shows "Not connected". Studio never wires flow->flow (shared_logicflows) dependencies.
                    A PowerApp-trigger flow ALWAYS runs its connections as the invoker, so the app must carry them.
  premium           a premium connector reference on the APP makes every user need a premium licence
                    (InsufficientPlanForApp at launch). Built-in HTTP inside an app-called flow does NOT. So premium
                    connectors are never wired as flow dependencies (that call then fails at run time -- design the
                    flow without them), and a premium DATA SOURCE is an error.
  table schemas     References/DataSources.json embeds each SharePoint list's schema; formulas on newer columns compile
                    to NULL rules until refreshed. Studio re-reads <runtime>/apim/sharepointonline/<conn>/$metadata.json/
                    datasets/<site double-encoded>/tables/<list GUID> and stores it verbatim + a display-name mapping.
  flow signatures   each flow data source embeds a WADL of Run() inputs + Respond outputs; a stale copy compiles every
                    call to NULL. Studio's Refresh = POST .../flows/<id>/listWadl, stored verbatim with siena:serviceId
                    pinned to the data-source name. listWadl 400s (FlowWadlConversionNotSupported) on a trigger input
                    typed ["string","null"] -- and so does Studio's Refresh ("Unable to add flow").
"""
import io
import json
import re
import uuid
import zipfile

LOGICFLOWS = 'shared_logicflows'
PREMIUM_CONNECTORS = {'shared_webcontents', 'shared_commondataserviceforapps', 'shared_commondataservice', 'shared_sql',
                      'shared_azureblob', 'shared_servicebus', 'shared_eventhubs', 'shared_documentdb', 'shared_azuread',
                      'shared_http'}
DEFAULT_SUFFIX = '0f0f0f0f0f0f0f0f'   # used only when the app has no existing flow entry to copy a suffix from
BS = chr(92)


# ------------------------------------------------------------------------------------------------ msapp io
class Msapp(object):
    """In-memory .msapp: entries by forward-slash name, original names/infos kept for a faithful rewrite."""

    def __init__(self, path):
        self.path = path
        self.infos, self.data = [], {}
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                self.infos.append(info)
                self.data[info.filename.replace(BS, '/')] = z.read(info.filename)

    def text(self, name):
        b = self.data.get(name)
        return None if b is None else b.decode('utf-8-sig')

    def json(self, name):
        t = self.text(name)
        return None if t is None else json.loads(t)

    def put_json(self, name, obj):
        self.data[name] = json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')

    def save(self, out):
        seen = set()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for info in self.infos:
                n = info.filename.replace(BS, '/')
                seen.add(n)
                z.writestr(info, self.data[n])
            for n, b in self.data.items():
                if n not in seen:
                    z.writestr(n, b)
        return out

    def refs(self):
        props = self.json('Properties.json') or {}
        raw = props.get('LocalConnectionReferences') or '{}'
        return json.loads(raw) if isinstance(raw, str) else raw

    def set_refs(self, refs):
        props = self.json('Properties.json')
        props['LocalConnectionReferences'] = json.dumps(refs, separators=(',', ':'), ensure_ascii=False)
        self.put_json('Properties.json', props)

    def data_sources(self):
        return (self.json('References/DataSources.json') or {}).get('DataSources') or []

    def formulas_text(self):
        """Formulas live in Src/*.pa.yaml for a LoadFromYaml package and in Controls/*.json otherwise: scan both."""
        return '\n'.join(b.decode('utf-8', 'replace') for n, b in self.data.items()
                         if (n.startswith('Src/') and n.endswith('.pa.yaml')) or (n.startswith('Controls/') and n.endswith('.json')))


def api_of(ref_entry):
    m = re.search(r'/apis/([^/]+)$', ((ref_entry or {}).get('connectionRef') or {}).get('id', ''))
    return m.group(1) if m else ''


def is_premium(api, tier=''):
    return (tier or '').lower() == 'premium' or (api or '').lower() in PREMIUM_CONNECTORS


def premium_references(refs, tiers=None):
    hits = []
    for k, e in (refs or {}).items():
        api = api_of(e)
        if api and is_premium(api, (tiers or {}).get(api, '')):
            hits.append({'api': api, 'key': k, 'dataSources': [d for d in e.get('dataSources') or [] if d],
                         'dependents': [d for d in e.get('dependents') or [] if d]})
    return hits


def _suffix_from(refs):
    for e in refs.values():
        m = re.search(r'/connections/[0-9A-Fa-f]{32}-([0-9A-Fa-f]{16})$', e.get('connectionInstanceId', ''))
        if m and api_of(e) == LOGICFLOWS:
            return m.group(1)
    return DEFAULT_SUFFIX


# ------------------------------------------------------------------------------------------------ bindings
def add_flow_references(refs, data_sources, logicflows_icon=''):
    """Every flow data source (ConnectedWadl + FlowNameId) gets an owning LocalConnectionReferences entry in Studio's
    shape; an owner whose workflowName differs from its data source's FlowNameId is REALIGNED to the data source
    (the data source is the truth of which flow the formulas call). Returns (refs, added, realigned, left)."""
    refs = refs or {}
    suffix = _suffix_from(refs)
    icon = logicflows_icon
    owned = {}
    for k, e in refs.items():
        for n in e.get('dataSources') or []:
            if n:
                owned[n] = k
        if api_of(e) == LOGICFLOWS and not icon:
            icon = (e.get('connectionRef') or {}).get('iconUri', '')
    added, realigned, left = [], [], []
    for ds in data_sources or []:
        if ds.get('ServiceKind') != 'ConnectedWadl':
            continue
        name, guid = ds.get('Name', ''), ds.get('FlowNameId', '')
        if not name or not re.match(r'^[0-9a-fA-F-]{36}$', guid):
            continue
        inst = '/providers/microsoft.powerapps/apis/shared_logicflows/connections/%s-%s' % (guid.replace('-', '').lower(), suffix)
        if name in owned:
            e = refs[owned[name]]
            cr = e.get('connectionRef') or {}
            if api_of(e) != LOGICFLOWS:
                continue
            current = ((cr.get('parameterHints') or {}).get('workflowName') or {}).get('value', '')
            if current == guid:
                continue
            m = re.search(r'/connections/[0-9A-Fa-f]{32}-([0-9A-Fa-f]{16})$', e.get('connectionInstanceId', ''))
            e['connectionInstanceId'] = inst if not m else inst[:-16] + m.group(1)
            for hk in ('parameterHints', 'parameterHintsV2'):
                h = cr.setdefault(hk, {})
                h['workflowName'] = {'value': guid}
                h.setdefault('workflowEntityId', {})
                h['workflowDisplayName'] = {'value': name}
            realigned.append(name)
            continue
        if not icon:
            left.append(name)      # never write an entry without displayName + iconUri (Studio drops ALL refs)
            continue
        key = str(uuid.uuid4())
        hints = {'workflowName': {'value': guid}, 'workflowEntityId': {}, 'workflowDisplayName': {'value': name}}
        refs[key] = {'id': key, 'connectionInstanceId': inst, 'dataSources': [name], 'datasets': {}, 'dependencies': {},
                     'dependents': [], 'connectionRef': {'id': '/providers/microsoft.powerapps/apis/shared_logicflows',
                                                         'displayName': 'Logic flows', 'iconUri': icon,
                                                         'parameterHints': json.loads(json.dumps(hints)),
                                                         'parameterHintsV2': json.loads(json.dumps(hints))}}
        owned[name] = key
        added.append(name)
    return refs, added, realigned, left


def set_flow_dependencies(refs, flow_connectors, samples=None):
    """Rebuild the per-flow connector wiring Studio writes (see module doc). flow_connectors: {guidNoDashLower: [api]}.
    samples: {api: {connectionName, displayName, iconUri, tier}}. Returns (refs, report) where report has
    'leftForStudio' (connectors with no complete sample), 'premiumSkipped' and 'premiumBound'."""
    samples = samples or {}
    report = {'leftForStudio': [], 'premiumSkipped': [], 'premiumBound': []}
    if refs is None or flow_connectors is None:
        return refs, report
    prem = lambda a: is_premium(a, (samples.get(a) or {}).get('tier', ''))  # noqa: E731
    placeholders = {k for k, e in refs.items() if api_of(e) != LOGICFLOWS and not [d for d in e.get('dataSources') or [] if d]}
    by_api = {}
    for k, e in refs.items():
        a = api_of(e)
        if a and a != LOGICFLOWS:
            by_api.setdefault(a, k)
    suffix = _suffix_from(refs)
    for k in list(refs.keys()):
        e = refs[k]
        cr = e.setdefault('connectionRef', {})
        if api_of(e) != LOGICFLOWS:
            continue
        guid = ((cr.get('parameterHints') or {}).get('workflowName') or {}).get('value', '')
        if not guid:
            m = re.search(r'/connections/([0-9A-Fa-f]{32})-', e.get('connectionInstanceId', ''))
            guid = m.group(1) if m else ''
        look = guid.replace('-', '').lower()
        if re.match(r'^[0-9a-f]{32}$', look):     # canonical <deDashedGuid>-<suffix>, keep an existing suffix
            m = re.search(r'/connections/[0-9A-Fa-f]{32}-([0-9A-Fa-f]{16})$', e.get('connectionInstanceId', ''))
            e['connectionInstanceId'] = '/providers/microsoft.powerapps/apis/shared_logicflows/connections/%s-%s' % (look, m.group(1) if m else suffix)
        in_map = look in flow_connectors
        label = ((cr.get('parameterHints') or {}).get('workflowDisplayName') or {}).get('value') or (e.get('dataSources') or [guid])[0]
        apis = []
        for a in (flow_connectors.get(look) or []):
            if not a or a == LOGICFLOWS:
                continue
            if prem(a):
                report['premiumSkipped'].append('%s: %s' % (label, a))
                continue
            apis.append(a)
        deps = e.setdefault('dependencies', {})
        cr.setdefault('parameterHints', {})
        cr.setdefault('parameterHintsV2', {})
        for stale in list(deps.keys()):
            if in_map and stale in apis:
                continue
            if not in_map and not prem(stale):
                continue
            if not in_map:
                report['premiumSkipped'].append('%s: %s' % (label, stale))
            skey = deps.pop(stale)
            cr['parameterHints'].pop(skey, None)
            cr['parameterHintsV2'].pop(stale, None)
            if skey in refs:
                refs[skey]['dependents'] = [d for d in refs[skey].get('dependents') or [] if d != k]
        if not in_map:
            continue
        for a in apis:
            dep_key = by_api.get(a)
            if not dep_key:
                s = samples.get(a) or {}
                if not (s.get('connectionName') and s.get('displayName') and s.get('iconUri')):
                    if a not in report['leftForStudio']:
                        report['leftForStudio'].append(a)
                    continue
                dep_key = str(uuid.uuid4())
                refs[dep_key] = {'id': dep_key, 'connectionInstanceId': '/providers/microsoft.powerapps/apis/%s/connections/%s' % (a, s['connectionName']),
                                 'dataSources': [], 'datasets': {}, 'dependencies': {}, 'dependents': [],
                                 'connectionRef': {'id': '/providers/microsoft.powerapps/apis/' + a, 'displayName': s['displayName'],
                                                   'iconUri': s['iconUri'], 'parameterHints': {}, 'parameterHintsV2': {}}}
                by_api[a] = dep_key
            deps[a] = dep_key
            cr['parameterHints'][dep_key] = {'value': a}
            cr['parameterHintsV2'][a] = {'value': dep_key}
            dl = refs[dep_key].setdefault('dependents', [])
            if k not in dl:
                dl.append(k)
    for pk in placeholders:
        if pk in refs and not [d for d in refs[pk].get('dependents') or [] if d]:
            del refs[pk]
    for hit in premium_references(refs):
        if hit['dataSources']:
            report['premiumBound'].append('%s: %s' % (', '.join(hit['dataSources']), hit['api']))
    return refs, report


def app_actions(msapp):
    """data source name -> WADL method ids the formulas actually call (Name.Method( or 'Name'.Method( )."""
    text = msapp.formulas_text()
    out = {}
    for ds in msapp.data_sources():
        wadl = (ds.get('WadlMetadata') or {}).get('WadlXml') or ''
        used = []
        for mid in re.findall(r'<method\b[^>]*\bid="([^"]+)"', wadl):
            n = re.escape(ds.get('Name', ''))
            if re.search(r"(?<![A-Za-z0-9_])(?:%s|'%s')\s*\.\s*%s\s*\(" % (n, n, re.escape(mid)), text) and mid not in used:
                used.append(mid)
        out[ds.get('Name')] = used
    return out


def runtime_references(msapp, flow_connectors=None, samples=None, logicflows_icon=''):
    """The app-level connectionReferences object for the leased draft PUT / metadata PATCH (not the package)."""
    refs = msapp.refs()
    refs, _a, _r, left = add_flow_references(refs, msapp.data_sources(), logicflows_icon)
    report = {'leftForStudio': list(left), 'premiumSkipped': [], 'premiumBound': []}
    if flow_connectors:
        refs, rep = set_flow_dependencies(refs, flow_connectors, samples)
        for k in report:
            report[k] += rep[k]
    actions = app_actions(msapp)
    out = {}
    for key, e in refs.items():
        inst = e.get('connectionInstanceId', '')
        m = re.match(r'^/providers/microsoft\.powerapps/apis/([^/]+)/connections/([^/]+)$', inst)
        if not m:
            raise SystemExit('invalid connection instance for app reference %s: %r' % (key, inst))
        cr = e.get('connectionRef') or {}
        if not re.search('/apis/%s$' % re.escape(m.group(1)), cr.get('id', '')):
            raise SystemExit('missing or mismatched connector metadata for app reference %s' % key)
        ref = dict(cr)
        for fld in ('dataSources', 'dependents', 'isOnPremiseConnection', 'bypassConsent'):
            if e.get(fld) is not None:
                ref[fld] = e[fld]
        ref['dataSets'] = e.get('datasets')
        ref['dependencies'] = [d for d in (e.get('dependencies') or {}).values()]
        for fld in ('parameterHints', 'parameterHintsV2'):
            ref.setdefault(fld, {})
        for fld in ('isOnPremiseConnection', 'bypassConsent'):
            ref.setdefault(fld, False)
        acts = list(e.get('appActions') or [])
        for ds_name in e.get('dataSources') or []:
            for a in actions.get(ds_name, []):
                if a not in acts:
                    acts.append(a)
        ref['actions'] = acts
        out[key] = ref
    return refs, out, report


# ------------------------------------------------------------------------------------------------ table schemas
def cdp_time(value):
    m = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:Z|\+00:00)?$', value or '')
    if not m:
        return ''
    return '%s.%s0000Z' % (m.group(1), ((m.group(2) or '') + '000')[:3])


def name_mapping(properties):
    """schema.items.properties (in response order) -> logical -> display name, Studio's collision rule:
    a title equal to ANOTHER property's logical name, or already handed out, becomes '<title> (<logical>)'."""
    keys = list(properties.keys())
    low = {}
    for k in keys:
        low[k.lower()] = low.get(k.lower(), 0) + 1
    used, out = set(), {}
    for k in keys:
        p = properties[k] or {}
        if p.get('title') is None:
            continue
        t = str(p['title'])
        hits = low.get(t.lower(), 0) - (1 if k.lower() == t.lower() else 0)
        if hits > 0 or t.lower() in used:
            out[k] = '%s (%s)' % (t, k)
        else:
            used.add(t.lower())
            out[k] = t
    return out


def sharepoint_tables(msapp):
    by_ds, conns = {}, []
    for e in msapp.refs().values():
        m = re.search(r'/apis/shared_sharepointonline/connections/([^/]+)$', e.get('connectionInstanceId', ''))
        if not m:
            continue
        if m.group(1) not in conns:
            conns.append(m.group(1))
        for n in e.get('dataSources') or []:
            by_ds[n] = m.group(1)
    out = []
    for d in msapp.data_sources():
        if d.get('Type') == 'ConnectedDataSourceInfo' and d.get('ApiId') == '/providers/microsoft.powerapps/apis/shared_sharepointonline':
            conn = by_ds.get(d.get('Name')) or (conns[0] if len(conns) == 1 else '')
            out.append({'name': d.get('Name'), 'dataset': d.get('DatasetName'), 'table': d.get('TableName'), 'connection': conn})
    return out


def refresh_table_schemas(msapp, metadata_by_table, connector_changed_time=''):
    """Rewrite every SharePoint data source from live $metadata.json text. Fails closed on any missing table."""
    doc = msapp.json('References/DataSources.json')
    lines, missing = [], []
    for d in doc.get('DataSources') or []:
        if not (d.get('Type') == 'ConnectedDataSourceInfo' and d.get('ApiId') == '/providers/microsoft.powerapps/apis/shared_sharepointonline'):
            continue
        text = metadata_by_table.get(d.get('TableName'))
        if text is None:
            missing.append(d.get('Name'))
            continue
        new_map = name_mapping(json.loads(text)['schema']['items']['properties'])
        old = d.get('ConnectedDataSourceInfoNameMapping') or {}
        added = [k for k in new_map if k not in old]
        removed = [k for k in old if k not in new_map]
        d['DataEntityMetadataJson'] = {d['TableName']: text}
        d['ConnectedDataSourceInfoNameMapping'] = new_map
        if connector_changed_time:
            rev = d.setdefault('CdpRevision', {'RevisionNumber': 1, 'BaseUrl': '/'})
            rev['LastChangedTimeString'] = connector_changed_time
        lines.append('SCHEMA %s: +%d %s / -%d %s' % (d.get('Name'), len(added), added, len(removed), removed))
    if missing:
        raise SystemExit('no live table metadata for SharePoint data source(s) %s -- refusing to leave a stale schema' % missing)
    msapp.put_json('References/DataSources.json', doc)
    return lines


# ------------------------------------------------------------------------------------------------ flow signatures
def wadl_signature(wadl):
    sig = {'serviceId': '', 'inputs': [], 'outputs': []}
    if not wadl:
        return sig
    m = re.search(r'siena:serviceId="([^"]*)"', wadl)
    sig['serviceId'] = m.group(1) if m else ''
    req = re.search(r'(?s)<request>.*?<representation mediaType="application/json">(.*?)</representation>', wadl)
    if req:
        for tag in re.findall(r'<param\s[^>]*?style="plain"[^>]*>', req.group(1)):
            n = re.search(r'\sname="([^"]+)"', tag).group(1)
            sig['inputs'].append(n + ('*' if 'required="true"' in tag else ''))
    obj = re.search(r'(?s)<(?:siena:)?object name="ResponseActionOutput">(.*?)</(?:siena:)?object>', wadl)
    if obj:
        for n, t in re.findall(r'<(?:siena:)?property name="([^"]+)"(?:\s+type="([^"]*)")?', obj.group(1)):
            sig['outputs'].append('%s:%s' % (n, t or 'object'))
    return sig


def definition_signature(definition):
    trig = next((t for t in (definition.get('triggers') or {}).values() if t.get('kind') in ('PowerApp', 'PowerAppV2')), None)
    if trig is None:
        raise SystemExit('the flow has no PowerApp trigger (not callable from a canvas app)')
    schema = (trig.get('inputs') or {}).get('schema') or {}
    req = schema.get('required') or []
    ins = [k + ('*' if k in req else '') for k in (schema.get('properties') or {})]
    outs = []

    def walk(node):
        if isinstance(node, dict):
            if node.get('type') == 'Response' and node.get('kind') == 'PowerApp':
                for k in (((node.get('inputs') or {}).get('schema') or {}).get('properties') or {}):
                    if k not in outs:
                        outs.append(k)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(definition.get('actions'))
    return {'inputs': ins, 'outputs': outs}


def refresh_flow_signatures(msapp, live_by_flow):
    """live_by_flow: {flowGuidLower: WADL text | {'unsupported': True, 'inputs': [...], 'outputs': [...]}}."""
    doc = msapp.json('References/DataSources.json')
    lines, missing = [], []
    for d in doc.get('DataSources') or []:
        if d.get('ServiceKind') != 'ConnectedWadl' or (d.get('ApiId') or '').lower() != '/providers/microsoft.powerapps/apis/shared_logicflows':
            continue
        fid = (d.get('FlowNameId') or '').lower()
        if not fid:
            continue
        live = live_by_flow.get(fid)
        if live is None:
            missing.append(d.get('Name'))
            continue
        wm = d.setdefault('WadlMetadata', {})
        old = wadl_signature(wm.get('WadlXml') or '')
        if isinstance(live, dict):
            old_out = [o.split(':')[0] for o in old['outputs']]
            if old['inputs'] != live['inputs'] or old_out != live['outputs']:
                raise SystemExit('flow %s: not expressible in WADL (["string","null"] input?) AND the embedded signature is stale: '
                                 'inputs %s vs live %s, outputs %s vs live %s' % (d.get('Name'), old['inputs'], live['inputs'], old_out, live['outputs']))
            lines.append('SIGNATURE %s: not expressible in WADL -- embedded copy KEPT (names match the live flow)' % d.get('Name'))
            continue
        if 'siena:serviceId="' not in live:
            raise SystemExit('live WADL for %s has no siena:serviceId' % d.get('Name'))
        new = re.sub(r'siena:serviceId="[^"]*"', 'siena:serviceId="%s"' % d.get('Name').replace('\\', '\\\\'), live)
        sig = wadl_signature(new)
        wm['WadlXml'] = new
        delta = []
        if old['inputs'] != sig['inputs']:
            delta.append('inputs %s -> %s' % (old['inputs'], sig['inputs']))
        if old['outputs'] != sig['outputs']:
            delta.append('outputs %s -> %s' % (old['outputs'], sig['outputs']))
        lines.append('SIGNATURE %s: %s' % (d.get('Name'), '; '.join(delta) or 'unchanged'))
    if missing:
        raise SystemExit('no live signature for flow data source(s) %s -- refusing to leave a stale Run() signature' % missing)
    msapp.put_json('References/DataSources.json', doc)
    return lines
