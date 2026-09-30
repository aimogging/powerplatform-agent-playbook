"""Find canvas-app rules the service compiled to NULL -- the silent failure no authoring tool reports.

MEASURED: the player compiles the published document into a JavaScript runtime package. A rule the compiler cannot
bind -- any UTCNow(), a column missing from the EMBEDDED list schema, a Run() call whose arity does not match the
embedded flow signature, Concat(Filter(t As X ...)) in a label -- is emitted as a rule that evaluates to null, while
Studio, pac, App checker and the launch packageStatus=Ready are all green. Symptoms: a button that does nothing, an
OnStart that never runs (themed areas black, data never loads).

Shapes in the compiled JS (control ids resolve to names through __addC(_0[<idx>],"<id>") and the _0=[...] string table):
  async    _re0("<id>","<Prop>",function(_as){return _w0(null)...        behaviour and async data rules
  sync     _re6["<id>.<Prop>"]=function(bc){_rg1("<id>",null).OpenAjax.sv("<Prop>",null,bc);}
  handler  _re1("<id>.<Prop>",null,false)   IDENTICAL to an empty property: only a null when the document HAS a formula
Null rules can hide in js/initScreen_N.js chunks, not only js/init.js.

Two ways to get the JS:
  launch   POST <env host>/powerapps/apps/<id>/launch?api-version=2 -> webPackage SAS -> web/manifest.json -> js files
           (devtenant.powerapps.PowerAppsClient.launch; needs a Power Apps-audience token)
  browser  capture every /appruntime/ response while the player loads the app (devtenant.appdriver.capture_runtime)
"""
import json
import os
import re

ASYNC = re.compile(r'_re0\("(\d+)","([A-Za-z0-9_]+)",function\(_as\)\{return _w0\(null\)')
SYNC = re.compile(r'_re6\["(\d+)\.([A-Za-z0-9_]+)"\]=function\(bc\)\{_rg1\("\d+",null\)\.OpenAjax\.sv\("[A-Za-z0-9_]+",null,bc\);\}')
HANDLER = re.compile(r'_re1\("(\d+)\.([A-Za-z0-9_]+)",null,false\)')
STATIC = re.compile(r'(?<!=function\(bc\)\{)_rg1\("(\d+)",null\)\.OpenAjax\.sv\("([A-Za-z0-9_]+)",null,bc\)')
TRIVIAL = re.compile(r'^\s*=?\s*(true|false|blank\(\)|-?\d+(\.\d+)?|"[^"]*")?\s*$', re.I)


def control_names(js_texts):
    names = {}
    for t in js_texts:
        table = []
        m = re.search(r'(?<![\w$])_0=\[((?:"(?:[^"\\]|\\.)*"|[^\]"])*)\]', t)
        if m:
            table = [json.loads(x) if x.startswith('"') else x for x in re.findall(r'"(?:[^"\\]|\\.)*"|[^,"]+', m.group(1))]
        for idx, cid in re.findall(r'__addC\(_0\[(\d+)\],"(\d+)"', t):
            if int(idx) < len(table):
                names[cid] = table[int(idx)]
        for n, cid in re.findall(r'__addC\("((?:[^"\\]|\\.)*)","(\d+)"', t):
            names[cid] = n
        for n, cid in re.findall(r'\{name:"([^"]+)",uniqueId:"(\d+)"', t):
            names[cid] = n
    return names


def document_formulas(msapp):
    """(control, property) -> formula from the PUBLISHED document: Src/*.pa.yaml when packed.json says LoadFromYaml,
    else Controls/*.json rules. `msapp` is a canvasdoc.Msapp."""
    out = {}
    if msapp.text('packed.json') and 'LoadFromYaml' in msapp.text('packed.json'):
        import importlib.util
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location('canvas_lint', os.path.join(here, 'canvas-lint.py'))
        cl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cl)
        for n, b in msapp.data.items():
            if n.startswith('Src/') and n.endswith('.pa.yaml'):
                for e in cl.parse_pa_yaml(b.decode('utf-8-sig'), n):
                    for p, (v, _l) in e.props.items():
                        out[(e.name, p)] = v
        return out
    for n, b in msapp.data.items():
        if n.startswith('Controls/') and n.endswith('.json'):
            def walk(node):
                if isinstance(node, dict):
                    if 'Name' in node and isinstance(node.get('Rules'), list):
                        for r in node['Rules']:
                            out[(node['Name'], r.get('Property'))] = r.get('InvariantScript', '')
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
            walk(json.loads(b.decode('utf-8-sig')))
    return out


def scan(js_texts, formulas=None):
    """-> [(control, property, shape)]. `formulas` (from document_formulas) decides the ambiguous handler/static shapes."""
    names = control_names(js_texts)
    found = []
    for t in js_texts:
        for cid, prop in ASYNC.findall(t):
            found.append((names.get(cid, cid), prop, 'async'))
        for cid, prop in SYNC.findall(t):
            found.append((names.get(cid, cid), prop, 'sync'))
        if formulas is not None:
            for rx, shape in ((HANDLER, 'handler'), (STATIC, 'static')):
                for cid, prop in rx.findall(t):
                    f = formulas.get((names.get(cid, cid), prop))
                    if f and not TRIVIAL.match(f):
                        found.append((names.get(cid, cid), prop, shape))
    seen, out = set(), []
    for x in found:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def fetch_package_js(http, launch_json):
    """launch response -> [js text] via the web package SAS (no Authorization header on SAS URLs)."""
    det = (launch_json or {}).get('listAppPackageOperationDetails') or {}
    web = ((det.get('appPackageDetails') or {}).get('webPackage') or {}).get('value')
    if not web:
        raise SystemExit('launch gave no webPackage (packageStatus=%s, error=%s)' % (det.get('packageStatus'), det.get('error')))
    base, query = web.split('?', 1)
    base = base.rsplit('/', 1)[0] + '/'
    files = ['js/init.js']
    r = http.request('GET', base + 'manifest.json?' + query, headers={})
    if r.status == 200:
        files = [f for f in (r.json() or {}).get('app', []) if str(f).endswith('.js')] or files
    texts = []
    for f in files:
        j = http.request('GET', base + f + '?' + query, headers={})
        if j.status == 200:
            texts.append(j.text)
    return texts
